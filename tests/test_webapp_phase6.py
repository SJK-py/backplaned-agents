"""Webapp Phase 6 — session new / close / remove ([webapp.md] §4, §9).

new   → router POST + suite session_info.
close → router DELETE (archive); suite rows kept.
remove→ router DELETE?purge=true THEN suite cleanup (history/info/cron).
Driven on one loop via httpx.ASGITransport (asyncpg is loop-bound).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import httpx
import pytest

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from tests.fake_store import UpstreamSessionMixin


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream(UpstreamSessionMixin):
    def __init__(self, *, sub: str = "usr_a", sessions: list[dict] | None = None) -> None:
        self._sub = sub
        self.created: list[str] = []
        self.deleted: list[tuple[str, bool]] = []
        if sessions is not None:
            self.sessions.clear()
            for row in sessions:
                self.sessions[row["session_id"]] = {"metadata": {}, **row}

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def create_session(self, *, access_token, metadata=None):
        self.sessions["ses_new"] = {
            "session_id": "ses_new", "opened_at": "2026-05-01T00:00:00Z",
            "closed_at": None, "metadata": dict(metadata or {}),
        }
        self.created.append("ses_new")
        return dict(self.sessions["ses_new"])

    async def delete_session(self, *, access_token, session_id, purge=False):
        self.deleted.append((session_id, purge))

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
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE cron_jobs, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str = "/") -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', (await client.get(path)).text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Suite-side purge cleanup (unit, DB)
# ---------------------------------------------------------------------------


def test_purge_session_suite_data_reclaims_the_suite_side(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[dict, dict]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_1",
                    cron_expression="0 8 * * *", cron_message="x",
                )
                # Another session's job must be untouched.
                await queries.create_cron_job(
                    conn, cron_id="c2", user_id="usr_a", session_id="ses_2",
                    cron_expression="0 9 * * *", cron_message="y",
                )
                counts = await queries.purge_session_suite_data(conn, "ses_1")
                jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
            return counts, {"jobs": [j.cron_id for j in jobs]}
        finally:
            await pool.close()

    counts, after = asyncio.run(_drive())
    # The conversation is the ROUTER's now and goes with its purge; cron is
    # all the suite still keys by session.
    assert counts == {"cron_jobs": 1}
    assert after["jobs"] == ["c2"]


# ---------------------------------------------------------------------------
# New / close / remove endpoints
# ---------------------------------------------------------------------------


def test_new_session_opens_router_and_creates_session_info(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str | None, object, list]:
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
                r = await client.post("/sessions", headers={"X-CSRF-Token": token})
            new_id = up.created[-1] if up.created else None
            metadata = (up.sessions.get(new_id) or {}).get("metadata")
            return r.status_code, r.headers.get("HX-Redirect"), metadata, up.created
        finally:
            await pool.close()

    status, redirect, metadata, created = asyncio.run(_drive())
    assert status == 204
    assert redirect == "/chat/ses_new"
    # Channel origin is the ROUTER session's own metadata — no suite row
    # shadows it any more.
    assert metadata == {"kind": "webapp"}
    assert len(created) == 1  # create_session called exactly once


def test_close_session_archives_via_router(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, list, object]:
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
                    "/sessions/ses_1/close", headers={"X-CSRF-Token": token}
                )
            return r.status_code, up.deleted, up.sessions.get("ses_1")
        finally:
            await pool.close()

    status, deleted, row = asyncio.run(_drive())
    assert status == 204
    assert deleted == [("ses_1", False)]  # archive, not purge
    assert row is not None  # the session (and its history) survive a close


def test_remove_session_purges_router_and_suite(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str | None, list, object, int]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_1",
                    cron_expression="0 8 * * *", cron_message="x",
                )
            up = _Upstream()
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                r = await client.post(
                    "/sessions/ses_1/remove", headers={"X-CSRF-Token": token}
                )
                async with pool.acquire() as conn:
                    jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
            return r.status_code, r.headers.get("HX-Trigger"), up.deleted, None, len(jobs)
        finally:
            await pool.close()

    status, trigger, deleted, _unused, n_jobs = asyncio.run(_drive())
    assert status == 204
    assert trigger == "sessionsChanged"  # refreshes the sidebar / list in place
    assert deleted == [("ses_1", True)]  # purge
    assert n_jobs == 0  # the suite rows the router purge can't reach


def test_close_remove_404_for_unowned_session(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, int]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # only ses_1 (usr_a) exists
            app = _build_app(upstream=_Upstream(), pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                close = await client.post(
                    "/sessions/ses_nope/close", headers={"X-CSRF-Token": token}
                )
                remove = await client.post(
                    "/sessions/ses_nope/remove", headers={"X-CSRF-Token": token}
                )
            return close.status_code, remove.status_code
        finally:
            await pool.close()

    close_status, remove_status = asyncio.run(_drive())
    assert close_status == 404 and remove_status == 404


def test_session_list_shows_new_and_action_buttons(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            up = _Upstream(sessions=[
                {"session_id": "ses_1", "opened_at": "2026-05-01T00:00:00Z",
                 "closed_at": None},  # open
                {"session_id": "ses_2", "opened_at": "2026-05-01T00:00:00Z",
                 "closed_at": "2026-05-02T00:00:00Z"},  # closed
            ])
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/")
            return r.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert 'hx-post="/sessions"' in html  # New session
    # Open row → Close, but Remove is closed-only now.
    assert 'hx-post="/sessions/ses_1/close"' in html
    assert 'hx-post="/sessions/ses_1/remove"' not in html
    # Closed row → Reopen + Remove (not Close).
    assert 'hx-post="/sessions/ses_2/reopen"' in html
    assert 'hx-post="/sessions/ses_2/remove"' in html
    assert 'hx-post="/sessions/ses_2/close"' not in html
    assert "hx-confirm" in html  # remove is confirmed (irreversible)
