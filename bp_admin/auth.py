"""bp_admin.auth — Session management and BFF auth middleware.

The admin UI uses a server-side signed session cookie (Starlette's
SessionMiddleware) to hold the upstream JWT pair. Every page-handler
reads tokens from `request.session`; this module proactively refreshes
the access token when it's near expiry so handlers don't have to
re-implement that logic.

Session-cookie payload:

    {
        "access_token":           str,
        "access_expires_at":      iso8601 str,
        "refresh_token":          str,
        "user_id":                str,
        "level":                  str,                # always "admin" here
        "email":                  str,
    }
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import RedirectResponse

from bp_admin.config import AdminConfig
from bp_admin.csrf import issue_token as _issue_csrf_token
from bp_admin.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def is_authenticated(request: Request) -> bool:
    sess = request.session
    return bool(sess.get("access_token") and sess.get("level") == "admin")


def store_login(request: Request, *, login_response: dict, email: str) -> None:
    """Persist the upstream login response into the session cookie.

    `login_response` is the body returned by `POST /v1/auth/login`
    plus an `email` we add ourselves (router doesn't echo it)."""
    request.session["access_token"] = login_response["access_token"]
    request.session["access_expires_at"] = login_response["expires_at"]
    request.session["refresh_token"] = login_response["refresh_token"]
    request.session["level"] = login_response["level"]
    request.session["email"] = email
    # Mint a fresh CSRF token bound to this session — see bp_admin.csrf.
    request.session["csrf_token"] = _issue_csrf_token()
    # Pull user_id from the access token's subject claim. The router
    # signed it; we don't re-verify (the BFF trusts upstream output).
    # Stored only for display; the upstream calls authenticate by JWT.
    user_id = _jwt_sub(login_response["access_token"])
    if user_id:
        request.session["user_id"] = user_id


def clear_session(request: Request) -> None:
    request.session.clear()


def session_user_email(request: Request) -> str | None:
    return request.session.get("email")


def _jwt_sub(token: str) -> str | None:
    """Pull the `sub` claim from a JWT without verifying the signature.

    The token came from upstream over the BFF's HTTP client, so we
    trust the upstream signed it. Used only for display (sidebar / audit
    deep-links); never for authorization decisions.
    """
    try:
        _, payload_b64, _ = token.split(".")
        # JWT payloads use URL-safe base64 without padding.
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


# ---------------------------------------------------------------------------
# Token-refresh shim
# ---------------------------------------------------------------------------


def _expires_at(request: Request) -> datetime | None:
    iso = request.session.get("access_expires_at")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except Exception:  # noqa: BLE001
        return None


async def ensure_fresh_token(
    request: Request, upstream: UpstreamClient, *, buffer_s: int
) -> None:
    """Proactively refresh the upstream access token if it's near expiry.

    Called by the auth middleware before every protected request. If the
    refresh upstream-call fails (e.g. router unreachable), the session
    is cleared and the next middleware iteration will redirect to login.
    """
    exp = _expires_at(request)
    if exp is None:
        return
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    remaining_s = (exp - datetime.now(UTC)).total_seconds()
    if remaining_s > buffer_s:
        return

    refresh_token = request.session.get("refresh_token")
    if not refresh_token:
        clear_session(request)
        return

    try:
        body = await upstream.refresh(refresh_token=refresh_token)
    except UpstreamError as exc:
        logger.warning(
            "admin_session_refresh_failed",
            extra={
                "event": "admin_session_refresh_failed",
                "status_code": exc.status_code,
            },
        )
        clear_session(request)
        return

    request.session["access_token"] = body["access_token"]
    request.session["access_expires_at"] = body["expires_at"]
    request.session["refresh_token"] = body["refresh_token"]
    request.session["level"] = body["level"]
    # NOTE: do NOT rotate the CSRF token here. This refresh runs in the auth
    # middleware BEFORE call_next reaches the (inner) CSRF middleware, which
    # validates the client's submitted token against session["csrf_token"]
    # (stack order: Session → Auth → CSRF → handler). Rotating it mid-request
    # invalidated the token the browser already held, so every state-changing
    # admin action that happened to land in a refresh window (a recurring
    # ~buffer_s window per token lifetime) failed with a spurious 403 until
    # the operator reloaded. The token is minted at login and lives in the
    # signed session cookie for the browser session — a double-submit token
    # in a tamper-proof cookie gains negligible security from per-refresh
    # rotation, and not rotating it removes the race entirely. (Mirrors the
    # identical fix in the user webapp BFF; second-pass review.)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


PUBLIC_PATHS = frozenset({"/login"})
"""In-app paths that do not require authentication. Compared
against the path AFTER the mount prefix has been stripped, so the
same set works for both standalone (`/login`) and mounted-under
(`/admin/login`, `/foo/bar/login`, …) deployments
Static files served from `/static/...` are also public."""


def make_auth_middleware(config: AdminConfig, upstream: UpstreamClient):  # type: ignore[no-untyped-def]
    """Build the BFF auth middleware. Must be added with
    `app.middleware("http")` AFTER SessionMiddleware (so `request.session`
    is available)."""
    from bp_admin.asgi_utils import strip_root_path  # noqa: PLC0415

    async def middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = strip_root_path(request)

        # Public surfaces — login and static assets.
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        if not is_authenticated(request):
            return RedirectResponse(
                url=_login_url(request, return_to=path),
                status_code=303,
            )

        await ensure_fresh_token(
            request, upstream, buffer_s=config.refresh_buffer_s
        )
        if not is_authenticated(request):
            return RedirectResponse(
                url=_login_url(request, return_to=path),
                status_code=303,
            )

        return await call_next(request)

    return middleware


def _login_url(request: Request, *, return_to: str) -> str:
    """Build the redirect-to-login URL preserving where the user
    was going. Mount-aware: prefixes the URL with the ASGI
    `root_path` so the redirect points at the correct mounted
    location (`/admin/login` when mounted under `/admin`,
    `/login` when running standalone via the `bp-admin`
    console-script). Hard-coding `/admin/login` here would
    break standalone mode with a 404.
    """
    from urllib.parse import quote  # noqa: PLC0415

    from bp_admin.asgi_utils import root_path as _root_path  # noqa: PLC0415

    return f"{_root_path(request)}/login?next={quote(return_to)}"
