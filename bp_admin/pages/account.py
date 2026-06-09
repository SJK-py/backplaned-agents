"""bp_admin.pages.account — the logged-in admin's own account.

Self-service password change for the admin, over the router's
`POST /v1/auth/change-password` (current → new). Distinct from the per-user
password-RESET token an admin mints for OTHER users on the user detail page:
this rotates the admin's OWN sign-in password. The router revokes the caller's
tokens on success, so we drop the BFF session and send the admin to re-login.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_admin._helpers import access_token, upstream
from bp_admin.asgi_utils import root_path
from bp_admin.auth import clear_session
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()

# Messages keyed by the code put on the redirect — never reflect the router's
# raw error text back to the page.
_ERRORS = {
    "mismatch": "The new password and its confirmation don’t match.",
    "weak": "Choose a new password of at least 8 characters.",
    "current": "Your current password is incorrect.",
    "same": "The new password must differ from your current one.",
    "conflict": "This account can’t use a password login.",
    "ratelimited": "Too many attempts — please wait a moment and try again.",
    "failed": "Couldn’t change your password. Please try again.",
}


@router.get("/password", response_class=HTMLResponse)
async def password_form(request: Request, error: str | None = None) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "account/password.html",
        {"active_section": None, "error": _ERRORS.get(error or "")},
    )


@router.post("/password")
async def password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    rp = root_path(request)
    if new_password != confirm_password:
        return RedirectResponse(
            url=f"{rp}/account/password?error=mismatch", status_code=303
        )
    if len(new_password) < 8:
        return RedirectResponse(
            url=f"{rp}/account/password?error=weak", status_code=303
        )
    try:
        await upstream(request).change_password(
            access_token=access_token(request),
            current_password=current_password,
            new_password=new_password,
        )
    except UpstreamError as exc:
        code = {
            401: "current", 400: "same", 409: "conflict",
            422: "weak", 429: "ratelimited",
        }.get(exc.status_code, "failed")
        logger.info(
            "admin_change_password_failed",
            extra={"event": "admin_change_password_failed",
                   "status_code": exc.status_code},
        )
        return RedirectResponse(
            url=f"{rp}/account/password?error={code}", status_code=303
        )
    # Router has revoked this session's tokens — drop the BFF session so the
    # admin re-authenticates with the new password.
    clear_session(request)
    return RedirectResponse(url=f"{rp}/login?changed=1", status_code=303)
