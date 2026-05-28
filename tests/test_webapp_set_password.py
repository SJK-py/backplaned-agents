"""Webapp — redeem a `/password` one-time token from the login page.

The bot's `/password` mints a token (router F9); the login page now has a
"set password" form that POSTs to /set-password, which consumes the token
via `POST /v1/auth/reset-password`. On success the user signs in below
with their email + the new password. Pre-auth + token-authenticated, so
the path is public + CSRF-exempt (like /login).
"""

from __future__ import annotations

import pytest


class _Upstream:
    """Fake router client: reset_password mirrors the F9 redeem contract."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    async def reset_password(self, *, token: str, new_password: str) -> dict:
        self.calls.append((token, new_password))
        if self.error is not None:
            raise self.error
        return {
            "access_token": "a", "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

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


def test_login_page_offers_set_password_form() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream())) as c:
        html = c.get("/login").text
    assert 'action="/set-password"' in html
    assert "One-time token" in html
    # No CSRF field needed — the form is pre-auth (token IS the auth).


def test_set_password_redeems_token_then_redirects_to_login() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.post(
            "/set-password",
            data={"token": "tok-123", "new_password": "hunter2hunter2"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers.get("location") == "/login?reset=1"
    assert up.calls == [("tok-123", "hunter2hunter2")]


def test_set_password_is_public_and_csrf_exempt() -> None:
    """No session/cookie + no CSRF token, yet the POST is accepted (not
    302→login from the auth gate, not 403 from CSRF)."""
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.post(
            "/set-password",
            data={"token": "t", "new_password": "longenough1"},
            follow_redirects=False,
        )
    assert r.status_code == 303  # neither 403 (CSRF) nor a login redirect loop
    assert up.calls  # reached the handler → upstream called


def test_set_password_rejects_short_password_without_calling_router() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.post(
            "/set-password",
            data={"token": "t", "new_password": "short"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers.get("location") == "/login?reset_error=weak"
    assert up.calls == []  # short-circuited before the router call


def test_set_password_maps_invalid_token_to_friendly_error() -> None:
    pytest.importorskip("fastapi")
    from bp_agents.agents.webapp.upstream import UpstreamError  # noqa: PLC0415

    up = _Upstream()
    up.error = UpstreamError(401, "invalid or expired token")
    with _client(_build_app(up)) as c:
        r = c.post(
            "/set-password",
            data={"token": "bad", "new_password": "longenough1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers.get("location") == "/login?reset_error=invalid"
        # The login page renders the friendly message + opens the reset form.
        page = c.get("/login?reset_error=invalid").text
    assert "invalid or expired" in page
    assert "mode: 'reset'" in page  # reset form is shown so the error is visible


def test_set_password_maps_rate_limit_and_conflict() -> None:
    pytest.importorskip("fastapi")
    from bp_agents.agents.webapp.upstream import UpstreamError  # noqa: PLC0415

    for status, code in ((429, "ratelimited"), (409, "conflict"), (500, "failed")):
        up = _Upstream()
        up.error = UpstreamError(status, "x")
        with _client(_build_app(up)) as c:
            r = c.post(
                "/set-password",
                data={"token": "t", "new_password": "longenough1"},
                follow_redirects=False,
            )
        assert r.headers.get("location") == f"/login?reset_error={code}"


def test_login_shows_success_banner_after_reset() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream())) as c:
        html = c.get("/login?reset=1").text
    assert "Password set" in html
