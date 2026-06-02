"""Webapp Phase 7 — polish: Telegram one-time note, prod CSS switch, mobile.

The Telegram-origin note ([webapp.md] §4) renders on the chat view (and
not for a web session). The prod CSS path swaps the Tailwind Play CDN for
a built stylesheet when WEBAPP_USE_BUILT_CSS is set. Mobile: the session
table scrolls horizontally and the drawer has a backdrop.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream:
    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt("usr_a"), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def list_sessions(self, *, access_token):
        return [{"session_id": "ses_tg", "opened_at": "2026-05-01T00:00:00Z",
                 "closed_at": None}]

    async def aclose(self):
        pass


def _build_app(*, pool, use_built_css: bool = False):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr  # noqa: PLC0415

    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415
    from bp_agents.agents.webapp.config import WebappConfig  # noqa: PLC0415

    cfg = WebappConfig(
        session_secret=SecretStr("x" * 32), session_cookie_secure=False,
        use_built_css=use_built_css,
    )
    return create_app(cfg, upstream=_Upstream(), pool=pool, core=None)


async def _seed(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        await queries.create_session_info(
            conn, session_id="ses_tg", user_id="usr_a", channel="chatbot_telegram",
            chat_id="tg1",
        )
        await queries.create_session_info(
            conn, session_id="ses_web", user_id="usr_a", channel="webapp",
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


def _chat(suite_db_url: str, session_id: str) -> str:
    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get(f"/chat/{session_id}")
            assert r.status_code == 200, r.text[:300]
            return r.text
        finally:
            await pool.close()

    return asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Telegram one-time note
# ---------------------------------------------------------------------------


def test_telegram_session_shows_one_time_note(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")
    html = _chat(suite_db_url, "ses_tg")
    assert "started on Telegram" in html
    # Per-session, per-browser dismissal via localStorage (channel-agnostic key).
    assert "chatnote:ses_tg" in html
    assert "Dismiss" in html


def test_web_session_has_no_telegram_note(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")
    html = _chat(suite_db_url, "ses_web")
    assert "started on Telegram" not in html


# ---------------------------------------------------------------------------
# Prod CSS switch
# ---------------------------------------------------------------------------


def test_dev_uses_cdn_prod_uses_built_css(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    async def _drive() -> tuple[str, str]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            dev_app = _build_app(pool=pool, use_built_css=False)
            prod_app = _build_app(pool=pool, use_built_css=True)
            with TestClient(dev_app) as c:
                dev = c.get("/login").text
            with TestClient(prod_app) as c:
                prod = c.get("/login").text
            return dev, prod
        finally:
            await pool.close()

    dev, prod = asyncio.run(_drive())
    assert "cdn.tailwindcss.com" in dev and "/static/tailwind.css" not in dev
    assert "/static/tailwind.css" in prod and "cdn.tailwindcss.com" not in prod


# ---------------------------------------------------------------------------
# Mobile polish
# ---------------------------------------------------------------------------


def test_session_list_table_scrolls_on_mobile(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/")
            return r.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert "overflow-x-auto" in html  # table scrolls instead of clipping
    assert 'class="fixed inset-0 z-10 bg-black/30 md:hidden"' in html  # drawer backdrop
