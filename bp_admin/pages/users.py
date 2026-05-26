"""bp_admin.pages.users — Users management page (phase 3).

Mounts under `/admin/users`. Handlers wrap the upstream
`/v1/admin/users` JSON API and render either full pages or HTMX
partials depending on the caller (full page on direct nav, partial
on `HX-Request: true`).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

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


# Hardcoded for the dropdown. Custom tier indices remain insertable
# via the JSON API; the UI shows the common five plus admin / service.
LEVEL_OPTIONS = ["admin", "service", "tier0", "tier1", "tier2", "tier3"]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_users(
    request: Request,
    level: str | None = None,
    include_deleted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> HTMLResponse:
    """List users with optional level filter.

    `include_deleted=False` (default) hides soft-deleted users —
    the common operator view. Set to True via the "Show deleted"
    toggle on the list page to surface them.

    Renders the full page on direct nav, or just the table body when
    HTMX swaps via the filter dropdown.
    """
    params: dict[str, str | int | bool] = {"limit": limit, "offset": offset}
    if level:
        params["level"] = level
    if include_deleted:
        params["include_deleted"] = "true"
    try:
        users = await upstream(request).admin_request(
            "GET",
            "/users",
            access_token=access_token(request),
            params=params,
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=is_htmx(request), active_section="users")

    templates = request.app.state.templates
    template = "users/_table_body.html" if is_htmx(request) else "users/list.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_section": "users",
            "users": users,
            "level_filter": level or "",
            "level_options": LEVEL_OPTIONS,
            "include_deleted": include_deleted,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(users),
            "has_more": len(users) == limit,
        },
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def new_user_form(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "users/new.html",
        {
            "active_section": "users",
            "level_options": LEVEL_OPTIONS,
            "form": {"email": "", "level": "tier0", "initial_password": ""},
            "error": None,
        },
    )


@router.post("/new", response_class=HTMLResponse)
async def create_user(
    request: Request,
    email: str = Form(...),
    level: str = Form(...),
    initial_password: str = Form(""),
) -> Response:
    templates = request.app.state.templates
    payload: dict[str, str] = {"email": email, "level": level}
    if initial_password:
        payload["initial_password"] = initial_password

    try:
        user = await upstream(request).admin_request(
            "POST",
            "/users",
            access_token=access_token(request),
            json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "users/new.html",
            {
                "active_section": "users",
                "level_options": LEVEL_OPTIONS,
                "form": {"email": email, "level": level, "initial_password": ""},
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )

    return RedirectResponse(url=f"/admin/users/{user['user_id']}", status_code=303)


@router.get("/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: str) -> HTMLResponse:
    up = upstream(request)
    token = access_token(request)
    try:
        user = await up.admin_request(
            "GET", f"/users/{user_id}", access_token=token
        )
    except UpstreamError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="user not found") from exc
        return error_response(request, exc, partial=False, active_section="users")

    # Tasks and audit are best-effort; show empty if the upstream
    # surfaces an error here so the main detail still renders.
    tasks: list = []
    audit: list = []
    try:
        tasks = await up.admin_request(
            "GET",
            f"/users/{user_id}/tasks",
            access_token=token,
            params={"limit": 10},
        )
    except UpstreamError as exc:
        logger.warning(
            "user_detail_tasks_fetch_failed",
            extra={
                "event": "user_detail_tasks_fetch_failed",
                "status_code": exc.status_code,
            },
        )
    try:
        audit = await up.admin_request(
            "GET",
            "/audit",
            access_token=token,
            params={"actor_id": user_id, "limit": 20},
        )
    except UpstreamError as exc:
        logger.warning(
            "user_detail_audit_fetch_failed",
            extra={
                "event": "user_detail_audit_fetch_failed",
                "status_code": exc.status_code,
            },
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "users/detail.html",
        {
            "active_section": "users",
            "user": user,
            "tasks": tasks,
            "audit": audit,
            "level_options": LEVEL_OPTIONS,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@router.post("/{user_id}/level", response_class=HTMLResponse)
async def change_level(
    request: Request,
    user_id: str,
    level: str = Form(...),
) -> Response:
    if level not in LEVEL_OPTIONS:
        # The router validates too; keep the UI message tight.
        return redirect_with_flash(
            request,
            f"/admin/users/{user_id}",
            f"unsupported level: {level!r}",
        )
    return await _patch_user(request, user_id, {"level": level}, "level changed")


@router.post("/{user_id}/suspend", response_class=HTMLResponse)
async def suspend_user(request: Request, user_id: str) -> Response:
    return await _patch_user(request, user_id, {"suspended": True}, "user suspended")


@router.post("/{user_id}/unsuspend", response_class=HTMLResponse)
async def unsuspend_user(request: Request, user_id: str) -> Response:
    return await _patch_user(request, user_id, {"suspended": False}, "user unsuspended")


async def _patch_user(
    request: Request,
    user_id: str,
    body: dict,
    success_msg: str,
) -> Response:
    try:
        await upstream(request).admin_request(
            "PATCH",
            f"/users/{user_id}",
            access_token=access_token(request),
            json=body,
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", detail_message(exc)
        )
    return redirect_with_flash(request, f"/admin/users/{user_id}", success_msg)


# ---------------------------------------------------------------------------
# Phase 9b: serviced_by membership + credential management
# ---------------------------------------------------------------------------


@router.post("/{user_id}/serviced-by", response_class=HTMLResponse)
async def grant_serviced_by(
    request: Request,
    user_id: str,
    service_user_id: str = Form(...),
) -> Response:
    """Add a service principal to the user's `serviced_by` array.
    Wraps PUT /v1/admin/users/{id}/serviced-by/{svc_id}. Router
    validates that the grantee has `level=service` and that both
    users exist; UI just passes the typed value through."""
    svc = service_user_id.strip()
    if not svc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", "service_user_id is required"
        )
    try:
        await upstream(request).admin_request(
            "PUT",
            f"/users/{user_id}/serviced-by/{svc}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", detail_message(exc)
        )
    return redirect_with_flash(
        request, f"/admin/users/{user_id}", f"granted {svc!r} to serviced_by"
    )


@router.post("/{user_id}/serviced-by/{service_user_id}/revoke", response_class=HTMLResponse)
async def revoke_serviced_by(
    request: Request, user_id: str, service_user_id: str
) -> Response:
    """Remove a service principal from `serviced_by`.

    IMPORTANT: this does NOT invalidate already-minted refresh
    tokens. The UI surfaces a warning on the confirm prompt; if the
    operator needs a full cut-off, they must also click 'Revoke
    all refresh tokens' on the same page."""
    try:
        await upstream(request).admin_request(
            "DELETE",
            f"/users/{user_id}/serviced-by/{service_user_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", detail_message(exc)
        )
    return redirect_with_flash(
        request,
        f"/admin/users/{user_id}",
        f"removed {service_user_id!r} from serviced_by",
    )


