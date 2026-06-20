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

# Friendly messages for a failed /set-password redeem, keyed by a code we
# put on the redirect (never reflect the router's raw error text).
_RESET_ERRORS = {
    "invalid": "That token is invalid or expired. Send /password to the bot for a new one.",
    "ratelimited": "Too many attempts — please wait a moment and try again.",
    "conflict": "This account can’t use a password login.",
    "weak": "Choose a password of at least 8 characters.",
    "failed": "Couldn’t set your password. Please try again.",
}

# Friendly messages for a failed /register submit, keyed by a code on the
# redirect (never reflect the router's raw error text).
_REGISTER_ERRORS = {
    "mismatch": "Those passwords don’t match.",
    "weak": "Choose a password of at least 8 characters.",
    "invalid": "Please check your email address and password and try again.",
    "ratelimited": "Too many attempts — please wait a moment and try again.",
}


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
async def login_form(
    request: Request,
    error: str | None = None,
    reset: str | None = None,
    reset_error: str | None = None,
    changed: str | None = None,
    registered: str | None = None,
) -> HTMLResponse:
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    templates = request.app.state.templates
    reset_msg = _RESET_ERRORS.get(reset_error or "")
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "next": request.query_params.get("next", ""),
            "reset_ok": reset == "1",
            "reset_error": reset_msg,
            # Open the set-password form when redeeming failed, so the error
            # shows next to it.
            "show_reset": reset_msg is not None,
            # Shown after a successful in-session password change (which
            # revokes the old session — the user must sign in again).
            "changed_ok": changed == "1",
            # Shown after a successful self-service signup submission.
            "registered_ok": registered == "1",
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


@router.get("/register", response_class=HTMLResponse)
async def register_form(
    request: Request,
    error: str | None = None,
) -> HTMLResponse:
    """Public self-service signup form. Reachable before any account
    exists. Carries the disclaimer that web accounts need a linked chat
    channel for password recovery / scheduled-task notifications."""
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "register.html", {"error": _REGISTER_ERRORS.get(error or "")},
    )


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    display_name: str = Form(""),
) -> RedirectResponse:
    """Submit a public registration request. Validates locally, then calls
    the router's unauthenticated `/v1/registrations/public`. Enumeration-
    safe: a duplicate email yields the same "request received" outcome as a
    new one (the router upsert is a no-op create), so the page never reveals
    whether an email is already registered."""
    if password != confirm_password:
        return RedirectResponse(url="/register?error=mismatch", status_code=303)
    if len(password.strip()) < 8:
        return RedirectResponse(url="/register?error=weak", status_code=303)
    upstream = request.app.state.upstream
    try:
        await upstream.submit_web_registration(
            email=email.strip(),
            password=password,
            display_name=display_name.strip() or None,
        )
    except UpstreamError as exc:
        if exc.status_code == 429:
            return RedirectResponse(
                url="/register?error=ratelimited", status_code=303
            )
        if exc.status_code == 422:
            # Bad email shape or weak password the router rejected.
            return RedirectResponse(url="/register?error=invalid", status_code=303)
        # Any other upstream error (incl. a duplicate surfaced as 4xx): don't
        # leak detail — log and show the neutral "received" outcome.
        logger.info(
            "webapp_register_failed",
            extra={"event": "webapp_register_failed", "status_code": exc.status_code},
        )
    return RedirectResponse(url="/login?registered=1", status_code=303)


@router.post("/set-password")
async def set_password_submit(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
) -> RedirectResponse:
    """Redeem a one-time token from the bot's `/password` and set the web
    password. On success the user signs in below with their email + the new
    password (the router's returned TokenPair is intentionally not used —
    the user just chose the password, so a normal sign-in is the clear UX)."""
    if len(new_password.strip()) < 8:
        return RedirectResponse(url="/login?reset_error=weak", status_code=303)
    upstream = request.app.state.upstream
    try:
        await upstream.reset_password(token=token.strip(), new_password=new_password)
    except UpstreamError as exc:
        code = {401: "invalid", 429: "ratelimited", 409: "conflict"}.get(
            exc.status_code, "failed"
        )
        logger.info(
            "webapp_set_password_failed",
            extra={"event": "webapp_set_password_failed", "status_code": exc.status_code},
        )
        return RedirectResponse(url=f"/login?reset_error={code}", status_code=303)
    return RedirectResponse(url="/login?reset=1", status_code=303)


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    """Change the logged-in user's own password (current → new). On success the
    router revokes this session's tokens, so we clear the local session and send
    the user to re-login; failures bounce back to Settings with a code. The
    `/config` page renders the matching message."""
    access = request.session.get("access_token")
    if not access:  # session lapsed mid-form
        return RedirectResponse(url="/login", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/config?pw_error=mismatch", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(url="/config?pw_error=weak", status_code=303)
    upstream = request.app.state.upstream
    try:
        await upstream.change_password(
            access_token=access,
            current_password=current_password,
            new_password=new_password,
        )
    except UpstreamError as exc:
        code = {
            401: "current", 400: "same", 409: "conflict",
            422: "weak", 429: "ratelimited",
        }.get(exc.status_code, "failed")
        logger.info(
            "webapp_change_password_failed",
            extra={"event": "webapp_change_password_failed",
                   "status_code": exc.status_code},
        )
        return RedirectResponse(url=f"/config?pw_error={code}", status_code=303)
    # Router has revoked this session's tokens — drop the local session so the
    # user re-authenticates with the new password.
    clear_session(request)
    return RedirectResponse(url="/login?changed=1", status_code=303)


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
