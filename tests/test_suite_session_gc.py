"""Suite-side session GC reconcile — reaps the suite's remaining per-session
rows (`cron_jobs`) for sessions the router has already hard-deleted, found via
the `filter_existing_sessions` existence check. The conversation itself is the
router's now and goes with its own purge. Gated on `suite_db_url`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from bp_agents.agents.chatbot.session_gc import reconcile_closed_sessions
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


class _StubCredentials:
    """Records which ids were probed; returns a fixed 'still exists' set."""

    def __init__(self, existing: set[str]) -> None:
        self._existing = existing
        self.probed: list[str] = []

    async def filter_existing_sessions(self, session_ids: list[str]) -> set[str]:
        self.probed.extend(session_ids)
        return {s for s in session_ids if s in self._existing}


async def _seed(pool, session_id: str, *, created_at) -> None:
    """A cron job pins the session suite-side; `created_at` is what the
    retention pre-filter reads."""
    async with pool.acquire() as conn:
        await queries.create_cron_job(
            conn, cron_id=f"c_{session_id}", user_id="usr_a",
            session_id=session_id, cron_expression="0 8 * * *", cron_message="x",
        )
        await conn.execute(
            "UPDATE cron_jobs SET created_at = $2 WHERE session_id = $1",
            session_id, created_at,
        )


def _settings(url: str) -> SuiteSettings:
    return SuiteSettings(database_url=url, session_gc_retention_days=90)


def test_reconcile_reaps_only_router_purged_old_sessions(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(_settings(suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE cron_jobs, "
                    "user_config, suite_platform_mappings RESTART IDENTITY"
                )
            now = datetime.now(UTC)
            old = now - timedelta(days=120)
            recent = now - timedelta(days=5)
            # old + router-purged → reap. old + still-in-router → keep.
            # recent → not even considered (pre-filter).
            await _seed(pool, "ses_gone", created_at=old)
            await _seed(pool, "ses_live", created_at=old)
            await _seed(pool, "ses_recent", created_at=recent)

            creds = _StubCredentials(existing={"ses_live"})  # router still has it
            reaped = await reconcile_closed_sessions(
                pool, creds, retention_days=90
            )
            assert reaped == 1

            async with pool.acquire() as conn:
                remaining = {
                    r["session_id"]
                    for r in await conn.fetch(
                        "SELECT DISTINCT session_id FROM cron_jobs"
                    )
                }
            # ses_gone reaped; ses_live kept (router has it); ses_recent untouched.
            assert remaining == {"ses_live", "ses_recent"}
            # The recent session was pre-filtered out — never probed.
            assert set(creds.probed) == {"ses_gone", "ses_live"}
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_reconcile_noop_when_nothing_old(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(_settings(suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute("TRUNCATE cron_jobs RESTART IDENTITY")
            creds = _StubCredentials(existing=set())
            reaped = await reconcile_closed_sessions(pool, creds, retention_days=90)
            assert reaped == 0
            assert creds.probed == []  # nothing to probe
        finally:
            await pool.close()

    asyncio.run(_drive())
