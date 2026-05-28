"""Boot smoke + login/session-list tests for the webapp channel (Phase 2).

Mirrors `tests/test_admin_smoke.py`: a `TestClient(create_app(...))`
round-trip through the full middleware stack (Session → Auth → CSRF),
pinning the bp_admin Bug-4 ordering contract so a future re-order
surfaces in CI. A fake upstream stands in for the router so login +
session-list render without a live server; the Telegram-badge test uses
the suite DB to exercise the `session_info` enrichment.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re

import httpx
import pytest
from pydantic import SecretStr

from bp_agents.agents.webapp.config import WebappConfig
from bp_agents.agents.webapp.upstream import UpstreamError
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


def _fake_jwt(sub: str) -> str:
    """A 3-segment token whose payload decodes to `{"sub": sub}`. The
    webapp only base64-decodes the payload (never verifies the sig), so
    this is enough to drive `user_id` into the session."""
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _FakeUpstream:
    """Stand-in for the router HTTP client."""

    def __init__(self, *, sub: str = "usr_test", sessions: list[dict] | None = None) -> None:
        self._sub = sub
        self._sessions = sessions or []
        self.logged_out = False

    async def login(self, *, email: str, password: str) -> dict:
        if password != "good":
            raise UpstreamError(401, "invalid")
        return {
            "access_token": _fake_jwt(self._sub),
            "refresh_token": "refresh-tok",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "level": "tier1",
        }

    async def refresh(self, *, refresh_token: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub),
            "refresh_token": "refresh-tok",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "level": "tier1",
        }

    async def logout(self, *, access_token: str, refresh_token: str | None = None) -> None:
        self.logged_out = True

    async def list_sessions(self, *, access_token: str) -> list[dict]:
        return self._sessions

    async def aclose(self) -> None:
        pass


def _build_app(upstream: object | None = None, pool: object | None = None):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415

    cfg = WebappConfig(
        session_secret=SecretStr("x" * 32),
        session_cookie_secure=False,  # TestClient is http://
    )
    return create_app(cfg, upstream=upstream or _FakeUpstream(), pool=pool)


# ---------------------------------------------------------------------------
# Boot smoke
# ---------------------------------------------------------------------------


def test_webapp_login_renders_clean() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    with TestClient(_build_app()) as client:
        r = client.get("/login")
    assert r.status_code == 200, r.text[:300]
    assert "<form" in r.text.lower()
    assert "csrf-token" in r.text.lower()


def test_webapp_root_redirects_unauthenticated_to_login() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    with TestClient(_build_app()) as client:
        r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303), r.text[:300]
    assert r.headers.get("location", "").startswith("/login")


# ---------------------------------------------------------------------------
# Middleware ordering (bp_admin Bug-4 contract)
# ---------------------------------------------------------------------------


def test_webapp_session_middleware_outermost_in_built_stack() -> None:
    pytest.importorskip("fastapi")
    from starlette.middleware.sessions import SessionMiddleware  # noqa: PLC0415

    app = _build_app()
    classes = [m.cls for m in app.user_middleware]
    assert classes[0] is SessionMiddleware, [c.__name__ for c in classes]


def test_webapp_session_middleware_added_after_auth_and_csrf() -> None:
    pytest.importorskip("fastapi")
    from bp_agents.agents.webapp import app as app_module  # noqa: PLC0415

    src = inspect.getsource(app_module.create_app)
    auth_idx = src.find("_auth_dispatch")
    csrf_idx = src.find("_csrf_dispatch")
    session_idx = src.find("add_middleware(\n        SessionMiddleware")
    if session_idx == -1:
        session_idx = src.find("add_middleware(SessionMiddleware")
    assert auth_idx > 0 and csrf_idx > 0 and session_idx > 0
    assert auth_idx < session_idx and csrf_idx < session_idx


# ---------------------------------------------------------------------------
# Login round-trip + read-only session list
# ---------------------------------------------------------------------------


def test_webapp_login_rejects_bad_credentials() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    with TestClient(_build_app()) as client:
        r = client.post(
            "/login",
            data={"email": "a@b.c", "password": "wrong", "next": ""},
            follow_redirects=False,
        )
    assert r.status_code == 401
    assert "invalid credentials" in r.text.lower()


def test_webapp_login_then_lists_sessions() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    upstream = _FakeUpstream(
        sessions=[
            {"session_id": "ses_open", "opened_at": "2026-05-01T10:00:00Z", "closed_at": None},
            {"session_id": "ses_closed", "opened_at": "2026-04-01T09:00:00Z",
             "closed_at": "2026-04-02T09:00:00Z"},
        ]
    )
    with TestClient(_build_app(upstream=upstream)) as client:
        r = client.post(
            "/login",
            data={"email": "a@b.c", "password": "good", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]
        assert r.headers.get("location") == "/"

        page = client.get("/")
    assert page.status_code == 200, page.text[:300]
    assert "ses_open" in page.text and "ses_closed" in page.text
    assert "Open" in page.text and "Closed" in page.text


def test_webapp_logout_clears_session_and_redirects() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    upstream = _FakeUpstream()
    with TestClient(_build_app(upstream=upstream)) as client:
        client.post("/login", data={"email": "a@b.c", "password": "good", "next": "/"},
                    follow_redirects=False)
        # Logout needs the CSRF token; pull it from the session via the page.
        page = client.get("/")
        token = _csrf_from_html(page.text)
        r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == "/login"
    assert upstream.logged_out is True


def _csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Telegram badge — suite session_info enrichment (DB)
# ---------------------------------------------------------------------------


def test_webapp_session_list_flags_telegram_channel(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    # Drive the ASGI app via httpx ON THE SAME loop as the pool — asyncpg
    # connections are loop-bound, so the sync TestClient (its own loop)
    # can't share a pool built here.
    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE TABLE session_info, user_config, "
                    "suite_platform_mappings RESTART IDENTITY CASCADE"
                )
                await queries.create_session_info(
                    conn, session_id="ses_tg", user_id="usr_a",
                    channel="chatbot_telegram", chat_id="tg1",
                )
                await queries.create_session_info(
                    conn, session_id="ses_web", user_id="usr_a", channel="webapp",
                )
            upstream = _FakeUpstream(
                sub="usr_a",
                sessions=[
                    {"session_id": "ses_tg", "opened_at": "2026-05-01T10:00:00Z",
                     "closed_at": None},
                    {"session_id": "ses_web", "opened_at": "2026-05-02T10:00:00Z",
                     "closed_at": None},
                ],
            )
            app = _build_app(upstream=upstream, pool=pool)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                await client.post(
                    "/login",
                    data={"email": "a@b.c", "password": "good", "next": "/"},
                )
                page = await client.get("/")
            assert page.status_code == 200, page.text[:300]
            return page.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert "Telegram" in html  # the chatbot_telegram session is flagged
