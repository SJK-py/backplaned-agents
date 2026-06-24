"""WEBAPP_PASSWORD_LOGIN_ENABLED — SSO-only mode.

When false (requires SSO on), the webapp drops every password-credential
path: the login form + its POST, `/register`, and `/set-password`. Only
"Sign in with SSO" remains.
"""

from __future__ import annotations

import pytest


class _Upstream:
    def __init__(self) -> None:
        self.login_calls = 0
        self.registrations = 0

    async def login(self, *, email: str, password: str) -> dict:
        self.login_calls += 1
        return {"access_token": "h.e.s", "refresh_token": "r",
                "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1"}

    async def submit_web_registration(self, **kw) -> dict:
        self.registrations += 1
        return {"registration_id": "r", "status": "pending", "attempts": 1}

    async def aclose(self) -> None:
        pass


def _build_app(upstream, *, password_login_enabled: bool):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr

    from bp_agents.agents.webapp.app import create_app
    from bp_agents.agents.webapp.config import WebappConfig

    cfg = WebappConfig(
        session_secret=SecretStr("x" * 32), session_cookie_secure=False,
        sso_enabled=True, public_base_url="https://app.test",
        password_login_enabled=password_login_enabled,
    )
    return create_app(cfg, upstream=upstream, pool=None, core=None)


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


# --- config validation -----------------------------------------------------


def test_disabling_password_without_sso_is_rejected() -> None:
    pytest.importorskip("fastapi")
    from pydantic import SecretStr, ValidationError

    from bp_agents.agents.webapp.config import WebappConfig

    with pytest.raises(ValidationError):
        WebappConfig(
            session_secret=SecretStr("x" * 32),
            sso_enabled=False, password_login_enabled=False,
        )


# --- SSO-only UI -----------------------------------------------------------


def test_login_page_is_sso_only_when_password_disabled() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream(), password_login_enabled=False)) as c:
        html = c.get("/login").text
    assert "Sign in with SSO" in html
    assert 'name="password"' not in html          # no password field
    assert 'action="/set-password"' not in html   # no reset form
    assert 'href="/register"' not in html         # no self-signup link


def test_login_page_has_password_form_when_enabled() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream(), password_login_enabled=True)) as c:
        html = c.get("/login").text
    assert 'name="password"' in html
    assert 'href="/register"' in html
    assert "Sign in with SSO" in html             # SSO still offered alongside


# --- POST handlers refuse password paths -----------------------------------


def test_post_login_refused_when_disabled() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up, password_login_enabled=False)) as c:
        r = c.post(
            "/login", data={"email": "a@b.co", "password": "secret123", "next": ""},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert up.login_calls == 0   # never hit the router


def test_register_refused_when_disabled() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up, password_login_enabled=False)) as c:
        get = c.get("/register", follow_redirects=False)
        post = c.post(
            "/register",
            data={"email": "a@b.co", "password": "hunter2hunter",
                  "confirm_password": "hunter2hunter", "display_name": ""},
            follow_redirects=False,
        )
    assert get.status_code == 303 and get.headers["location"] == "/login"
    assert post.status_code == 303 and post.headers["location"] == "/login"
    assert up.registrations == 0


def test_set_password_refused_when_disabled() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream(), password_login_enabled=False)) as c:
        r = c.post(
            "/set-password", data={"token": "t", "new_password": "longenough1"},
            follow_redirects=False,
        )
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_post_login_still_works_when_enabled() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up, password_login_enabled=True)) as c:
        r = c.post(
            "/login", data={"email": "a@b.co", "password": "secret123", "next": ""},
            follow_redirects=False,
        )
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert up.login_calls == 1
