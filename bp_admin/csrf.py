"""bp_admin.csrf — CSRF protection for the admin BFF.

Double-submit pattern bound to the session cookie:

  - On login, mint a 32-byte random token and stash in
    `request.session["csrf_token"]`. The cookie is signed by Starlette,
    so the token isn't tamperable client-side.
  - Every state-changing request (POST/PUT/PATCH/DELETE) must echo the
    token back, either as the `X-CSRF-Token` header (HTMX/fetch path)
    or as a `csrf_token` form field (raw HTML form path). The middleware
    compares the echoed value to the session copy in constant time.
  - Login itself is exempt — there's no session yet.
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

# Paths under the admin mount that don't carry a CSRF token.
# Login can't — there's no session before the user authenticates.
EXEMPT_PATHS = frozenset({"/login"})


def issue_token() -> str:
    """Generate a fresh CSRF token (URL-safe; ~43 chars)."""
    return secrets.token_urlsafe(32)


def session_token(request: Request) -> str | None:
    return request.session.get("csrf_token")


async def _received_token(request: Request) -> str | None:
    """Pull the echoed CSRF token from header or form body.

    Header check runs first (HTMX/fetch path). For the form path we
    parse the cached raw body rather than `request.form()`: under
    Starlette >= 1.0 with FastAPI's `BaseHTTPMiddleware`, calling
    `request.form()` from middleware consumes the body in a way the
    inner handler's later `Form(...)` parameters can't see, and they
    then report `missing`. Reading the body via `request.body()`
    (cached on `_body`) is safe to re-read in the handler.

    Only `application/x-www-form-urlencoded` is supported; admin forms
    don't post multipart, and parsing it would require a manual
    multipart parser since we can't rely on `request.form()` either.
    """
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
    """Build the CSRF middleware. Must run AFTER SessionMiddleware so
    `request.session` is populated, and AFTER the auth middleware so
    unauth requests are already redirected away.

    Path comparisons are mount-aware via `strip_root_path` so the
    same `EXEMPT_PATHS = {"/login"}` works in both standalone and
    mounted-under-`/admin` deployments. Without this, POST
    `/admin/login` was rejected with 403 `csrf_validation_failed`
    because `/admin/login` ∉ `{"/login"}`.
    """
    from bp_admin.asgi_utils import strip_root_path  # noqa: PLC0415

    async def middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        method = request.method.upper()
        path = strip_root_path(request)

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
                extra={
                    "event": "csrf_validation_failed",
                    "path": path,
                    "method": method,
                    "had_session_token": bool(expected),
                    "had_received_token": bool(received),
                },
            )
            return _forbidden(request)

        return await call_next(request)

    return middleware


def _forbidden(request: Request) -> Response:
    """Render a small 403 page (or JSON for HTMX requests)."""
    if request.headers.get("HX-Request", "").lower() == "true":
        return JSONResponse(
            status_code=403,
            content={"detail": "csrf token missing or invalid"},
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "upstream_error.html",
        {
            "active_section": None,
            "status_code": 403,
            "message": (
                "Request rejected: missing or invalid CSRF token. "
                "Reload the page and try again."
            ),
        },
        status_code=403,
    )