@router.post("/{user_id}/refresh-tokens/revoke", response_class=HTMLResponse)
async def revoke_user_refresh_tokens(
    request: Request, user_id: str
) -> Response:
    """Delete every refresh token for the user. Forces re-login on
    every device on the next refresh. Use after revoking a service
    principal's servicing rights to fully cut off access."""
    try:
        await upstream(request).admin_request(
            "DELETE",
            f"/users/{user_id}/refresh-tokens",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", detail_message(exc)
        )
    return redirect_with_flash(
        request,
        f"/admin/users/{user_id}",
        "revoked all refresh tokens — user must re-login on every device",
    )


@router.post("/{user_id}/password-reset", response_class=HTMLResponse)
async def mint_password_reset_token(
    request: Request, user_id: str
) -> Response:
    """Mint a single-use password-reset token for this user. Renders
    the one-time-reveal page on success — admin must explicitly
    dismiss the acknowledgement before navigation links re-enable."""
    try:
        body = await upstream(request).admin_request(
            "POST",
            f"/users/{user_id}/password-reset-tokens",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", detail_message(exc)
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "users/password_reset_minted.html",
        {
            "active_section": "users",
            "user_id": user_id,
            "reset_token": body["reset_token"],
            "expires_at": body["expires_at"],
            "target_user_id": body["target_user_id"],
        },
    )


@router.post("/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(request: Request, user_id: str) -> Response:
    """Soft-delete the user. Idempotent: a second click on an
    already-deleted user is a no-op flash. Refused by the router
    when the operator tries to delete their own user."""
    try:
        await upstream(request).admin_request(
            "DELETE",
            f"/users/{user_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/users/{user_id}", detail_message(exc)
        )
    return redirect_with_flash(
        request,
        f"/admin/users/{user_id}",
        "user deleted — refresh tokens revoked, serviced_by references swept",
    )
