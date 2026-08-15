"""chatbot.cron — the scheduler (firing) + cron-management tools.

Scheduler ([cron.md]): poll active jobs each minute, compute the most
recent due time in the job's timezone, atomically claim it (no
double-fire), and run `orchestrator(cron_message)` for the resolved
session. The job's `report` policy rides the payload, so the orchestrator —
the task's active executor, and the only thing that can write its own
thread — decides and records the result itself; the scheduler then only
DELIVERS (send to the channel, or nudge an unreachable one) and always
writes a `cron_executions` row.

Management: `make_cron_tools` builds the add/list/remove/modify local
tools the chatbot's `cron` mode loop uses.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from bp_agents.agents.chatbot.gateway import send_named_file
from bp_agents.channel.store import StoreError
from bp_agents.common.payloads import CronMessage
from bp_agents.db import queries

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.agents.chatbot.credentials import ChannelCredentials
    from bp_agents.agents.chatbot.gateway import RootDispatcher
    from bp_agents.agents.chatbot.telegram import TelegramClient
    from bp_agents.channel.store import SessionStore
    from bp_agents.db.models import CronJobRow
    from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT_ID = "orchestrator"


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class CronScheduler:
    def __init__(
        self,
        *,
        dispatcher: RootDispatcher,
        pool: asyncpg.Pool,
        settings: SuiteSettings,
        telegram: TelegramClient | None,
        store: SessionStore,
        credentials: ChannelCredentials | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._pool = pool
        self._settings = settings
        self._telegram = telegram
        self._store = store
        self._credentials = credentials

    async def run_loop(self, stop: asyncio.Event, *, interval_s: float = 60.0) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("cron_tick_error", extra={"event": "cron_tick_error"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass

    async def tick(self, *, now: datetime | None = None) -> int:
        """One scheduler pass. Returns the number of jobs fired."""
        now = now or datetime.now(UTC)
        async with self._pool.acquire() as conn:
            jobs = await queries.list_active_cron_jobs(conn)
        fired = 0
        for job in jobs:
            if await self._maybe_fire(job, now):
                fired += 1
        return fired

    async def _maybe_fire(self, job: CronJobRow, now: datetime) -> bool:
        # Most recent scheduled time at/just before `now`, in the job's tz.
        local_now = now.astimezone(_tz(job.timezone))
        try:
            prev_local = croniter(job.cron_expression, local_now).get_prev(datetime)
        except (ValueError, KeyError):
            logger.warning(
                "cron_bad_expression",
                extra={"event": "cron_bad_expression", "cron_id": job.cron_id},
            )
            return False
        due = prev_local.astimezone(UTC)
        # Missed-firing bound: only the single most-recent window fires, so
        # downtime never replays the whole gap ([cron.md] §1).
        if job.last_executed_at is not None and due <= job.last_executed_at:
            return False
        async with self._pool.acquire() as conn:
            claimed = await queries.claim_cron_job(
                conn, cron_id=job.cron_id, due=due, now=now
            )
        if not claimed:
            return False
        await self._execute(job, now)
        return True

    async def _session_metadata(
        self, user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """The router's metadata for a session, or None if it isn't the
        user's / doesn't exist. An empty patch is a read that also 404s on a
        session that is gone, which is the other thing we need to know."""
        try:
            return await self._store.patch_metadata(
                user_id=user_id, session_id=session_id, patch={}
            )
        except StoreError:
            return None

    async def _resolve_session(self, job: CronJobRow) -> str:
        """job.session_id if it's a real session, else the user's default
        ([cron.md] §2). (C4 open-a-fresh-session fallback is deferred.)"""
        if await self._session_metadata(job.user_id, job.session_id) is not None:
            return job.session_id
        async with self._pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, job.user_id)
        if cfg and cfg.default_session_id:
            return cfg.default_session_id
        return job.session_id

    async def _execute(self, job: CronJobRow, now: datetime) -> None:
        session_id = await self._resolve_session(job)
        message: str | None = None
        reason: str | None = None
        reported = False
        error: str | None = None
        try:
            task_id = await self._dispatcher.spawn_root_for_user(
                ORCHESTRATOR_AGENT_ID,
                CronMessage(prompt=job.cron_message, report=job.report),
                user_id=job.user_id, session_id=session_id, mode="cron_message",
            )
            result = await self._dispatcher.await_root_result(
                task_id, timeout_s=self._settings.dispatch_result_timeout_s
            )
            out = result.output
            message = (out.content if out else None) or ""
            out_files = list(out.files) if out else []
            meta = out.metadata if out else {}
            reason = meta.get("reason")
            # The orchestrator already applied the job's policy and recorded
            # the row; this is the delivery decision only.
            reported = bool(meta.get("report", False))
        except Exception as exc:  # noqa: BLE001
            # C3: log the error, mark executed anyway (no retry storm).
            logger.exception("cron_execute_failed", extra={"event": "cron_execute_failed"})
            error = type(exc).__name__
            out_files = []

        if reported and (message or out_files):
            # Delivery only — the orchestrator wrote the canonical row, which
            # is why this no longer needs the session serialized.
            metadata = await self._session_metadata(job.user_id, session_id) or {}
            chat_id = metadata.get("external_id")
            if self._telegram is not None and chat_id:
                # The target session is itself Telegram-reachable — deliver
                # the full result there.
                if message:
                    try:
                        await self._telegram.send_message(chat_id=chat_id, text=message)
                    except Exception:  # noqa: BLE001 — C5: row already appended
                        logger.exception("cron_send_failed", extra={"event": "cron_send_failed"})
                # Deliver any files the run produced for the user ([channel.md] §7).
                for name in out_files:
                    await send_named_file(
                        telegram=self._telegram, credentials=self._credentials,
                        chat_id=chat_id, user_id=job.user_id,
                        session_id=session_id, name=name,
                    )
            else:
                # The target session isn't live-reachable by this scheduler
                # (a webapp / released session, no Telegram chat_id). The
                # result is already persisted there; route a pointer nudge to
                # the user's reachable channel so they know to open it
                # ([cron.md] §6, channel-agnostic routing).
                await self._nudge_unreachable(job, metadata)

        async with self._pool.acquire() as conn:
            await queries.record_cron_execution(
                conn, cron_id=job.cron_id, user_id=job.user_id,
                session_id=session_id, reported=reported, reason=reason,
                message=message if reported else None, error=error,
            )
            if job.execute_until is not None and now >= job.execute_until:
                await queries.deactivate_cron_job(conn, job.cron_id)

    async def _nudge_unreachable(
        self, job: CronJobRow, metadata: dict[str, Any]
    ) -> None:
        """Channel-agnostic fallback ([cron.md] §6): the cron's result landed
        in a session this scheduler can't deliver to live (e.g. a webapp
        session — no Telegram `chat_id`). Send a pointer to the user's
        Telegram chat so they know to open the session; the full result is
        already persisted there. Best-effort — a user with no Telegram
        mapping simply sees it on their next visit (the row is the record)."""
        if self._telegram is None:
            return
        async with self._pool.acquire() as conn:
            mappings = await queries.list_platform_mappings_for_user(
                conn, user_id=job.user_id, platform="telegram"
            )
        if not mappings:
            return  # no out-of-band channel — the persisted row is the record
        where = (
            "your web app" if metadata.get("kind") == "webapp"
            else "another session"
        )
        nudge = f"⏰ A scheduled task just ran in {where}. Open it to see the result."
        for m in mappings:
            try:
                await self._telegram.send_message(chat_id=m.chat_id, text=nudge)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "cron_nudge_failed", extra={"event": "cron_nudge_failed"}
                )

