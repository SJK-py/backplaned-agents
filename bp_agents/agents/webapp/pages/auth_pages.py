"""bp_agents.agents.webapp.pages.auth_pages — login / logout (UI side).

These wrap the router's `/v1/auth/*` endpoints and translate to
session-cookie state. Any authenticated user may log in (not
admin-gated, unlike the admin BFF).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_agents.agents.webapp.auth import (
    clear_session,
    is_authenticated,
    store_login,
)
from bp_agents.agents.webapp.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(raw: str | None) -> str:
    """Sanitise the post-login redirect target: reject absolute URLs
    (open-redirect) and the login page itself; default to `/`."""
    if not raw:
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    if parsed.path == "/login" or parsed.path.startswith("/login/"):
        return "/"
    return raw


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next": request.query_params.get("next", "")},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
) -> HTMLResponse:
    upstream = request.app.state.upstream
    templates = request.app.state.templates
    try:
        body = await upstream.login(email=email, password=password)
    except UpstreamError as exc:
        # Don't reveal which field was wrong; don't log the email (PII).
        logger.info(
            "webapp_login_failed",
            extra={"event": "webapp_login_failed", "status_code": exc.status_code},
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials.", "next": next, "email": email},
            status_code=401,
        )
    store_login(request, login_response=body, email=email)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    upstream = request.app.state.upstream
    access = request.session.get("access_token")
    refresh = request.session.get("refresh_token")
    if access:
        try:
            await upstream.logout(access_token=access, refresh_token=refresh)
        except UpstreamError as exc:
            logger.warning(
                "webapp_logout_upstream_failed",
                extra={"event": "webapp_logout_upstream_failed",
                       "status_code": exc.status_code},
            )
    clear_session(request)
    return RedirectResponse(url="/login", status_code=303)
