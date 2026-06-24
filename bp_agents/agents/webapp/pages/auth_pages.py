"""bp_agents.agents.webapp.pages.auth_pages — login / logout (UI side).

These wrap the router's `/v1/auth/*` endpoints and translate to
session-cookie state. Any authenticated user may log in (not
admin-gated, unlike the admin BFF).
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_agents.agents.webapp.auth import (
    clear_session,
    is_authenticated,
    store_login,
)
from bp_agents.agents.webapp.pages._common import ensure_user_config
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
    next_q = request.query_params.get("next", "")
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Single sign-on failed. Please try again." if error == "sso"
            else error,
            "next": next_q,
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
            # SSO button (frontend toggle; the router gates the actual flow).
            "sso_enabled": request.app.state.config.sso_enabled,
            "sso_login_url": "/auth/sso/login" + (
                f"?next={quote(next_q)}" if next_q else ""
            ),
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
    request.session["auth_kind"] = "password"
    # Seed the suite-side user_config for first-time web accounts (no-op if it
    # already exists) — otherwise config reads/saves silently no-op.
    await ensure_user_config(request)
    return RedirectResponse(url=_safe_next(next), status_code=303)


# Session key holding the in-flight OIDC flow's transient values between the
# login redirect and the callback (state/nonce/PKCE verifier + post-login
# next). Lives in the signed session cookie — same store as the eventual
# tokens, so no weaker than the rest of the BFF session.
_SSO_FLOW_KEY = "sso_flow"


def _sso_redirect_uri(request: Request) -> str:
    base = (request.app.state.config.public_base_url or "").rstrip("/")
    return f"{base}/auth/sso/callback"


@router.get("/auth/sso/login")
async def sso_login(request: Request, next: str = "") -> RedirectResponse:
    """Begin SSO: get the OP authorize URL from the router, stash the
    transient state/nonce/verifier in the session cookie, redirect to the OP."""
    cfg = request.app.state.config
    if not cfg.sso_enabled:
        return RedirectResponse(url="/login", status_code=303)
    try:
        body = await request.app.state.upstream.oidc_authorize(
            redirect_uri=_sso_redirect_uri(request)
        )
    except UpstreamError as exc:
        logger.warning(
            "webapp_sso_authorize_failed",
            extra={"event": "webapp_sso_authorize_failed",
                   "status_code": exc.status_code},
        )
        return RedirectResponse(url="/login?error=sso", status_code=303)
    request.session[_SSO_FLOW_KEY] = {
        "state": body["state"],
        "nonce": body["nonce"],
        "verifier": body["code_verifier"],
        "next": _safe_next(next),
        # A pending link token (bot bootstrap or add-an-IdP) attaches the
        # resulting identity to a specific account instead of provisioning.
        "link_token": request.session.pop("sso_link_token", None),
    }
    return RedirectResponse(url=body["authorize_url"], status_code=303)


@router.post("/auth/sso/link")
async def sso_link_start(
    request: Request, token: str = Form(...)
) -> RedirectResponse:
    """Bootstrap SSO for a PRE-EXISTING account (e.g. a Telegram-first user):
    stash the bot-minted `/password` token, then start the SSO redirect — the
    callback attaches the validated identity to that account."""
    if not request.app.state.config.sso_enabled:
        return RedirectResponse(url="/login", status_code=303)
    request.session["sso_link_token"] = token.strip()
    return RedirectResponse(url="/auth/sso/login", status_code=303)


@router.post("/auth/sso/connect")
async def sso_connect(request: Request) -> RedirectResponse:
    """Add another SSO login to the CURRENT account. Mints a self-service link
    token (authenticated by this session), stashes it, then runs the SSO
    redirect so the callback links the new identity to this account."""
    cfg = request.app.state.config
    access = request.session.get("access_token")
    if not cfg.sso_enabled or not access:
        return RedirectResponse(url="/config", status_code=303)
    try:
        body = await request.app.state.upstream.mint_link_token(access_token=access)
    except UpstreamError as exc:
        logger.info(
            "webapp_sso_connect_failed",
            extra={"event": "webapp_sso_connect_failed",
                   "status_code": exc.status_code},
        )
        return RedirectResponse(url="/config?sso_error=1", status_code=303)
    request.session["sso_link_token"] = body["link_token"]
    return RedirectResponse(url="/auth/sso/login", status_code=303)


@router.get("/auth/sso/callback")
async def sso_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    """Finish SSO: verify `state` against the cookie, exchange the code via
    the router, and store the returned TokenPair like any other login."""
    cfg = request.app.state.config
    if not cfg.sso_enabled:
        return RedirectResponse(url="/login", status_code=303)
    # Single-use: pop the flow so a replayed callback can't be reused.
    flow = request.session.pop(_SSO_FLOW_KEY, None)
    if error or not code or not state or not flow or state != flow.get("state"):
        logger.info(
            "webapp_sso_callback_rejected",
            extra={"event": "webapp_sso_callback_rejected",
                   "reason": error or "state_mismatch_or_missing"},
        )
        return RedirectResponse(url="/login?error=sso", status_code=303)
    try:
        body = await request.app.state.upstream.oidc_exchange(
            code=code, code_verifier=flow["verifier"], nonce=flow["nonce"],
            redirect_uri=_sso_redirect_uri(request),
            link_token=flow.get("link_token"),
        )
    except UpstreamError as exc:
        logger.info(
            "webapp_sso_exchange_failed",
            extra={"event": "webapp_sso_exchange_failed",
                   "status_code": exc.status_code},
        )
        return RedirectResponse(url="/login?error=sso", status_code=303)
    # The TokenPair carries no email; SSO display falls back to user_id.
    store_login(request, login_response=body, email="")
    request.session["auth_kind"] = "oidc"
    # OIDC accounts are provisioned router-side only; seed their suite-side
    # user_config here (idempotent) so config + the agents work.
    await ensure_user_config(request)
    return RedirectResponse(
        url=_safe_next(flow.get("next") or "/"), status_code=303
    )


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
    was_oidc = request.session.get("auth_kind") == "oidc"
    # For an SSO session, ask the router for the OP's RP-initiated logout URL
    # (while we still hold the access token) so we can propagate the logout to
    # the IdP after revoking our own session.
    op_logout_url: str | None = None
    if access and was_oidc and request.app.state.config.sso_enabled:
        try:
            op_logout_url = await upstream.oidc_logout_url(access_token=access)
        except UpstreamError:
            op_logout_url = None
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
    return RedirectResponse(url=op_logout_url or "/login", status_code=303)
