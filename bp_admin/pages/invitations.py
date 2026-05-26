"""bp_admin.pages.invitations — Agent invitations (phase 5).

Mounts under `/admin/invitations`. Wraps:
  - GET    /v1/admin/invitations
  - POST   /v1/admin/invitations
  - DELETE /v1/admin/invitations/{token_hash}

The freshly-issued raw token is rendered ONCE inline on the success
page; the router never stores it (only the SHA hash). If the admin
loses it, they revoke and re-issue.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from bp_admin._helpers import (
    access_token,
    detail_message,
    error_response,
    is_htmx,
    pop_flash,
    redirect_with_flash,
    upstream,
)
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


# Mirrors the level grammar; keep in sync with bp_admin.pages.users.
LEVEL_OPTIONS = ["admin", "service", "tier0", "tier1", "tier2", "tier3"]

# Common TTL presets; the form also accepts a custom seconds value.
TTL_PRESETS = [
    ("3600", "1 hour"),
    ("86400", "24 hours"),
    ("604800", "7 days"),
    ("2592000", "30 days"),
]
STATUS_OPTIONS = ["valid", "used", "expired"]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_invitations(
    request: Request,
    status: str | None = None,
) -> HTMLResponse:
    params: dict[str, str | int] = {"limit": 200}
    if status:
        params["status"] = status

    try:
        rows = await upstream(request).admin_request(
            "GET",
            "/invitations",
            access_token=access_token(request),
            params=params,
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=is_htmx(request), active_section="invitations"
        )

    templates = request.app.state.templates
    template = (
        "invitations/_table_body.html" if is_htmx(request) else "invitations/list.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_section": "invitations",
            "invitations": rows,
            "status_filter": status or "",
            "status_options": STATUS_OPTIONS,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def new_invitation_form(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "invitations/new.html",
        {
            "active_section": "invitations",
            "level_options": LEVEL_OPTIONS,
            "ttl_presets": TTL_PRESETS,
            "form": {
                "level": "tier0",
                "expires_in": "86400",
                "custom_seconds": "",
                "token": "",
            },
            "error": None,
        },
    )


# Mirrors the router-side validation in
# `bp_router.api.admin.IssueInvitationRequest._token_shape`. We
# check UI-side too so the operator sees a clear inline error
# instead of an opaque 422 from the upstream.
_TOKEN_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_token(token: str) -> str | None:
    """Return an error string if `token` is invalid; None if OK."""
    if len(token) < 32:
        return "token must be at least 32 characters"
    if not _TOKEN_URLSAFE_RE.match(token):
        return (
            "token must use URL-safe characters only "
            "(A-Z a-z 0-9 - _)"
        )
    return None


@router.post("/new", response_class=HTMLResponse)
async def issue_invitation(
    request: Request,
    level: str = Form(...),
    expires_in: str = Form(...),
    custom_seconds: str = Form(""),
    token: str = Form(""),
) -> Response:
    templates = request.app.state.templates

    def _redisplay(error: str, status_code: int = 400) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "invitations/new.html",
            {
                "active_section": "invitations",
                "level_options": LEVEL_OPTIONS,
                "ttl_presets": TTL_PRESETS,
                "form": {
                    "level": level,
                    "expires_in": expires_in,
                    "custom_seconds": custom_seconds,
                    "token": token,
                },
                "error": error,
            },
            status_code=status_code,
        )

    # Resolve TTL: either a preset value or the custom seconds field.
    if expires_in == "custom":
        try:
            ttl_s = int(custom_seconds)
            if ttl_s <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _redisplay("Custom seconds must be a positive integer.")
    else:
        try:
            ttl_s = int(expires_in)
        except ValueError:
            return redirect_with_flash(
                request, "/admin/invitations/new", "invalid TTL preset"
            )

    # Custom-token validation (UI-side mirror of the router check).
    custom_token = token.strip()
    if custom_token:
        err = _validate_token(custom_token)
        if err is not None:
            return _redisplay(err)

    body_json: dict[str, str | int] = {"level": level, "expires_in_s": ttl_s}
    if custom_token:
        body_json["token"] = custom_token

    try:
        body = await upstream(request).admin_request(
            "POST",
            "/invitations",
            access_token=access_token(request),
            json=body_json,
        )
    except UpstreamError as exc:
        return _redisplay(detail_message(exc), status_code=exc.status_code)

    # Render the one-time-token reveal page. The raw token is not
    # persisted server-side and is gone after this response — admin
    # must save it before clicking through.
    return templates.TemplateResponse(
        request,
        "invitations/issued.html",
        {
            "active_section": "invitations",
            "level": level,
            "ttl_s": ttl_s,
            "invitation_token": body["invitation_token"],
            "expires_at": body["expires_at"],
        },
    )


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


@router.post("/{token_hash}/revoke")
async def revoke_invitation(request: Request, token_hash: str) -> Response:
    try:
        await upstream(request).admin_request(
            "DELETE",
            f"/invitations/{token_hash}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/invitations", detail_message(exc)
        )
    return redirect_with_flash(request, "/admin/invitations", "invitation revoked")
