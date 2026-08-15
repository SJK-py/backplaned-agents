"""Suite Postgres layer — round-trips over `bp_agents.db.queries`.

Assumes the suite schema is applied to `SUITE_DATABASE_URL`
(`alembic -c alembic_suite.ini upgrade head`); truncates the suite
tables at the start of each test. Driven via `asyncio.run` so the file
works on CI matrices without pytest-asyncio (matches test_smoke_e2e).
"""

from __future__ import annotations

import asyncio

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


async def _truncate(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )


def test_suite_db_round_trips(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                # --- user_config: create + idempotence ---
                # Two fields wide now: the user's SETTINGS moved to the
                # router's user scope, so what is left is only what is read
                # outside a task (`bp_agents.user_prefs`).
                cfg = await queries.create_user_config(
                    conn, user_id="usr_a", default_session_id="ses_0",
                )
                assert cfg.default_session_id == "ses_0"
                assert cfg.sandbox_uid is None

                # Idempotent — second create returns the existing row.
                again = await queries.create_user_config(
                    conn, user_id="usr_a", default_session_id="SHOULD-NOT-OVERWRITE",
                )
                assert again.default_session_id == "ses_0"

                await queries.set_default_session_id(
                    conn, user_id="usr_a", session_id="ses_1"
                )
                assert (await queries.get_user_config(conn, "usr_a")).default_session_id == "ses_1"  # noqa: E501

                # --- platform mappings: upsert + resolve + re-bind ---
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-42", user_id="usr_a"
                )
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg-42"
                ) == "usr_a"
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="unmapped"
                ) is None
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-42", user_id="usr_b"
                )
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg-42"
                ) == "usr_b"

                # --- per-chat current session (session_id) ---
                # Seeded on first insert; re-bind without a session_id keeps it
                # (COALESCE), so a later reconcile can't clobber a /new move.
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99",
                    user_id="usr_a", session_id="ses_seed",
                )
                m = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99"
                )
                assert m is not None and m.session_id == "ses_seed"
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99", user_id="usr_a"
                )  # no session_id -> COALESCE preserves the existing one
                m = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99"
                )
                assert m.session_id == "ses_seed"
                # set_mapping_session_id moves it unconditionally (/new path).
                await queries.set_mapping_session_id(
                    conn, platform="telegram", chat_id="tg-99",
                    session_id="ses_moved",
                )
                m = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99"
                )
                assert m.session_id == "ses_moved"
        finally:
            await pool.close()

    asyncio.run(_drive())
