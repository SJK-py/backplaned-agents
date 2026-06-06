"""Suite-side user purge — `purge_user_suite_data` erases a user's suite rows,
and `reconcile_purged_users` reaps only users the router reports as purged.
Gated on `suite_db_url`. (Per-user LanceDB is erased by the memory/KB agents.)
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.session_gc import reconcile_purged_users
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


class _StubCredentials:
    def __init__(self, purged: set[str]) -> None:
        self._purged = purged
        self.probed: list[str] = []

    async def filter_purged_users(self, user_ids: list[str]) -> set[str]:
        self.probed.extend(user_ids)
        return {u for u in user_ids if u in self._purged}


async def _seed_user(pool, user_id: str, session_id: str) -> None:
    async with pool.acquire() as conn:
        await queries.create_user_config(
            conn, user_id=user_id, default_session_id=session_id,
            preset_pro="p", preset_balanced="b", preset_lite="l",
            preset_embedding="e", language="en",
        )
        await queries.create_session_info(
            conn, session_id=session_id, user_id=user_id, channel="webapp",
        )
        await queries.append_history(
            conn, session_id=session_id, agent_id="orchestrator",
            role="user", message="hi",
        )
        await queries.upsert_platform_mapping(
            conn, platform="telegram", chat_id=f"chat_{user_id}",
            user_id=user_id, session_id=session_id,
        )


def _settings(url: str) -> SuiteSettings:
    return SuiteSettings(database_url=url)


async def _counts(pool, user_id: str, session_id: str) -> dict[str, int]:
    async with pool.acquire() as conn:
        return {
            "user_config": await conn.fetchval(
                "SELECT count(*) FROM user_config WHERE user_id=$1", user_id),
            "session_info": await conn.fetchval(
                "SELECT count(*) FROM session_info WHERE user_id=$1", user_id),
            "session_history": await conn.fetchval(
                "SELECT count(*) FROM session_history WHERE session_id=$1", session_id),
            "mappings": await conn.fetchval(
                "SELECT count(*) FROM suite_platform_mappings WHERE user_id=$1", user_id),
        }


def _truncate_sql() -> str:
    return ("TRUNCATE session_history, session_info, cron_jobs, "
            "user_config, suite_platform_mappings RESTART IDENTITY")


def test_purge_user_suite_data_erases_all_rows(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(_settings(suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(_truncate_sql())
            await _seed_user(pool, "usr_a", "ses_a")
            before = await _counts(pool, "usr_a", "ses_a")
            assert all(v == 1 for v in before.values())

            async with pool.acquire() as conn, conn.transaction():
                counts = await queries.purge_user_suite_data(conn, "usr_a")
            assert counts["user_config"] == 1 and counts["session_history"] == 1

            after = await _counts(pool, "usr_a", "ses_a")
            assert all(v == 0 for v in after.values())
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_reconcile_reaps_only_router_purged_users(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(_settings(suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(_truncate_sql())
            await _seed_user(pool, "usr_purged", "ses_p")
            await _seed_user(pool, "usr_live", "ses_l")

            creds = _StubCredentials(purged={"usr_purged"})
            erased = await reconcile_purged_users(pool, creds)
            assert erased == 1
            assert set(creds.probed) == {"usr_purged", "usr_live"}

            # Purged user's suite data gone; live user's intact.
            assert all(v == 0 for v in (await _counts(pool, "usr_purged", "ses_p")).values())
            assert all(v == 1 for v in (await _counts(pool, "usr_live", "ses_l")).values())
        finally:
            await pool.close()

    asyncio.run(_drive())
