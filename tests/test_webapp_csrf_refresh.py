"""Webapp: a proactive token refresh must NOT invalidate the CSRF token.

Pre-release blocker: the auth middleware runs `ensure_fresh_token` BEFORE
`call_next` reaches the (inner) CSRF middleware, and it rotated
`session["csrf_token"]` on every refresh. The CSRF middleware then validated
the client's already-held (pre-rotation) token against the freshly-rotated
session token → spurious 403 on any state-changing request that happened to
land in a refresh window (a recurring ~refresh_buffer_s window per token
lifetime). The user had to reload to recover.

Fix: `ensure_fresh_token` no longer rotates the CSRF token. It is minted at
login and lives in the signed session cookie for the browser session.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any


class _Req:
    """Minimal stand-in for `Request` — `ensure_fresh_token` only touches
    `.session` (a dict)."""

    def __init__(self, session: dict[str, Any]) -> None:
        self.session = session


class _Upstream:
    def __init__(self) -> None:
        self.refresh_calls = 0

    async def refresh(self, *, refresh_token: str) -> dict:
        self.refresh_calls += 1
        return {
            "access_token": "NEW.access.tok",
            "refresh_token": "NEW.refresh.tok",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "level": "tier1",
        }


def _session(*, seconds_to_expiry: int) -> dict[str, Any]:
    return {
        "access_token": "OLD.access.tok",
        "access_expires_at": (
            datetime.now(UTC) + timedelta(seconds=seconds_to_expiry)
        ).isoformat(),
        "refresh_token": "OLD.refresh.tok",
        "csrf_token": "csrf-T1",
        "level": "tier1",
    }


def _run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


def test_refresh_preserves_csrf_token() -> None:
    """The token is within the buffer → refresh runs and updates the access
    token, but the CSRF token the browser holds MUST survive."""
    from bp_agents.agents.webapp.auth import ensure_fresh_token

    up = _Upstream()
    req = _Req(_session(seconds_to_expiry=10))  # inside the 30s buffer
    _run(ensure_fresh_token(req, up, buffer_s=30))

    assert up.refresh_calls == 1, "refresh should have run (token near expiry)"
    assert req.session["access_token"] == "NEW.access.tok", "access token refreshed"
    assert req.session["csrf_token"] == "csrf-T1", (
        "CSRF token must NOT change on refresh — rotating it mid-request 403s "
        "the write the browser is making with its existing token"
    )


def test_no_refresh_when_not_near_expiry() -> None:
    from bp_agents.agents.webapp.auth import ensure_fresh_token

    up = _Upstream()
    req = _Req(_session(seconds_to_expiry=3600))  # well outside the buffer
    _run(ensure_fresh_token(req, up, buffer_s=30))

    assert up.refresh_calls == 0
    assert req.session["access_token"] == "OLD.access.tok"
    assert req.session["csrf_token"] == "csrf-T1"


def test_ensure_fresh_token_does_not_rotate_csrf_source() -> None:
    """Source pin: the refresh path must not assign session['csrf_token']."""
    from bp_agents.agents.webapp import auth

    src = inspect.getsource(auth.ensure_fresh_token)
    # Rotation requires calling the minter; its absence proves no rotation.
    # (The explanatory comment references the session key, so we pin on the
    # actual call, not the substring.)
    assert "_issue_csrf_token" not in src


def test_login_still_mints_csrf() -> None:
    """The CSRF token is still minted at login (the meaningful boundary)."""
    from bp_agents.agents.webapp import auth

    src = inspect.getsource(auth.store_login)
    assert 'request.session["csrf_token"] = _issue_csrf_token()' in src
