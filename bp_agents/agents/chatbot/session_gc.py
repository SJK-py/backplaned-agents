"""chatbot.session_gc — suite-side reaper for GC'd sessions' history.

The router's closed-session GC hard-deletes old **closed** sessions from its
own store but can't reach the suite Postgres, where the conversation history
lives. This loop reconciles the gap: it lists the suite's old `session_info`
rows, asks the router which still exist (`filter_existing_sessions`), and
purges `session_history` / `session_info` / `cron_jobs` for the ones the
router has already dropped.

Self-healing (stateless — no cursor) and privilege-light: it holds NO
cross-user purge authority. It only ever deletes the SUITE store, and only for
sessions the router has already removed. Runs in the chatbot gateway, which
already owns the suite pool + a service principal; the GC itself is
deployment-wide (all users), not channel-specific.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bp_agents.db import queries

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.agents.chatbot.credentials import ChannelCredentials
    from bp_agents.settings import SuiteSettings

# Erases one user's per-user LanceDB; returns True iff erased (else retry).
LancePurger = Callable[[str], Awaitable[bool]]

logger = logging.getLogger(__name__)


async def reconcile_closed_sessions(
    pool: asyncpg.Pool,
    credentials: ChannelCredentials,
    *,
    retention_days: int,
    batch: int = 200,
) -> int:
    """One reconcile pass. Returns the number of sessions reaped suite-side.

    Lists up to `batch` suite sessions older than the retention window, asks
    the router which still exist, and purges suite rows for the rest (the
    router already hard-deleted them). Idempotent and safe to repeat.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with pool.acquire() as conn:
        ids = await queries.list_old_session_ids(conn, before=cutoff, limit=batch)
    if not ids:
        return 0
    existing = await credentials.filter_existing_sessions(ids)
    orphans = [sid for sid in ids if sid not in existing]
    reaped = 0
    for sid in orphans:
        async with pool.acquire() as conn, conn.transaction():
            await queries.purge_session_suite_data(conn, sid)
        reaped += 1
    if reaped:
        logger.info(
            "suite_session_gc",
            extra={"event": "suite_session_gc", "reaped": reaped,
                   "considered": len(ids)},
        )
    return reaped


async def reconcile_purged_users(
    pool: asyncpg.Pool,
    credentials: ChannelCredentials,
    *,
    purge_lance: LancePurger,
    batch: int = 500,
) -> int:
    """One user-purge reconcile pass. Returns the number of users erased
    suite-side.

    Lists up to `batch` users the suite holds config for, asks the router
    which are permanently purged, then for each: FIRST erase the per-user
    LanceDB (via `purge_lance`, which spawns a memory task), and ONLY on
    success erase the suite rows (`purge_user_suite_data`). The ordering is
    the retry mechanism — a failed LanceDB erase leaves the user in
    `user_config`, so the next sweep retries; suite rows aren't dropped (which
    would lose the trigger). Self-healing and idempotent; holds no router
    delete authority.
    """
    async with pool.acquire() as conn:
        ids = await queries.list_user_config_ids(conn, limit=batch)
    if not ids:
        return 0
    purged = await credentials.filter_purged_users(ids)
    erased = 0
    for uid in purged:
        if not await purge_lance(uid):
            continue  # LanceDB erase failed — retry next sweep
        async with pool.acquire() as conn, conn.transaction():
            await queries.purge_user_suite_data(conn, uid)
        erased += 1
    if erased:
        logger.info(
            "suite_user_gc",
            extra={"event": "suite_user_gc", "erased": erased,
                   "considered": len(ids)},
        )
    return erased


def make_lance_purger(
    dispatcher: Any,
    credentials: ChannelCredentials,
    *,
    result_timeout_s: float = 60.0,
) -> LancePurger:
    """Build the `purge_lance(user_id) -> bool` callable the reconcile uses.

    Erases a purged user's per-user LanceDB by spawning a `purge_user_data`
    task on the memory agent, run AS this channel's service principal (a live
    `level=service` user) with the target user_id in the payload — the existing
    NewTask path, no new protocol surface. Caches the service-principal
    maintenance session (the admit anchor) and re-opens it if the router
    reports it gone. Returns True iff the memory task terminated SUCCEEDED.
    """
    from bp_protocol.types import TaskStatus  # noqa: PLC0415
    from bp_sdk.peers import SpawnRejected  # noqa: PLC0415

    anchor: dict[str, tuple[str, str] | None] = {"session": None}

    async def purge(user_id: str) -> bool:
        try:
            if anchor["session"] is None:
                anchor["session"] = await credentials.open_maintenance_session()
            svc_user_id, session_id = anchor["session"]
            task_id = await dispatcher.spawn_root_for_user(
                "memory", {"user_id": user_id},
                user_id=svc_user_id, session_id=session_id,
                mode="purge_user_data",
            )
            result = await dispatcher.await_root_result(
                task_id, timeout_s=result_timeout_s
            )
            return getattr(result, "status", None) == TaskStatus.SUCCEEDED
        except SpawnRejected as exc:
            # A stale/closed maintenance session — drop it so the next attempt
            # re-opens. The user stays purge-pending and retries next sweep.
            anchor["session"] = None
            logger.warning(
                "lance_purge_spawn_rejected",
                extra={"event": "lance_purge_spawn_rejected",
                       "bp.user_id": user_id, "reason": str(exc)},
            )
            return False
        except Exception:  # noqa: BLE001
            logger.exception(
                "lance_purge_failed",
                extra={"event": "lance_purge_failed", "bp.user_id": user_id},
            )
            return False

    return purge


async def session_gc_loop(
    *,
    credentials: ChannelCredentials,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
    stop: asyncio.Event,
    dispatcher: Any,
    interval_s: float | None = None,
) -> None:
    """Periodically reconcile suite-side data against the router: closed
    sessions (history) AND permanently purged users (per-user LanceDB via the
    memory agent, then the suite config/rows). No-op (returns immediately) when
    `session_gc_retention_days` is 0. `dispatcher` is the channel Agent, used to
    spawn the memory purge task."""
    retention = settings.session_gc_retention_days
    if retention <= 0:
        return
    period = interval_s if interval_s is not None else settings.session_gc_interval_s
    purge_lance = make_lance_purger(dispatcher, credentials)
    while not stop.is_set():
        try:
            await reconcile_closed_sessions(
                pool, credentials, retention_days=retention
            )
            await reconcile_purged_users(
                pool, credentials, purge_lance=purge_lance
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "suite_session_gc_failed",
                extra={"event": "suite_session_gc_failed"},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except TimeoutError:
            pass
