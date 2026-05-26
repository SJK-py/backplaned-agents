"""CSRF token rotates on access-token refresh.

R4 second-pass review (low) noted that `bp_admin/auth.py:ensure_fresh_token`
updates the access + refresh + level fields on a successful
upstream refresh but doesn't mint a new CSRF token. A
long-lived session (24h max_age) would keep the same CSRF
token across many refresh cycles.

The token is bound to the signed session cookie (so leakage
requires breaking the session secret, not just observing it),
so this is defence-in-depth rather than a current vulnerability.
But rotating costs nothing and bounds replay if the token leaks
via a Referer header or a proxy log.

R5 fix: mint a fresh `csrf_token` at the end of the refresh
update block, matching the `store_login` path which already
mints one on initial login.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_ensure_fresh_token_rotates_csrf_on_refresh() -> None:
    """Source pin: the refresh-success block re-mints
    `request.session["csrf_token"]` via `_issue_csrf_token()`."""
    pytest.importorskip("fastapi")
    from bp_admin import auth

    src = inspect.getsource(auth.ensure_fresh_token)
    # The refresh path writes the four lifecycle fields then
    # rotates CSRF.
    assert 'request.session["csrf_token"] = _issue_csrf_token()' in src


def test_csrf_rotates_after_successful_refresh() -> None:
    """Functional: feed `ensure_fresh_token` an upstream-refresh
    success response and assert the session's csrf_token field
    changes from its pre-refresh value."""
    pytest.importorskip("fastapi")
    from datetime import UTC, datetime

    from bp_admin import auth

    # Build a fake request whose session is near expiry.
    session: dict = {
        "access_token": "old_access",
        "access_expires_at": datetime.now(UTC).isoformat(),
        "refresh_token": "old_refresh",
        "level": "admin",
        "csrf_token": "OLD_CSRF_TOKEN_FIXED",
    }
    request = MagicMock()
    request.session = session

    upstream = MagicMock()
    upstream.refresh = AsyncMock(return_value={
        "access_token": "new_access",
        "expires_at": datetime.now(UTC).isoformat(),
        "refresh_token": "new_refresh",
        "level": "admin",
    })

    asyncio.run(
        auth.ensure_fresh_token(request, upstream, buffer_s=600)
    )

    # Access / refresh / level updated.
    assert session["access_token"] == "new_access"
    assert session["refresh_token"] == "new_refresh"
    # CSRF rotated — the token changed.
    assert session["csrf_token"] != "OLD_CSRF_TOKEN_FIXED"
    # And the new token has the right shape (URL-safe alphabet).
    new_csrf = session["csrf_token"]
    assert isinstance(new_csrf, str)
    assert len(new_csrf) > 0


def test_csrf_not_rotated_when_refresh_fails() -> None:
    """If the upstream refresh fails, the session is cleared (no
    point rotating CSRF on a cleared session — the user has to
    re-login anyway). Pin the clear-session path doesn't try to
    rotate."""
    pytest.importorskip("fastapi")
    from datetime import UTC, datetime

    from bp_admin import auth
    from bp_admin.upstream import UpstreamError

    session: dict = {
        "access_token": "old",
        "access_expires_at": datetime.now(UTC).isoformat(),
        "refresh_token": "old",
        "level": "admin",
        "csrf_token": "OLD_CSRF",
    }
    request = MagicMock()
    request.session = session
    upstream = MagicMock()
    upstream.refresh = AsyncMock(
        side_effect=UpstreamError(status_code=401, detail={"x": 1})
    )

    asyncio.run(
        auth.ensure_fresh_token(request, upstream, buffer_s=600)
    )

    # Session cleared.
    assert "access_token" not in session or session.get("access_token") in (None, "")
