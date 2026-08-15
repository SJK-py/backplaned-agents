"""Regression: web/OIDC accounts must get a suite-side `user_config` row.

Chat users get one from the chatbot approval reconcile; web-first and OIDC
accounts never go through it. `ensure_user_config` seeds it idempotently.

**Half of the original bug is now structurally gone.** It was: with no row,
`update_user_config` patched zero rows, so the webapp reported 'saved' while
nothing changed. The user's SETTINGS moved to the router's user scope, where
a write creates the key and an absent key is simply the operator default —
there is no row to be missing and no `update_user_config` left to no-op.

What survives is the pointer half: `set_default_session_id` still patches
zero rows for a user with no `user_config`, which would lose the cron
fallback. That is what this file now pins.
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


def test_setting_the_default_session_without_a_row_is_a_silent_noop(
    suite_db_url: str,
) -> None:
    """Documents the surviving bug: pointing the cron fallback at a session
    for a user with no row changes nothing and raises nothing."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            uid = "usr_noconfig_1"
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM user_config WHERE user_id = $1", uid)
                await queries.set_default_session_id(
                    conn, user_id=uid, session_id="ses_lost"
                )
                assert await queries.get_user_config(conn, uid) is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_ensure_user_config_creates_the_row_then_the_pointer_persists(
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
            assert cfg.default_session_id is None

            # Idempotent: a second ensure doesn't duplicate or reset.
            await ensure_user_config(req)

            # And now the pointer actually persists.
            async with pool.acquire() as conn:
                await queries.set_default_session_id(
                    conn, user_id=uid, session_id="ses_kept"
                )
                cfg2 = await queries.get_user_config(conn, uid)
            assert cfg2 is not None and cfg2.default_session_id == "ses_kept"
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
