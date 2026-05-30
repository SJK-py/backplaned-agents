"""Admin BFF: a proactive token refresh must NOT invalidate the CSRF token.

Second-pass blocker (parity with the webapp fix #70): the admin auth
middleware runs `ensure_fresh_token` BEFORE `call_next` reaches the (inner)
CSRF middleware (stack order: Session → Auth → CSRF → handler), and it rotated
`session["csrf_token"]` on every refresh. The CSRF middleware then validated
the operator's already-held (pre-rotation) token against the freshly-rotated
session token → spurious 403 on any state-changing admin action that happened
to land in a refresh window (a recurring ~buffer_s window per token lifetime).
The operator had to reload to recover.

A prior review round (R4/R5) had ADDED that rotation as defence-in-depth, and
this file originally asserted it. The rotation is a net loss: a double-submit
token in a tamper-proof signed session cookie gains negligible security from
per-refresh rotation, and rotating it mid-request is the bug. Fix:
`ensure_fresh_token` no longer rotates — the token is minted at login and
lives for the browser session.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _session(*, seconds_to_expiry: int) -> dict[str, Any]:
    return {
        "access_token": "OLD.access.tok",
        "access_expires_at": (
            datetime.now(UTC) + timedelta(seconds=seconds_to_expiry)
        ).isoformat(),
        "refresh_token": "OLD.refresh.tok",
        "csrf_token": "csrf-ADMIN-T1",
        "level": "admin",
    }


def _upstream_ok() -> MagicMock:
    up = MagicMock()
    up.refresh = AsyncMock(return_value={
        "access_token": "NEW.access.tok",
        "refresh_token": "NEW.refresh.tok",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "level": "admin",
    })
    return up


def test_refresh_preserves_csrf_token() -> None:
    """Token within the buffer → refresh runs and updates the access token,
    but the CSRF token the browser holds MUST survive."""
    pytest.importorskip("fastapi")
    from bp_admin.auth import ensure_fresh_token

    up = _upstream_ok()
    req = MagicMock()
    req.session = _session(seconds_to_expiry=10)  # inside the 600s buffer

    _run(ensure_fresh_token(req, up, buffer_s=600))

    up.refresh.assert_awaited_once()
    assert req.session["access_token"] == "NEW.access.tok"
    assert req.session["refresh_token"] == "NEW.refresh.tok"
    assert req.session["csrf_token"] == "csrf-ADMIN-T1", (
        "CSRF token must NOT change on refresh — rotating it mid-request 403s "
        "the admin action the browser is making with its existing token"
    )


def test_no_refresh_when_not_near_expiry() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.auth import ensure_fresh_token

    up = _upstream_ok()
    req = MagicMock()
    req.session = _session(seconds_to_expiry=3600)  # well outside the buffer

    _run(ensure_fresh_token(req, up, buffer_s=600))

    up.refresh.assert_not_awaited()
    assert req.session["access_token"] == "OLD.access.tok"
    assert req.session["csrf_token"] == "csrf-ADMIN-T1"


def test_csrf_not_rotated_when_refresh_fails() -> None:
    """A failed upstream refresh clears the session (the operator re-logins);
    the failure path must not rotate either."""
    pytest.importorskip("fastapi")
    from bp_admin.auth import ensure_fresh_token
    from bp_admin.upstream import UpstreamError

    req = MagicMock()
    req.session = _session(seconds_to_expiry=10)
    up = MagicMock()
    up.refresh = AsyncMock(
        side_effect=UpstreamError(status_code=401, detail={"x": 1})
    )

    _run(ensure_fresh_token(req, up, buffer_s=600))

    # Session cleared (no valid access token remains).
    assert req.session.get("access_token") in (None, "")


def test_ensure_fresh_token_does_not_rotate_csrf_source() -> None:
    """Source pin: the refresh path must not mint a CSRF token."""
    pytest.importorskip("fastapi")
    from bp_admin import auth

    src = inspect.getsource(auth.ensure_fresh_token)
    assert "_issue_csrf_token" not in src


def test_login_still_mints_csrf() -> None:
    """The CSRF token is still minted at login (the meaningful boundary)."""
    pytest.importorskip("fastapi")
    from bp_admin import auth

    src = inspect.getsource(auth.store_login)
    assert 'request.session["csrf_token"] = _issue_csrf_token()' in src
