"""bp_admin.pages.registrations — Pending user-registration queue (Phase 2 / F7).

Mounts under `/admin/registrations`. Wraps:
  - GET    /v1/admin/registrations(?channel=)
  - GET    /v1/admin/registrations/{id}
  - POST   /v1/admin/registrations/{id}/approve
  - POST   /v1/admin/registrations/{id}/reject

Approve flow: detail page shows the override form (pre-filled with
sensible defaults — `email` from the pending row, `level=tier0`,
an auto-generated 16-char password the admin can edit, optional
label). On success, the one-time-password reveal page is shown
and the admin must explicitly dismiss it before navigating away.
"""

from __future__ import annotations

import logging
import secrets

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


# Keep in sync with bp_admin.pages.users / bp_admin.pages.invitations.
LEVEL_OPTIONS = ["admin", "service", "tier0", "tier1", "tier2", "tier3"]


def _generate_initial_password() -> str:
    """Default password pre-fill. Admin can edit inline before
    submitting the form; we don't persist this server-side, so a
    reload generates a fresh value (harmless — nothing has been
    written to the DB yet)."""
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_registrations(
    request: Request,
    channel: str | None = None,
    submitted_by: str | None = None,
) -> HTMLResponse:
    params: dict[str, str | int] = {"limit": 200}
    if channel:
        params["channel"] = channel
    if submitted_by:
        # Upstream parameter name matches the column / row field.
        params["submitted_by_service_user_id"] = submitted_by
    try:
        rows = await upstream(request).admin_request(
            "GET",
            "/registrations",
            access_token=access_token(request),
            params=params,
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=is_htmx(request), active_section="registrations"
        )

    # Derive filter dropdown populations from the rows we actually
    # have. Cheaper than a dedicated catalog endpoint and adequate
    # for a small operator-facing list. When a filter is active the
    # current value is added to the option list even if no rows
    # match — otherwise the dropdown would lose the active value
    # mid-filtering.
    channels = sorted({r.get("channel") for r in rows if r.get("channel")})
    if channel and channel not in channels:
        channels = sorted([*channels, channel])
    submitters = sorted({
        r.get("submitted_by_service_user_id")
        for r in rows
        if r.get("submitted_by_service_user_id")
    })
    if submitted_by and submitted_by not in submitters:
        submitters = sorted([*submitters, submitted_by])

    templates = request.app.state.templates
    template = (
        "registrations/_table_body.html"
        if is_htmx(request)
        else "registrations/list.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_section": "registrations",
            "registrations": rows,
            "channel_filter": channel or "",
            "channel_options": channels,
            "submitted_by_filter": submitted_by or "",
            "submitted_by_options": submitters,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# Detail / approve form
# ---------------------------------------------------------------------------


@router.get("/{registration_id}", response_class=HTMLResponse)
async def registration_detail(
    request: Request, registration_id: str
) -> HTMLResponse:
    try:
        row = await upstream(request).admin_request(
            "GET",
            f"/registrations/{registration_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=False, active_section="registrations"
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "registrations/detail.html",
        {
            "active_section": "registrations",
            "registration": row,
            "level_options": LEVEL_OPTIONS,
            # Pre-filled form values. `initial_password` is generated
            # fresh on each render — the admin sees a new pre-fill on
            # reload, which is fine since nothing has been committed.
            "form": {
                "email": row.get("requested_email") or "",
                "level": "tier0",
                "initial_password": _generate_initial_password(),
                "label": "",
            },
            "error": None,
        },
    )


@router.post("/{registration_id}/approve", response_class=HTMLResponse)
async def approve_registration(
    request: Request,
    registration_id: str,
    email: str = Form(...),
    level: str = Form(...),
    initial_password: str = Form(...),
    label: str = Form(""),
) -> Response:
    templates = request.app.state.templates

    payload: dict[str, str] = {
        "email": email,
        "level": level,
        "initial_password": initial_password,
    }
    if label.strip():
        payload["label"] = label.strip()

    try:
        body = await upstream(request).admin_request(
            "POST",
            f"/registrations/{registration_id}/approve",
            access_token=access_token(request),
            json=payload,
        )
    except UpstreamError as exc:
        # Re-render the override form with the admin's existing
        # values so they don't lose their edits on a typo / 409.
        try:
            row = await upstream(request).admin_request(
                "GET",
                f"/registrations/{registration_id}",
                access_token=access_token(request),
            )
        except UpstreamError:
            row = None
        return templates.TemplateResponse(
            request,
            "registrations/detail.html",
            {
                "active_section": "registrations",
                "registration": row,
                "level_options": LEVEL_OPTIONS,
                "form": {
                    "email": email,
                    "level": level,
                    "initial_password": initial_password,
                    "label": label,
                },
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )

    return templates.TemplateResponse(
        request,
        "registrations/approved.html",
        {
            "active_section": "registrations",
            "result": body,
        },
    )


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@router.post("/{registration_id}/reject")
async def reject_registration(request: Request, registration_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "POST",
            f"/registrations/{registration_id}/reject",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/registrations", detail_message(exc)
        )
    return redirect_with_flash(
        request, "/admin/registrations", "registration rejected"
    )
