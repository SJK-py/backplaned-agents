"""Webapp left-panel session list + Telegram close-block.

The sidebar (`GET /sidebar/sessions`, HTMX partial) groups sessions Open /
Closed: open rows are clickable with a Close button unless Telegram-linked
(which shows a TG flag and no Close); closed rows expose Reopen + Remove.
A Telegram-linked session can't be closed from the web app (409). Driven on
one loop via httpx.ASGITransport against a live suite DB.
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


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream:
    def __init__(self, *, sub: str = "usr_a", sessions: list[dict] | None = None) -> None:
        self._sub = sub
        self._sessions = sessions or []
        self.deleted: list[tuple[str, bool]] = []

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def list_sessions(self, *, access_token):
        return self._sessions

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


async def _seed(pool, rows: list[tuple[str, str | None]]) -> None:
    """rows = [(session_id, channel|None)] for usr_a."""
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, cron_jobs, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        for sid, channel in rows:
            await queries.create_session_info(
                conn, session_id=sid, user_id="usr_a", channel=channel,
            )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str = "/") -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', (await client.get(path)).text)
    return m.group(1) if m else ""


_TS = "2026-05-01T00:00:00Z"


def test_sidebar_groups_and_button_rules(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, [
                ("ses_web", "webapp"),
                ("ses_tg", "chatbot_telegram"),
                ("ses_kt", "chatbot_kakao"),
                ("ses_closed", None),
            ])
            up = _Upstream(sessions=[
                {"session_id": "ses_web", "opened_at": _TS, "closed_at": None},
                {"session_id": "ses_tg", "opened_at": _TS, "closed_at": None},
                {"session_id": "ses_kt", "opened_at": _TS, "closed_at": None},
                {"session_id": "ses_closed", "opened_at": _TS,
                 "closed_at": "2026-05-02T00:00:00Z"},
            ])
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                return (await client.get("/sidebar/sessions")).text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    # Grouping headers.
    assert "Open" in html and "Closed" in html
    # Open webapp session: clickable + Close.
    assert 'href="/chat/ses_web"' in html
    assert 'hx-post="/sessions/ses_web/close"' in html
    # Open Telegram session: TG flag, NO close button.
    assert ">TG<" in html
    assert 'hx-post="/sessions/ses_tg/close"' not in html
    # Open KakaoTalk session: KT flag, NO close button (same chatbot guard).
    assert ">KT<" in html
    assert 'hx-post="/sessions/ses_kt/close"' not in html
    # Closed session: not clickable, Reopen + Remove.
    assert 'href="/chat/ses_closed"' not in html
    assert 'hx-post="/sessions/ses_closed/reopen"' in html
    assert 'hx-post="/sessions/ses_closed/remove"' in html


def test_close_telegram_session_is_blocked(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, list, int, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, [("ses_tg", "chatbot_telegram"), ("ses_web", "webapp")])
            up = _Upstream()
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                tg = await client.post(
                    "/sessions/ses_tg/close", headers={"X-CSRF-Token": token}
                )
                web = await client.post(
                    "/sessions/ses_web/close", headers={"X-CSRF-Token": token}
                )
            return tg.status_code, up.deleted, web.status_code, [
                web.headers.get("HX-Trigger")
            ]
        finally:
            await pool.close()

    tg_status, deleted, web_status, web_trigger = asyncio.run(_drive())
    assert tg_status == 409  # Telegram-linked → refused
    assert ("ses_tg", False) not in deleted  # never reached the router
    # A webapp session still closes, and signals the panel to refresh.
    assert web_status == 204
    assert deleted == [("ses_web", False)]
    assert web_trigger == ["sessionsChanged"]


def test_close_kakao_session_is_blocked(suite_db_url: str) -> None:
    """A KakaoTalk-linked session is protected from the web-app close the same
    way Telegram is — it's retired from the chatbot (`/new`)."""
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, [("ses_kt", "chatbot_kakao")])
            up = _Upstream()
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                kt = await client.post(
                    "/sessions/ses_kt/close", headers={"X-CSRF-Token": token}
                )
            return kt.status_code, up.deleted
        finally:
            await pool.close()

    kt_status, deleted = asyncio.run(_drive())
    assert kt_status == 409  # KakaoTalk-linked → refused
    assert deleted == []  # never reached the router


def test_rename_sets_name_via_hx_prompt(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str | None, str | None, int, int]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, [("ses_web", "webapp")])
            app = _build_app(upstream=_Upstream(), pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/")
                ok = await client.post(
                    "/sessions/ses_web/rename",
                    headers={"X-CSRF-Token": token, "HX-Prompt": "Trip to Japan"},
                )
                empty = await client.post(
                    "/sessions/ses_web/rename",
                    headers={"X-CSRF-Token": token, "HX-Prompt": "   "},
                )
                missing = await client.post(
                    "/sessions/ses_nope/rename",
                    headers={"X-CSRF-Token": token, "HX-Prompt": "x"},
                )
                async with pool.acquire() as conn:
                    info = await queries.get_session_info(conn, "ses_web")
            return (ok.status_code, ok.headers.get("HX-Trigger"),
                    info.session_name, empty.status_code, missing.status_code)
        finally:
            await pool.close()

    status, trigger, name, empty_status, missing_status = asyncio.run(_drive())
    assert status == 204 and trigger == "sessionsChanged"
    assert name == "Trip to Japan"
    assert empty_status == 400   # blank name rejected
    assert missing_status == 404  # not the caller's session


def test_sidebar_shows_session_name_over_id(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, [("ses_web", "webapp")])
            async with pool.acquire() as conn:
                await queries.update_session_info(
                    conn, "ses_web", session_name="Weekend trip plan"
                )
            up = _Upstream(sessions=[
                {"session_id": "ses_web", "opened_at": _TS, "closed_at": None},
            ])
            app = _build_app(upstream=up, pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                return (await client.get("/sidebar/sessions")).text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert "Weekend trip plan" in html  # the friendly title is shown
    assert 'hx-post="/sessions/ses_web/rename"' in html  # rename available on open
    assert 'href="/chat/ses_web"' in html  # still links to the chat
