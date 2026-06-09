"""Admin webUI — the logged-in admin's own "Change password" page.

`/account/password` (GET form, POST submit) calls the router's authenticated
`POST /v1/auth/change-password`. Distinct from the per-user password-RESET
token an admin mints for OTHER users. On success the router revokes the
caller's tokens, so the BFF session is cleared and the admin is sent to
`{root_path}/login?changed=1`; mismatch / short / upstream errors bounce back
to the form with a `?error=<code>`. Redirects are mount-aware (`root_path`).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_admin.pages.account import _ERRORS, password_form, password_submit
from bp_admin.upstream import UpstreamError

_TPL = Path(__file__).parent.parent / "bp_admin" / "templates"


def _req(*, upstream: object, root: str = "", access_token: str = "tok") -> MagicMock:
    req = MagicMock()
    req.scope = {"root_path": root}
    req.session = {"access_token": access_token, "csrf_token": "c"}
    req.app.state.upstream = upstream
    return req


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_success_clears_session_and_redirects_to_login() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up)
    resp = _run(password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="newpass12",
    ))
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?changed=1"
    up.change_password.assert_awaited_once_with(
        access_token="tok", current_password="oldpass12", new_password="newpass12",
    )
    assert "access_token" not in req.session  # session cleared → re-login


def test_success_redirect_is_mount_aware() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up, root="/admin")
    resp = _run(password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="newpass12",
    ))
    assert resp.headers["location"] == "/admin/login?changed=1"


def test_mismatch_short_circuits_without_calling_router() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up, root="/admin")
    resp = _run(password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="nope12345",
    ))
    assert resp.headers["location"] == "/admin/account/password?error=mismatch"
    up.change_password.assert_not_awaited()
    assert req.session.get("access_token") == "tok"  # still signed in


def test_short_password_short_circuits() -> None:
    up = SimpleNamespace(change_password=AsyncMock())
    req = _req(upstream=up)
    resp = _run(password_submit(
        req, current_password="oldpass12", new_password="short",
        confirm_password="short",
    ))
    assert resp.headers["location"] == "/account/password?error=weak"
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
    resp = _run(password_submit(
        req, current_password="oldpass12", new_password="newpass12",
        confirm_password="newpass12",
    ))
    assert resp.headers["location"] == f"/account/password?error={code}"
    assert req.session.get("access_token") == "tok"  # failed → not logged out


def test_form_renders_mapped_error_message() -> None:
    req = MagicMock()
    req.app.state.templates = MagicMock()
    _run(password_form(req, error="current"))
    _args, kwargs = req.app.state.templates.TemplateResponse.call_args
    # context is the 3rd positional arg: (request, template, context)
    ctx = _args[2]
    assert ctx["error"] == _ERRORS["current"]


def test_unknown_error_code_renders_no_message() -> None:
    req = MagicMock()
    req.app.state.templates = MagicMock()
    _run(password_form(req, error="bogus"))
    ctx = req.app.state.templates.TemplateResponse.call_args[0][2]
    assert ctx["error"] is None


# --- template / wiring pins ------------------------------------------------


def test_password_template_posts_to_account_route_with_csrf() -> None:
    body = (_TPL / "account" / "password.html").read_text()
    assert 'action="/admin/account/password"' in body
    assert '{% include "_partials/csrf.html" %}' in body
    for field in ("current_password", "new_password", "confirm_password"):
        assert f'name="{field}"' in body


def test_nav_links_to_change_password() -> None:
    assert 'href="/admin/account/password"' in (_TPL / "base.html").read_text()


def test_login_template_has_changed_banner() -> None:
    body = (_TPL / "login.html").read_text()
    assert "changed_ok" in body
    assert "Password changed" in body


def test_account_router_registered_under_prefix() -> None:
    src = inspect.getsource(__import__("bp_admin.app", fromlist=["create_app"]).create_app)
    assert 'account.router, prefix="/account"' in src
