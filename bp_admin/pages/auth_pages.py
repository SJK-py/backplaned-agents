"""bp_admin.pages.auth_pages — login and logout routes (UI side).

These are the BFF endpoints the login form posts to, NOT the upstream
`/v1/auth/login` endpoint. They wrap the upstream call and translate
to session-cookie state.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_admin.auth import (
    clear_session,
    is_authenticated,
    store_login,
)
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(request: Request, raw: str | None) -> str:
    """Sanitise the `next` redirect target.

    Mount-aware: the redirect targets the
    request's ASGI `root_path`, so the same code works for the
    standalone `bp-admin` deployment (no prefix) and the
    mounted-under-`/admin` deployment. Hard-coding `/admin/`
    here used to break standalone mode (404 on the redirect).

    Rejects:
      - Absolute URLs (scheme / netloc set) — open-redirect
        protection.
      - Paths outside this app's mount (so an attacker can't
        bounce a logged-in admin to an attacker-controlled URL
        in the parent app's namespace).
      - The login page itself — bouncing through login back to
        login is a bad UX.

    Defaults to the app's root (`/` standalone, `/admin/`
    mounted).
    """
    from bp_admin.asgi_utils import root_path as _root_path  # noqa: PLC0415

    rp = _root_path(request)
    safe_default = f"{rp}/" if rp else "/"
    if not raw:
        return safe_default
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return safe_default
    # Path must be inside this app's mount. For standalone
    # (`rp=""`) every relative path qualifies.
    in_app_prefix = rp + "/" if rp else "/"
    if not parsed.path.startswith(in_app_prefix):
        return safe_default
    # Reject the login page itself.
    login_path = f"{rp}/login"
    if parsed.path == login_path or parsed.path.startswith(login_path + "/"):
        return safe_default
    return raw


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    from bp_admin.asgi_utils import root_path as _root_path  # noqa: PLC0415

    if is_authenticated(request):
        rp = _root_path(request)
        return RedirectResponse(url=f"{rp}/" if rp else "/", status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "next": request.query_params.get("next", ""),
        },
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
        # Common case: 401 invalid credentials. Don't reveal which
        # field was wrong — same message for missing user, wrong
        # password, suspended user.
        #
        # Email is deliberately NOT included in this log line
        # The router-side `auth.login_failed`
        # audit row already captures the email scoped to the
        # admin-only audit table; duplicating it here puts PII
        # into the BFF log stream where access control is broader
        # (Loki / Cloud Logging shipping → log indexers see it).
        # An attacker who probes admin login with a username list
        # would also seed the log index with those queries — log
        # injection / poisoning at scale.
        logger.info(
            "admin_login_failed",
            extra={
                "event": "admin_login_failed",
                "status_code": exc.status_code,
            },
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Invalid credentials.",
                "next": next,
                "email": email,
            },
            status_code=401,
        )

    if body.get("level") != "admin":
        # Authenticated but not an admin — no session for this UI.
        # Don't disclose that the account exists with non-admin level.
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Invalid credentials.",
                "next": next,
                "email": email,
            },
            status_code=403,
        )

    store_login(request, login_response=body, email=email)
    return RedirectResponse(url=_safe_next(request, next), status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    upstream = request.app.state.upstream
    access = request.session.get("access_token")
    refresh = request.session.get("refresh_token")
    if access:
        try:
            await upstream.logout(access_token=access, refresh_token=refresh)
        except UpstreamError as exc:
            # Logout is best-effort — clear the local session even if
            # the upstream call fails.
            logger.warning(
                "admin_logout_upstream_failed",
                extra={
                    "event": "admin_logout_upstream_failed",
                    "status_code": exc.status_code,
                },
            )
    clear_session(request)
    # Mount-aware redirect — same pattern as the GET handlers above
    # use via `_login_url`. Standalone `bp-admin` deployments serve
    # the UI from root (`/login`); mounted-under-router deployments
    # serve from `/admin/login`. Hardcoding the `/admin/login` path
    # breaks the standalone case with a 404 right after logout.
    # R4 second-pass review.
    from bp_admin.asgi_utils import root_path as _root_path  # noqa: PLC0415
    rp = _root_path(request)
    return RedirectResponse(url=f"{rp}/login", status_code=303)
