"""bp_agents.agents.webapp.auth — session cookie + auth middleware.

The webapp holds the logged-in *user's* JWT pair in a server-side signed
session cookie (Starlette's SessionMiddleware). Unlike the admin BFF this
is not admin-gated — any authenticated user may use it. The access token
is refreshed proactively before expiry so handlers don't re-implement it.

Session-cookie payload: access_token, access_expires_at, refresh_token,
user_id, email, csrf_token.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import RedirectResponse

from bp_agents.agents.webapp.config import WebappConfig
from bp_agents.agents.webapp.csrf import issue_token as _issue_csrf_token
from bp_agents.agents.webapp.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/login", "/set-password"})
"""Paths that don't require authentication. `/set-password` redeems a
one-time token (the token IS the auth, like login) — reachable before the
user has a session. The webapp is standalone (no mount prefix), so these
are compared against `request.url.path` directly. `/static/...` is also
public."""


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("access_token"))


def store_login(request: Request, *, login_response: dict, email: str) -> None:
    """Persist the upstream login response into the session cookie."""
    request.session["access_token"] = login_response["access_token"]
    request.session["access_expires_at"] = login_response["expires_at"]
    request.session["refresh_token"] = login_response["refresh_token"]
    request.session["level"] = login_response.get("level", "")
    request.session["email"] = email
    request.session["csrf_token"] = _issue_csrf_token()
    user_id = _jwt_sub(login_response["access_token"])
    if user_id:
        request.session["user_id"] = user_id


def clear_session(request: Request) -> None:
    request.session.clear()


def session_user_id(request: Request) -> str | None:
    return request.session.get("user_id")


def _jwt_sub(token: str) -> str | None:
    """Pull the `sub` claim from a JWT without verifying the signature —
    the token came from the router over our own HTTP client, so we trust
    it. Used only for scoping suite reads / display, never for authz."""
    try:
        _, payload_b64, _ = token.split(".")
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


def _expires_at(request: Request) -> datetime | None:
    iso = request.session.get("access_expires_at")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def ensure_fresh_token(
    request: Request, upstream: UpstreamClient, *, buffer_s: int
) -> None:
    """Refresh the user's access token if it's within `buffer_s` of expiry.
    A failed refresh clears the session so the next check redirects to login."""
    exp = _expires_at(request)
    if exp is None:
        return
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if (exp - datetime.now(UTC)).total_seconds() > buffer_s:
        return

    refresh_token = request.session.get("refresh_token")
    if not refresh_token:
        clear_session(request)
        return
    try:
        body = await upstream.refresh(refresh_token=refresh_token)
    except UpstreamError as exc:
        logger.warning(
            "webapp_session_refresh_failed",
            extra={"event": "webapp_session_refresh_failed",
                   "status_code": exc.status_code},
        )
        clear_session(request)
        return
    request.session["access_token"] = body["access_token"]
    request.session["access_expires_at"] = body["expires_at"]
    request.session["refresh_token"] = body["refresh_token"]
    request.session["level"] = body.get("level", "")
    # NOTE: do NOT rotate the CSRF token here. This refresh runs in the auth
    # middleware BEFORE call_next reaches the (inner) CSRF middleware, which
    # validates the client's submitted token against session["csrf_token"].
    # Rotating it mid-request invalidated the token the browser already held,
    # so every write that happened to land in a refresh window (a recurring
    # ~buffer_s window per token lifetime) failed with a spurious 403 until
    # the user reloaded. The CSRF token is minted at login and lives in the
    # signed session cookie for the browser session — a double-submit token
    # in a tamper-proof cookie gains negligible security from per-refresh
    # rotation, and not rotating it removes the race entirely.


def make_auth_middleware(config: WebappConfig, upstream: UpstreamClient):  # type: ignore[no-untyped-def]
    """Build the auth middleware. Add AFTER SessionMiddleware so
    `request.session` is populated."""

    async def middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if not is_authenticated(request):
            return _redirect_to_login(path)
        await ensure_fresh_token(request, upstream, buffer_s=config.refresh_buffer_s)
        if not is_authenticated(request):
            return _redirect_to_login(path)
        return await call_next(request)

    return middleware


def _redirect_to_login(return_to: str) -> RedirectResponse:
    from urllib.parse import quote  # noqa: PLC0415

    return RedirectResponse(url=f"/login?next={quote(return_to)}", status_code=303)
