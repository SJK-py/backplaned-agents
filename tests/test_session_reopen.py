"""Session reopen — `POST /v1/sessions/{id}/reopen` + the webapp wiring.

Router side: source-inspection guards pin the query + endpoint contract; the
real-DB test (guarded by `TEST_DB_URL`) confirms the open → close → reopen
round-trip, including idempotency on an already-open session.

Webapp side: a phase6-style behavioral test (httpx ASGITransport, guarded by
`SUITE_DATABASE_URL`) confirms the handler calls the upstream, redirects into
the chat to resume, and 404s an unowned session — plus a template check that
the Reopen/Close buttons toggle on `closed`.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re

import asyncpg
import httpx
import pytest

from bp_router.api import sessions as sessions_mod
from bp_router.db import queries

# ---------------------------------------------------------------------------
# Router — query + endpoint contract (source-inspection)
# ---------------------------------------------------------------------------


def test_reopen_query_clears_closed_at_and_is_conditional() -> None:
    src = inspect.getsource(queries.Scope.reopen_session)
    # Sets closed_at to NULL (re-open), not a timestamp.
    assert "closed_at = NULL" in src
    # Only transitions rows that ARE closed — keeps it a no-op (and the
    # returned bool honest) on an already-open session.
    assert "closed_at IS NOT NULL" in src
    # User-scoped — can't reopen another user's session.
    assert "user_id = $2" in src
    # Detects the change via RETURNING (not by parsing the command tag).
    assert "RETURNING session_id" in src


def test_reopen_endpoint_audits_idempotently_and_404s() -> None:
    src = inspect.getsource(sessions_mod.reopen_session)
    assert 'event="session.reopened"' in src
    assert "reopen_session(session_id)" in src
    assert "404" in src  # 404 when the session isn't the caller's
    # Idempotent: only reopens + audits when the row is actually closed.
    assert "closed_at is not None" in src


# ---------------------------------------------------------------------------
# Router — real-DB round-trip
# ---------------------------------------------------------------------------


def test_reopen_session_roundtrip(test_db_url: str) -> None:
    """open → close → reopen → idempotent, against the live schema."""

    async def _drive() -> None:
        conn = await asyncpg.connect(test_db_url)
        try:
            # Match the router pool's jsonb codec so dict ↔ jsonb (session
            # metadata) round-trips through the real Scope queries.
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads,
                schema="pg_catalog",
            )
            await conn.execute("TRUNCATE users, sessions RESTART IDENTITY CASCADE")
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_r', 'tier0', 'password')"
            )
            scope = queries.Scope.user(conn, "usr_r")
            row = await scope.open_session()
            sid = row.session_id

            # Reopening an already-open session is a no-op.
            assert await scope.reopen_session(sid) is False

            # Close, then reopen.
            await scope.close_session(sid)
            assert (await scope.get_session(sid)).closed_at is not None
            assert await scope.reopen_session(sid) is True
            assert (await scope.get_session(sid)).closed_at is None

            # Idempotent again now that it's open.
            assert await scope.reopen_session(sid) is False

            # Scoped: another user can't reopen it.
            await scope.close_session(sid)
            other = queries.Scope.user(conn, "usr_other")
            assert await other.reopen_session(sid) is False
            assert (await scope.get_session(sid)).closed_at is not None
        finally:
            await conn.close()

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Webapp — handler behaviour + template (phase6 style)
# ---------------------------------------------------------------------------


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream:
    def __init__(self, *, sub: str = "usr_a", sessions: list[dict] | None = None) -> None:
        self._sub = sub
        self._sessions = sessions or []
        self.reopened: list[str] = []

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def list_sessions(self, *, access_token):
        return self._sessions

    async def reopen_session(self, *, access_token, session_id):
        self.reopened.append(session_id)
        return {"session_id": session_id, "opened_at": "2026-05-01T00:00:00Z",
                "closed_at": None}

    async def aclose(self):
        pass


def _build_app(*, upstream, pool):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr  # noqa: PLC0415

    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415
    from bp_agents.agents.webapp.config import WebappConfig  # noqa: PLC0415

    cfg = WebappConfig(session_secret=SecretStr("x" * 32), session_cookie_secure=False)
    return create_app(cfg, upstream=upstream, pool=pool, core=None)


async def _seed(pool) -> None:
    from bp_agents.db import queries as suite_q  # noqa: PLC0415

    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, cron_jobs, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        await suite_q.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="webapp",
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str = "/") -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', (await client.get(path)).text)
    return m.group(1) if m else ""


def test_reopen_session_resumes_chat_via_router(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")
    from bp_agents.db.connection import open_pool  # noqa: PLC0415
    from bp_agents.settings import SuiteSettings  # noqa: PLC0415

    async def _drive() -> tuple[int, str | None, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            up = _Upstream()
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                r = await client.post(
                    "/sessions/ses_1/reopen", headers={"X-CSRF-Token": token}
                )
            return r.status_code, r.headers.get("HX-Redirect"), up.reopened
        finally:
            await pool.close()

    status, redirect, reopened = asyncio.run(_drive())
    assert status == 204
    assert redirect == "/chat/ses_1"  # resume the conversation
    assert reopened == ["ses_1"]  # upstream reopen called exactly once


def test_reopen_404_for_unowned_session(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")
    from bp_agents.db.connection import open_pool  # noqa: PLC0415
    from bp_agents.settings import SuiteSettings  # noqa: PLC0415

    async def _drive() -> tuple[int, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # only ses_1 (usr_a) exists
            up = _Upstream()
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                r = await client.post(
                    "/sessions/ses_nope/reopen", headers={"X-CSRF-Token": token}
                )
            return r.status_code, up.reopened
        finally:
            await pool.close()

    status, reopened = asyncio.run(_drive())
    assert status == 404
    assert reopened == []  # never reached the upstream


def test_session_list_toggles_reopen_and_close_buttons(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")
    from bp_agents.db.connection import open_pool  # noqa: PLC0415
    from bp_agents.settings import SuiteSettings  # noqa: PLC0415

    async def _drive(closed: bool) -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            up = _Upstream(sessions=[
                {"session_id": "ses_1", "opened_at": "2026-05-01T00:00:00Z",
                 "closed_at": "2026-05-02T00:00:00Z" if closed else None},
            ])
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                return (await client.get("/")).text
        finally:
            await pool.close()

    closed_html = asyncio.run(_drive(closed=True))
    assert 'hx-post="/sessions/ses_1/reopen"' in closed_html
    assert 'hx-post="/sessions/ses_1/close"' not in closed_html

    open_html = asyncio.run(_drive(closed=False))
    assert 'hx-post="/sessions/ses_1/close"' in open_html
    assert 'hx-post="/sessions/ses_1/reopen"' not in open_html
