"""bp_admin.pages.acl — ACL rule editor (phase 6).

Mounts under `/admin/acl/rules`. Wraps:
  - GET    /v1/admin/acl/rules
  - POST   /v1/admin/acl/rules
  - PATCH  /v1/admin/acl/rules/{rule_id}
  - DELETE /v1/admin/acl/rules/{rule_id}
  - POST   /v1/admin/acl/rules/reorder
  - POST   /v1/admin/acl/rules/simulate

Three pieces of UX:
  - Drag-drop reorder (SortableJS) → fires `POST /reorder` with the new
    rule_id sequence; the server normalises to 0,10,20,…
  - Inline simulate panel (HTMX swap) → renders the trace as a fragment.
  - Inline edit (separate form page); create-on-top via `/new`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from bp_admin._helpers import (
    access_token,
    detail_message,
    error_response,
    pop_flash,
    redirect_with_flash,
    upstream,
)
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


# Same level grammar as users/invitations, plus "*" for "any level".
RULE_USER_LEVELS = ["*", "admin", "service", "tier0", "tier1", "tier2", "tier3"]
SIM_USER_LEVELS = ["admin", "service", "tier0", "tier1", "tier2", "tier3"]
EFFECTS = ["allow", "deny"]
ORD_STEP = 10

# Defense in depth: cap the size of a reorder payload before
# forwarding upstream. Real ACL rule sets in this project sit in the
# tens; 1000 is a generous ceiling that still bounds memory / payload.
MAX_REORDER_RULES = 1000


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_rules(request: Request) -> HTMLResponse:
    try:
        rules = await upstream(request).admin_request(
            "GET", "/acl/rules", access_token=access_token(request)
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=False, active_section="acl")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "acl/list.html",
        {
            "active_section": "acl",
            "rules": rules,
            "rule_user_levels": RULE_USER_LEVELS,
            "sim_user_levels": SIM_USER_LEVELS,
            "effects": EFFECTS,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# Reorder (HTMX swap target: #rules-table)
# ---------------------------------------------------------------------------


@router.post("/reorder", response_class=HTMLResponse)
async def reorder_rules(request: Request) -> Response:
    """Accepts `rule_ids` form-encoded as a list (DOM order). Maps each
    to `i * ORD_STEP` and forwards to upstream `/acl/rules/reorder`.
    Returns the refreshed `_table_body` fragment so the table redraws
    with the canonical ords from the server."""
    form = await request.form()
    rule_ids = form.getlist("rule_ids")
    if not rule_ids:
        raise HTTPException(status_code=400, detail="rule_ids required")
    if len(rule_ids) > MAX_REORDER_RULES:
        raise HTTPException(
            status_code=400,
            detail=f"too many rule_ids ({len(rule_ids)} > {MAX_REORDER_RULES})",
        )

    new_ords = {rid: i * ORD_STEP for i, rid in enumerate(rule_ids)}

    try:
        await upstream(request).admin_request(
            "POST",
            "/acl/rules/reorder",
            access_token=access_token(request),
            json={"new_ords": new_ords},
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=True, active_section="acl")

    # Re-fetch and render the updated table body.
    rules = await upstream(request).admin_request(
        "GET", "/acl/rules", access_token=access_token(request)
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "acl/_table_body.html",
        {
            "active_section": "acl",
            "rules": rules,
        },
    )


# ---------------------------------------------------------------------------
# Simulate (HTMX swap target: #simulate-result)
# ---------------------------------------------------------------------------


@router.post("/simulate", response_class=HTMLResponse)
async def simulate_rule(
    request: Request,
    caller_id: str = Form(...),
    callee_id: str = Form(...),
    user_level: str = Form(...),
) -> HTMLResponse:
    templates = request.app.state.templates
    try:
        result = await upstream(request).admin_request(
            "POST",
            "/acl/rules/simulate",
            access_token=access_token(request),
            json={
                "caller_id": caller_id.strip(),
                "callee_id": callee_id.strip(),
                "user_level": user_level,
            },
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "acl/_simulate_result.html",
            {
                "result": None,
                "error": detail_message(exc),
                "caller_id": caller_id,
                "callee_id": callee_id,
                "user_level": user_level,
            },
            status_code=exc.status_code,
        )

    return templates.TemplateResponse(
        request,
        "acl/_simulate_result.html",
        {
            "result": result,
            "error": None,
            "caller_id": caller_id,
            "callee_id": callee_id,
            "user_level": user_level,
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def new_rule_form(request: Request) -> HTMLResponse:
    # Pre-fill `ord` with the next free slot so the admin doesn't collide
    # with an existing rule by default.
    try:
        rules = await upstream(request).admin_request(
            "GET", "/acl/rules", access_token=access_token(request)
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=False, active_section="acl")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "acl/new.html",
        {
            "active_section": "acl",
            "rule_user_levels": RULE_USER_LEVELS,
            "effects": EFFECTS,
            "form": {
                "ord": str(_next_ord(rules)),
                "name": "",
                "description": "",
                "effect": "allow",
                "user_level": "*",
                "caller_pattern": "*/*",
                "callee_pattern": "*/*",
            },
            "error": None,
        },
    )


@router.post("/new", response_class=HTMLResponse)
async def create_rule(
    request: Request,
    ord: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
    effect: str = Form(...),
    user_level: str = Form(...),
    caller_pattern: str = Form(...),
    callee_pattern: str = Form(...),
) -> Response:
    templates = request.app.state.templates
    form = {
        "ord": ord,
        "name": name,
        "description": description,
        "effect": effect,
        "user_level": user_level,
        "caller_pattern": caller_pattern,
        "callee_pattern": callee_pattern,
    }

    try:
        ord_int = int(ord)
        if ord_int < 0:
            raise ValueError
    except (TypeError, ValueError):
        return templates.TemplateResponse(
            request,
            "acl/new.html",
            {
                "active_section": "acl",
                "rule_user_levels": RULE_USER_LEVELS,
                "effects": EFFECTS,
                "form": form,
                "error": "Ord must be a non-negative integer.",
            },
            status_code=400,
        )

    payload = {
        "ord": ord_int,
        "effect": effect,
        "user_level": user_level,
        "caller_pattern": caller_pattern.strip(),
        "callee_pattern": callee_pattern.strip(),
        "name": name.strip() or None,
        "description": description.strip() or None,
    }

    try:
        await upstream(request).admin_request(
            "POST",
            "/acl/rules",
            access_token=access_token(request),
            json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "acl/new.html",
            {
                "active_section": "acl",
                "rule_user_levels": RULE_USER_LEVELS,
                "effects": EFFECTS,
                "form": form,
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )

    return redirect_with_flash(request, "/admin/acl/rules", "rule created")


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@router.get("/{rule_id}/edit", response_class=HTMLResponse)
async def edit_rule_form(request: Request, rule_id: str) -> HTMLResponse:
    try:
        rules = await upstream(request).admin_request(
            "GET", "/acl/rules", access_token=access_token(request)
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=False, active_section="acl")

    rule = next((r for r in rules if r.get("rule_id") == rule_id), None)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "acl/edit.html",
        {
            "active_section": "acl",
            "rule_user_levels": RULE_USER_LEVELS,
            "effects": EFFECTS,
            "rule": rule,
            "form": {
                "ord": str(rule["ord"]),
                "name": rule.get("name") or "",
                "description": rule.get("description") or "",
                "effect": rule["effect"],
                "user_level": rule["user_level"],
                "caller_pattern": rule["caller_pattern"],
                "callee_pattern": rule["callee_pattern"],
            },
            "error": None,
        },
    )


@router.post("/{rule_id}/edit", response_class=HTMLResponse)
async def update_rule(
    request: Request,
    rule_id: str,
    ord: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
    effect: str = Form(...),
    user_level: str = Form(...),
    caller_pattern: str = Form(...),
    callee_pattern: str = Form(...),
) -> Response:
    templates = request.app.state.templates
    form = {
        "ord": ord,
        "name": name,
        "description": description,
        "effect": effect,
        "user_level": user_level,
        "caller_pattern": caller_pattern,
        "callee_pattern": callee_pattern,
    }

    try:
        ord_int = int(ord)
        if ord_int < 0:
            raise ValueError
    except (TypeError, ValueError):
        return templates.TemplateResponse(
            request,
            "acl/edit.html",
            {
                "active_section": "acl",
                "rule_user_levels": RULE_USER_LEVELS,
                "effects": EFFECTS,
                "rule": {"rule_id": rule_id, **form, "ord": ord_int if ord.isdigit() else 0},
                "form": form,
                "error": "Ord must be a non-negative integer.",
            },
            status_code=400,
        )

    payload = {
        "ord": ord_int,
        "effect": effect,
        "user_level": user_level,
        "caller_pattern": caller_pattern.strip(),
        "callee_pattern": callee_pattern.strip(),
        "name": name.strip() or None,
        "description": description.strip() or None,
    }

    try:
        await upstream(request).admin_request(
            "PATCH",
            f"/acl/rules/{rule_id}",
            access_token=access_token(request),
            json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "acl/edit.html",
            {
                "active_section": "acl",
                "rule_user_levels": RULE_USER_LEVELS,
                "effects": EFFECTS,
                "rule": {"rule_id": rule_id, **form},
                "form": form,
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )

    return redirect_with_flash(request, "/admin/acl/rules", "rule updated")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.post("/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "DELETE",
            f"/acl/rules/{rule_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(request, "/admin/acl/rules", detail_message(exc))
    return redirect_with_flash(request, "/admin/acl/rules", "rule deleted")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_ord(rules: list[dict]) -> int:
    if not rules:
        return 0
    return max(int(r["ord"]) for r in rules) + ORD_STEP
