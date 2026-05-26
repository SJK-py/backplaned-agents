"""bp_admin.pages.test_task — Test-agent page (phase 9).

Mounts under `/admin/test-task`. Wraps `POST /v1/admin/tasks/test`,
which admits a task as the synthetic `admin_console` agent through the
real admit/dispatch path. End-to-end smoke for ACL + schema + delivery
+ state machine — useful for poking at a specific agent without
bringing up an SDK client.

The form fetches the active-agent roster on GET and offers it as a
datalist suggestion. `act_as_user_id` is exposed but gated upstream by
`ROUTER_ADMIN_TEST_ALLOW_ACT_AS`; if disabled, the upstream returns 403
and we surface the error inline.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from bp_admin._helpers import access_token, upstream
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
# GET — form
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def test_task_form(request: Request) -> HTMLResponse:
    up = upstream(request)
    token = access_token(request)

    # Best-effort: surface active agents as a datalist so the admin can
    # pick from connected callees rather than typing free-form.
    try:
        agents = await up.admin_request("GET", "/agents", access_token=token)
        active_agents = [a for a in agents if a.get("status") == "active"]
    except UpstreamError as exc:
        logger.warning(
            "test_task_agents_fetch_failed",
            extra={
                "event": "test_task_agents_fetch_failed",
                "status_code": exc.status_code,
            },
        )
        active_agents = []

    # Schema lookup map for the client-side Alpine handler. When the
    # admin types / picks an agent_id, the form renders that agent's
    # accepts_schema (and accepts_control_schema, when set) so they
    # know what payload shape the router will admit. Pre-serialised
    # here so Alpine can dereference without an extra HTTP round-trip.
    agent_schema_lookup: dict[str, dict[str, Any]] = {}
    for a in active_agents:
        info = a.get("agent_info") or {}
        agent_schema_lookup[a["agent_id"]] = {
            "description": info.get("description") or "",
            "capabilities": a.get("capabilities") or [],
            "accepts_schema": info.get("accepts_schema"),
            "accepts_control_schema": info.get("accepts_control_schema"),
        }

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "test_task/form.html",
        {
            "active_section": "test",
            "active_agents": active_agents,
            # Pass the dict; template renders via `tojson` which is
            # HTML-attribute-safe. Pre-serialising with `json.dumps`
            # and rendering with `|safe` was an XSS vector (agent
            # description containing `"` broke out of the
            # double-quoted x-data attribute and injected onto the
            # admin's session). See R4 second-pass review.
            "agent_schema_lookup": agent_schema_lookup,
            "form": {
                "destination_agent_id": request.query_params.get("agent_id", ""),
                "payload": "{}",
                "act_as_user_id": "",
                "session_id": "",
                "wait": True,
                "timeout_s": str(DEFAULT_TIMEOUT_S),
            },
            "default_timeout_s": DEFAULT_TIMEOUT_S,
            "max_timeout_s": MAX_TIMEOUT_S,
        },
    )


# ---------------------------------------------------------------------------
# POST — submit (HTMX swap target: #test-result)
# ---------------------------------------------------------------------------


@router.post("", response_class=HTMLResponse)
async def submit_test_task(
    request: Request,
    destination_agent_id: str = Form(...),
    payload: str = Form("{}"),
    act_as_user_id: str = Form(""),
    session_id: str = Form(""),
    wait: str = Form(""),
    timeout_s: str = Form(str(DEFAULT_TIMEOUT_S)),
) -> HTMLResponse:
    templates = request.app.state.templates

    # Parse the JSON payload locally so we give a clean error on
    # malformed input rather than relying on the upstream 422.
    try:
        payload_json: Any = json.loads(payload) if payload.strip() else {}
        if not isinstance(payload_json, dict):
            raise ValueError("payload must be a JSON object")
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            request,
            "test_task/_result.html",
            {
                "result": None,
                "error": {"code": "invalid_payload", "message": str(exc) or "invalid JSON"},
                "submitted_destination": destination_agent_id,
            },
            status_code=400,
        )

    try:
        timeout_f = float(timeout_s)
    except (TypeError, ValueError):
        timeout_f = DEFAULT_TIMEOUT_S
    timeout_f = max(0.1, min(MAX_TIMEOUT_S, timeout_f))

    body: dict[str, Any] = {
        "destination_agent_id": destination_agent_id.strip(),
        "payload": payload_json,
        "wait": wait == "on" or wait == "true",
        "timeout_s": timeout_f,
    }
    if act_as_user_id.strip():
        body["act_as_user_id"] = act_as_user_id.strip()
    if session_id.strip():
        body["session_id"] = session_id.strip()

    try:
        result = await upstream(request).admin_request(
            "POST",
            "/tasks/test",
            access_token=access_token(request),
            json=body,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "test_task/_result.html",
            {
                "result": None,
                "error": _error_from_exc(exc),
                "submitted_destination": destination_agent_id,
            },
            status_code=exc.status_code,
        )

    # Pre-render output / error JSON for the template.
    output = result.get("output")
    err = result.get("error")
    result["output_json"] = (
        json.dumps(output, indent=2, sort_keys=True, default=str)
        if output is not None
        else None
    )
    result["error_json"] = (
        json.dumps(err, indent=2, sort_keys=True, default=str)
        if err is not None
        else None
    )

    return templates.TemplateResponse(
        request,
        "test_task/_result.html",
        {
            "result": result,
            "error": None,
            "submitted_destination": destination_agent_id,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_from_exc(exc: UpstreamError) -> dict[str, Any]:
    """Normalise an UpstreamError into {code, message, status_code}."""
    detail = exc.detail
    code: str | None = None
    message: str
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message") or detail.get("detail") or str(detail)
    else:
        message = str(detail)
    return {"code": code, "message": message, "status_code": exc.status_code}
