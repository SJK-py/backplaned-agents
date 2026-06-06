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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from bp_agents.db import queries

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.agents.chatbot.credentials import ChannelCredentials
    from bp_agents.settings import SuiteSettings

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
    batch: int = 500,
) -> int:
    """One user-purge reconcile pass. Returns the number of users erased
    suite-side.

    Lists up to `batch` users the suite holds config for, asks the router
    which are permanently purged, and erases their suite rows
    (`purge_user_suite_data`). Self-healing and idempotent; holds no router
    delete authority. (Per-user LanceDB is erased by the memory/KB agents,
    which own that volume.)
    """
    async with pool.acquire() as conn:
        ids = await queries.list_user_config_ids(conn, limit=batch)
    if not ids:
        return 0
    purged = await credentials.filter_purged_users(ids)
    erased = 0
    for uid in purged:
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


async def session_gc_loop(
    *,
    credentials: ChannelCredentials,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
    stop: asyncio.Event,
    interval_s: float | None = None,
) -> None:
    """Periodically reconcile suite-side data against the router: closed
    sessions (history) AND permanently purged users (config + remaining
    rows). No-op (returns immediately) when `session_gc_retention_days` is 0."""
    retention = settings.session_gc_retention_days
    if retention <= 0:
        return
    period = interval_s if interval_s is not None else settings.session_gc_interval_s
    while not stop.is_set():
        try:
            await reconcile_closed_sessions(
                pool, credentials, retention_days=retention
            )
            await reconcile_purged_users(pool, credentials)
        except Exception:  # noqa: BLE001
            logger.exception(
                "suite_session_gc_failed",
                extra={"event": "suite_session_gc_failed"},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except TimeoutError:
            pass
