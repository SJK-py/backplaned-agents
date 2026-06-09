"""Webapp — in-session "Change password" (current → new) on Settings.

The Settings page posts to `/change-password`, which calls the router's
authenticated `POST /v1/auth/change-password`. On success the router revokes
this session's tokens, so the handler clears the local session and sends the
user to `/login?changed=1`. Client-side mismatch/short-password checks and
upstream error codes bounce back to `/config?pw_error=<code>` (the Settings
page renders the matching message).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bp_agents.agents.webapp.pages.auth_pages import change_password_submit
from bp_agents.agents.webapp.upstream import UpstreamError

_TPL = Path(__file__).parent.parent / "bp_agents" / "agents" / "webapp" / "templates"


def _req(*, upstream: object, access_token: str | None = "tok") -> SimpleNamespace:
    session: dict[str, object] = {"csrf_token": "c", "user_id": "u"}
    if access_token is not None:
        session["access_token"] = access_token
    return SimpleNamespace(
        session=session,
        app=SimpleNamespace(state=SimpleNamespace(upstream=upstream)),
    )


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_success_clears_session_and_redirects_to_login() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up)
    resp = _run(change_password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="newpass12",
    ))
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?changed=1"
    up.change_password.assert_awaited_once_with(
        access_token="tok", current_password="oldpass12", new_password="newpass12",
    )
    assert req.session == {}  # tokens revoked router-side → forced re-login


def test_mismatch_short_circuits_without_calling_router() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up)
    resp = _run(change_password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="different12",
    ))
    assert resp.headers["location"] == "/config?pw_error=mismatch"
    up.change_password.assert_not_awaited()
    assert req.session.get("access_token") == "tok"  # still signed in


def test_short_password_short_circuits_without_calling_router() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up)
    resp = _run(change_password_submit(
        req, current_password="oldpass12", new_password="short",
        confirm_password="short",
    ))
    assert resp.headers["location"] == "/config?pw_error=weak"
    up.change_password.assert_not_awaited()


def test_lapsed_session_redirects_to_login() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up, access_token=None)
    resp = _run(change_password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="newpass12",
    ))
    assert resp.headers["location"] == "/login"
    up.change_password.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "current"), (400, "same"), (409, "conflict"),
     (422, "weak"), (429, "ratelimited"), (500, "failed")],
)
def test_maps_upstream_errors_and_stays_signed_in(status: int, code: str) -> None:
    up = SimpleNamespace(
        change_password=AsyncMock(side_effect=UpstreamError(status, "x"))
    )
    req = _req(upstream=up)
    resp = _run(change_password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="newpass12",
    ))
    assert resp.headers["location"] == f"/config?pw_error={code}"
    assert req.session.get("access_token") == "tok"  # change failed → not logged out


def test_route_is_auth_gated_and_csrf_protected() -> None:
    """Unlike /set-password (pre-auth, token-authed), changing a password in
    session requires both a session and a CSRF token."""
    from bp_agents.agents.webapp.auth import PUBLIC_PATHS
    from bp_agents.agents.webapp.csrf import EXEMPT_PATHS
    assert "/change-password" not in PUBLIC_PATHS
    assert "/change-password" not in EXEMPT_PATHS


# --- template pins ---------------------------------------------------------


def test_settings_page_has_change_password_form() -> None:
    body = (_TPL / "config" / "form.html").read_text()
    assert 'action="/change-password"' in body
    assert '{% include "_partials/csrf.html" %}' in body
    for field in ("current_password", "new_password", "confirm_password"):
        assert f'name="{field}"' in body


def test_login_template_has_changed_banner() -> None:
    body = (_TPL / "login.html").read_text()
    assert "changed_ok" in body
    assert "Password changed" in body
