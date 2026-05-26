"""bp_admin.pages.agents — Agents management page (phase 4).

Mounts under `/admin/agents`. Wraps the upstream `/v1/admin/agents`
JSON API. Three lifecycle actions: suspend (reversible), unsuspend
(reverse), evict (terminal — type-to-confirm).

Status filter is applied client-side (in the BFF) since the upstream
list endpoint doesn't currently support `?status=` filtering and
agent counts are typically small.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
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


STATUS_OPTIONS = ["active", "suspended", "removed", "pending"]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_agents(
    request: Request,
    status: str | None = None,
) -> HTMLResponse:
    """List agents with optional status filter (client-side filtering).

    Renders the full page on direct nav, or just the table body when
    HTMX swaps via the filter dropdown.
    """
    try:
        agents = await upstream(request).admin_request(
            "GET", "/agents", access_token=access_token(request)
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=is_htmx(request), active_section="agents")

    if status:
        agents = [a for a in agents if a.get("status") == status]

    templates = request.app.state.templates
    template = "agents/_table_body.html" if is_htmx(request) else "agents/list.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_section": "agents",
            "agents": agents,
            "status_filter": status or "",
            "status_options": STATUS_OPTIONS,
        },
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/{agent_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_id: str) -> HTMLResponse:
    up = upstream(request)
    token = access_token(request)
    try:
        agent = await up.admin_request(
            "GET", f"/agents/{agent_id}", access_token=token
        )
    except UpstreamError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="agent not found") from exc
        return error_response(request, exc, partial=False, active_section="agents")

    # Tasks and audit are best-effort; render the page even if these fail.
    tasks: list = []
    audit: list = []
    try:
        tasks = await up.admin_request(
            "GET",
            f"/agents/{agent_id}/tasks",
            access_token=token,
            params={"limit": 10},
        )
    except UpstreamError as exc:
        logger.warning(
            "agent_detail_tasks_fetch_failed",
            extra={
                "event": "agent_detail_tasks_fetch_failed",
                "status_code": exc.status_code,
            },
        )
    try:
        audit = await up.admin_request(
            "GET",
            "/audit",
            access_token=token,
            params={"actor_id": agent_id, "limit": 20},
        )
    except UpstreamError as exc:
        logger.warning(
            "agent_detail_audit_fetch_failed",
            extra={
                "event": "agent_detail_audit_fetch_failed",
                "status_code": exc.status_code,
            },
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "agents/detail.html",
        {
            "active_section": "agents",
            "agent": agent,
            "tasks": tasks,
            "audit": audit,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/suspend", response_class=HTMLResponse)
async def suspend_agent(request: Request, agent_id: str) -> Response:
    return await _post_lifecycle(
        request, agent_id, "/suspend", "agent suspended"
    )


@router.post("/{agent_id}/unsuspend", response_class=HTMLResponse)
async def unsuspend_agent(request: Request, agent_id: str) -> Response:
    return await _post_lifecycle(
        request, agent_id, "/unsuspend", "agent unsuspended"
    )


@router.post("/{agent_id}/evict", response_class=HTMLResponse)
async def evict_agent(
    request: Request,
    agent_id: str,
    confirm_agent_id: str = Form(...),
) -> Response:
    if confirm_agent_id != agent_id:
        return redirect_with_flash(
            request,
            f"/admin/agents/{agent_id}",
            "Eviction cancelled — agent_id confirmation didn't match.",
        )
    return await _post_lifecycle(
        request, agent_id, "/evict", "agent evicted (terminal)"
    )


async def _post_lifecycle(
    request: Request,
    agent_id: str,
    action_path: str,
    success_msg: str,
) -> Response:
    try:
        body = await upstream(request).admin_request(
            "POST",
            f"/agents/{agent_id}{action_path}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, f"/admin/agents/{agent_id}", detail_message(exc)
        )

    msg = success_msg
    if body and "failed_tasks" in body:
        n = body["failed_tasks"]
        if n:
            msg = f"{success_msg} — {n} in-flight task(s) failed."
    return redirect_with_flash(request, f"/admin/agents/{agent_id}", msg)
