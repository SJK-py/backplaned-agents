"""bp_admin._helpers — shared utilities for the page-handler modules.

Until phase 6 each page module had its own copies of these. They were
identical; lifted here so a behaviour change happens in one place.

  - `is_htmx`           — HX-Request: true detector.
  - `access_token`      — pulls the upstream JWT from the session.
  - `upstream`          — request → app.state.upstream shortcut.
  - `detail_message`    — flatten an UpstreamError detail to a string.
  - `set_flash`         — stash a one-shot flash in the session.
  - `pop_flash`         — read + clear the session flash.
  - `redirect_with_flash` — set flash + 303 to a bare URL.
  - `error_response`    — render `_partials/upstream_error.html` or
                          `upstream_error.html` for a non-2xx upstream.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_admin.upstream import UpstreamClient, UpstreamError


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def access_token(request: Request) -> str:
    """Read the upstream access token from the session, raising 401 if
    missing. The auth middleware should have redirected already; this
    is a defensive check for handlers that bypass the middleware order."""
    token = request.session.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    return token


def upstream(request: Request) -> UpstreamClient:
    return request.app.state.upstream


def detail_message(exc: UpstreamError) -> str:
    """Flatten an UpstreamError's detail into a human-readable string.

    The router returns either a plain string in `detail` or a structured
    `{code, message}` dict (for AdmitError-shaped 4xx responses)."""
    detail = exc.detail
    if isinstance(detail, dict):
        return detail.get("message") or detail.get("detail") or str(detail)
    return str(detail)


# ---------------------------------------------------------------------------
# Flash messages — one-shot session value
# ---------------------------------------------------------------------------


_FLASH_KEY = "_flash"


def set_flash(request: Request, message: str) -> None:
    request.session[_FLASH_KEY] = message


def pop_flash(request: Request) -> str | None:
    """Read and clear the session flash. Returns None if unset."""
    return request.session.pop(_FLASH_KEY, None)


def redirect_with_flash(
    request: Request, url: str, message: str, *, status_code: int = 303
) -> RedirectResponse:
    """Stash `message` in the session and 303 to `url`.

    Replaces the older `?flash=...` URL pattern: flash messages are
    now one-shot session values, so they don't survive in browser
    history, bookmarks, or shared links.
    """
    set_flash(request, message)
    return RedirectResponse(url=url, status_code=status_code)


# ---------------------------------------------------------------------------
# Error response — surface upstream errors with a consistent shape
# ---------------------------------------------------------------------------


def error_response(
    request: Request,
    exc: UpstreamError,
    *,
    partial: bool,
    active_section: str | None = None,
) -> HTMLResponse:
    templates = request.app.state.templates
    template = "_partials/upstream_error.html" if partial else "upstream_error.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_section": active_section,
            "status_code": exc.status_code,
            "message": detail_message(exc),
        },
        status_code=exc.status_code,
    )
