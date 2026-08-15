"""Regression: web/OIDC accounts must get a suite-side `user_config`.

Chat users get `user_config` seeded by the chatbot approval reconcile;
web-first and OIDC accounts never go through it. Without a row,
`update_user_config` patches zero rows (the webapp 'saves' but nothing
changes) and `get_user_config` returns None (the config agent reads
nothing). The webapp's `ensure_user_config` seeds the row idempotently.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


def _stub_request(pool, user_id, settings):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool,
        )),
        session={"user_id": user_id},
    )


def test_update_without_create_is_a_silent_noop(suite_db_url: str) -> None:
    """Documents the bug: patching a non-existent row changes nothing and
    raises nothing — exactly the 'saved but unchanged' symptom."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            uid = "usr_noconfig_1"
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM user_config WHERE user_id = $1", uid)
                await queries.update_user_config(conn, uid, full_name="Nope")
                assert await queries.get_user_config(conn, uid) is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_ensure_user_config_creates_the_row_then_update_persists(
    suite_db_url: str,
) -> None:
    from bp_agents.agents.webapp.pages._common import ensure_user_config

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            uid = "usr_web_oidc_1"
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM user_config WHERE user_id = $1", uid)

            req = _stub_request(pool, uid, SimpleNamespace())

            # First ensure creates the row.
            await ensure_user_config(req)
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, uid)
            assert cfg is not None
            assert cfg.timezone == "UTC"

            # Idempotent: a second ensure doesn't duplicate or reset.
            await ensure_user_config(req)

            # And now an update actually persists (the fixed save path).
            async with pool.acquire() as conn:
                await queries.update_user_config(conn, uid, full_name="Ada")
                cfg2 = await queries.get_user_config(conn, uid)
            assert cfg2 is not None and cfg2.full_name == "Ada"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_ensure_user_config_noops_without_pool_or_user() -> None:
    """No suite pool (read-only test app) or no session → safe no-op."""
    from bp_agents.agents.webapp.pages._common import ensure_user_config

    async def _drive() -> None:
        # pool=None
        await ensure_user_config(_stub_request(None, "u", SimpleNamespace()))
        # no user_id
        req = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                pool=object())),
            session={},
        )
        await ensure_user_config(req)

    asyncio.run(_drive())
