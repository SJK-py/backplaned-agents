"""Webapp — public self-service signup page (`/register`) + the logged-in
link-code generator (Settings → Connect a chat channel).

`/register` is pre-auth + CSRF-exempt (like /login): the anonymous visitor
chooses a password and the webapp proxies it to the router's unauthenticated
`POST /v1/registrations/public`. The link-code form is authenticated and
mints a single-use token via `POST /v1/auth/link-tokens`.
"""

from __future__ import annotations

import pytest


class _Upstream:
    """Fake router client covering the two new webapp calls."""

    def __init__(self) -> None:
        self.registrations: list[tuple[str, str, str | None]] = []
        self.register_error: Exception | None = None
        self.link_calls: int = 0
        self.link_error: Exception | None = None

    async def submit_web_registration(
        self, *, email: str, password: str, display_name: str | None = None
    ) -> dict:
        self.registrations.append((email, password, display_name))
        if self.register_error is not None:
            raise self.register_error
        return {"registration_id": "reg_1", "status": "pending", "attempts": 1}

    async def mint_link_token(self, *, access_token: str) -> dict:
        self.link_calls += 1
        if self.link_error is not None:
            raise self.link_error
        return {"link_token": "lnk-abc123", "expires_at": "2999-01-01T00:00:00+00:00"}

    async def aclose(self) -> None:
        pass


def _build_app(upstream: object):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr  # noqa: PLC0415

    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415
    from bp_agents.agents.webapp.config import WebappConfig  # noqa: PLC0415

    cfg = WebappConfig(session_secret=SecretStr("x" * 32), session_cookie_secure=False)
    return create_app(cfg, upstream=upstream, pool=None, core=None)


def _client(app):
    from fastapi.testclient import TestClient  # noqa: PLC0415

    return TestClient(app)


def test_register_page_renders_form_and_disclaimer() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream())) as c:
        html = c.get("/register").text
    assert 'action="/register"' in html
    # The recovery / notifications disclaimer is present.
    assert "no email-based password reset" in html
    assert "Telegram" in html


def test_login_page_links_to_register() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream())) as c:
        html = c.get("/login").text
    assert 'href="/register"' in html


def test_register_is_public_and_csrf_exempt() -> None:
    """No session + no CSRF token, yet the POST reaches the handler (not a
    login redirect, not a 403)."""
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.post(
            "/register",
            data={"email": "a@b.co", "password": "hunter2hunter",
                  "confirm_password": "hunter2hunter", "display_name": "A"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers.get("location") == "/login?registered=1"
    assert up.registrations == [("a@b.co", "hunter2hunter", "A")]


def test_register_rejects_mismatch_without_calling_router() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.post(
            "/register",
            data={"email": "a@b.co", "password": "hunter2hunter",
                  "confirm_password": "different12", "display_name": ""},
            follow_redirects=False,
        )
    assert r.headers.get("location") == "/register?error=mismatch"
    assert up.registrations == []


def test_register_rejects_short_password_without_calling_router() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.post(
            "/register",
            data={"email": "a@b.co", "password": "short",
                  "confirm_password": "short", "display_name": ""},
            follow_redirects=False,
        )
    assert r.headers.get("location") == "/register?error=weak"
    assert up.registrations == []


def test_register_rate_limit_maps_to_friendly_error() -> None:
    pytest.importorskip("fastapi")
    from bp_agents.agents.webapp.upstream import UpstreamError  # noqa: PLC0415

    up = _Upstream()
    up.register_error = UpstreamError(429, "slow down")
    with _client(_build_app(up)) as c:
        r = c.post(
            "/register",
            data={"email": "a@b.co", "password": "hunter2hunter",
                  "confirm_password": "hunter2hunter", "display_name": ""},
            follow_redirects=False,
        )
    assert r.headers.get("location") == "/register?error=ratelimited"


def test_register_duplicate_is_enumeration_safe() -> None:
    """A non-429/422 upstream error (e.g. a duplicate surfaced as 4xx) still
    lands the neutral "request received" outcome — never reveals the email."""
    pytest.importorskip("fastapi")
    from bp_agents.agents.webapp.upstream import UpstreamError  # noqa: PLC0415

    up = _Upstream()
    up.register_error = UpstreamError(409, "exists")
    with _client(_build_app(up)) as c:
        r = c.post(
            "/register",
            data={"email": "a@b.co", "password": "hunter2hunter",
                  "confirm_password": "hunter2hunter", "display_name": ""},
            follow_redirects=False,
        )
    assert r.headers.get("location") == "/login?registered=1"
