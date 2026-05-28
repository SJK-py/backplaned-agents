"""bp_agents.agents.webapp.csrf — CSRF protection (double-submit).

Mirrors the admin BFF: a 32-byte token minted at login, stashed in the
signed session cookie, and echoed back on every state-changing request
via the `X-CSRF-Token` header (HTMX/fetch) or a `csrf_token` form field
(raw HTML form). Login is exempt — there's no session yet.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Callable
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT_PATHS = frozenset({"/login"})


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def session_token(request: Request) -> str | None:
    return request.session.get("csrf_token")


async def _received_token(request: Request) -> str | None:
    """Pull the echoed token from header or form body. The form body is
    read via `request.body()` (cached, re-readable by the handler) rather
    than `request.form()`, which would consume the stream the inner
    handler's `Form(...)` params need."""
    header = request.headers.get("X-CSRF-Token")
    if header:
        return header
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("application/x-www-form-urlencoded"):
        return None
    body = await request.body()
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    values = parsed.get("csrf_token", [])
    return values[0] if values else None


def make_csrf_middleware() -> Callable:  # type: ignore[type-arg]
    """Build the CSRF middleware. Must run AFTER SessionMiddleware (so
    `request.session` is populated) and AFTER auth (so unauth requests are
    already redirected)."""

    async def middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        method = request.method.upper()
        path = request.url.path
        if method in SAFE_METHODS or path in EXEMPT_PATHS or path.startswith("/static/"):
            return await call_next(request)

        expected = session_token(request)
        received = await _received_token(request)
        if (
            not expected
            or not received
            or not hmac.compare_digest(expected, received)
        ):
            logger.warning(
                "csrf_validation_failed",
                extra={"event": "csrf_validation_failed", "path": path,
                       "method": method},
            )
            return _forbidden(request)
        return await call_next(request)

    return middleware


def _forbidden(request: Request) -> Response:
    if request.headers.get("HX-Request", "").lower() == "true":
        return JSONResponse(
            status_code=403, content={"detail": "csrf token missing or invalid"}
        )
    return Response(
        content="Request rejected: missing or invalid CSRF token. Reload and retry.",
        status_code=403,
        media_type="text/plain",
    )
