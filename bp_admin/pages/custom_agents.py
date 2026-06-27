"""bp_admin.pages.custom_agents — operator-defined LLM agent config UI.

Mounts under `/admin/custom-agents`. Wraps:
  - GET    /v1/admin/custom-agents
  - GET    /v1/admin/custom-agents/{agent_id}
  - POST   /v1/admin/custom-agents
  - PATCH  /v1/admin/custom-agents/{agent_id}
  - DELETE /v1/admin/custom-agents/{agent_id}
  - POST   /v1/admin/custom-agents/{agent_id}/reconnect

This UI manages config only; the `bp_mcp_bridge` process is the runtime
that turns each row into one live agent (`custom_<id>`, a single mode
whose handler runs an LLM completion against the chosen preset).
"""

from __future__ import annotations

import logging
from typing import Any

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


def _parse_groups(raw: str) -> list[str]:
    """Comma-separated group list, stripped + deduped, order-stable."""
    seen: set[str] = set()
    out: list[str] = []
    for raw_group in raw.split(","):
        g = raw_group.strip()
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _parse_capabilities(raw: str) -> list[str]:
    """Comma/space-separated capability list, stripped + deduped, order-stable.
    The router validates each against the dotted capability grammar."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw.replace(",", " ").split():
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _parse_parameters(raw: str) -> list[dict[str, Any]]:
    """One parameter per line: `name | description | required`.

    Only `name` is mandatory; `description` defaults to empty and
    `required` defaults to true (set the third field to false/optional/no
    to make it optional). The router validates names + uniqueness."""
    out: list[dict[str, Any]] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        if not name:
            continue
        desc = parts[1] if len(parts) > 1 else ""
        required = True
        if len(parts) > 2:
            required = parts[2].lower() not in ("false", "0", "no", "optional")
        out.append({"name": name, "description": desc, "required": required})
    return out


def _format_parameters(params: list[dict[str, Any]]) -> str:
    """Render parameter dicts back to the textarea format."""
    lines = []
    for p in params or []:
        required = "true" if p.get("required", True) else "false"
        lines.append(f"{p.get('name', '')} | {p.get('description', '')} | {required}")
    return "\n".join(lines)


async def _preset_names(request: Request) -> list[str]:
    """The preset names for the dropdown. Best-effort: an upstream error
    yields an empty list (the form still renders; submit will surface the
    real error)."""
    try:
        presets = await upstream(request).admin_request(
            "GET", "/llm/presets", access_token=access_token(request),
        )
    except UpstreamError:
        return []
    return [p["name"] for p in presets if isinstance(p, dict) and p.get("name")]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_custom_agents(request: Request) -> HTMLResponse:
    try:
        agents = await upstream(request).admin_request(
            "GET", "/custom-agents", access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=is_htmx(request),
            active_section="custom_agents",
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "custom_agents/list.html",
        {
            "active_section": "custom_agents",
            "agents": agents,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# New / Edit
# ---------------------------------------------------------------------------


def _empty_form() -> dict[str, Any]:
    return {
        "agent_id": "",
        "description": "",
        "preset_name": "",
        "system_prompt": "",
        "user_prompt": "",
        "parameters": "",
        "groups": "",
        "capabilities": "",
        "expose_to_llm": True,
        "output_as_file": False,
        "enabled": True,
    }


def _form_from_agent(a: dict[str, Any]) -> dict[str, Any]:
    # The stored agent_id is the full `custom_<slug>`; the form edits the
    # bare slug (the prefix is fixed + shown next to the input).
    agent_id = a["agent_id"]
    slug = agent_id[len("custom_"):] if agent_id.startswith("custom_") else agent_id
    return {
        "agent_id": slug,
        "description": a.get("description") or "",
        "preset_name": a.get("preset_name") or "",
        "system_prompt": a.get("system_prompt") or "",
        "user_prompt": a.get("user_prompt") or "",
        "parameters": _format_parameters(a.get("parameters") or []),
        "groups": ", ".join(a.get("groups") or []),
        "capabilities": ", ".join(a.get("capabilities") or []),
        "expose_to_llm": a.get("expose_to_llm", True),
        "output_as_file": a.get("output_as_file", False),
        "enabled": a.get("enabled", True),
    }


@router.get("/new", response_class=HTMLResponse)
async def new_custom_agent_form(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "custom_agents/form.html",
        {
            "active_section": "custom_agents",
            "mode": "new",
            "form": _empty_form(),
            "presets": await _preset_names(request),
            "error": None,
        },
    )


@router.get("/{agent_id}/edit", response_class=HTMLResponse)
async def edit_custom_agent_form(request: Request, agent_id: str) -> HTMLResponse:
    try:
        agent = await upstream(request).admin_request(
            "GET", f"/custom-agents/{agent_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=False, active_section="custom_agents",
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "custom_agents/form.html",
        {
            "active_section": "custom_agents",
            "mode": "edit",
            "agent": agent,
            "form": _form_from_agent(agent),
            "presets": await _preset_names(request),
            "error": None,
        },
    )


def _create_payload(
    slug: str,
    description: str,
    preset_name: str,
    system_prompt: str,
    user_prompt: str,
    parameters: str,
    groups: str,
    capabilities: str,
    expose_to_llm: bool,
    output_as_file: bool,
    enabled: bool,
) -> dict[str, Any]:
    return {
        "agent_id": f"custom_{slug.strip()}",
        "description": description.strip(),
        "preset_name": preset_name.strip(),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "parameters": _parse_parameters(parameters),
        "groups": _parse_groups(groups),
        "capabilities": _parse_capabilities(capabilities),
        "expose_to_llm": expose_to_llm,
        "output_as_file": output_as_file,
        "enabled": enabled,
    }


def _form_echo(payload: dict[str, Any], slug: str) -> dict[str, Any]:
    """Rebuild the form dict from a rejected payload so the admin's inputs
    survive the re-render."""
    return {
        "agent_id": slug,
        "description": payload["description"],
        "preset_name": payload["preset_name"],
        "system_prompt": payload["system_prompt"],
        "user_prompt": payload["user_prompt"],
        "parameters": _format_parameters(payload["parameters"]),
        "groups": ", ".join(payload["groups"]),
        "capabilities": ", ".join(payload["capabilities"]),
        "expose_to_llm": payload["expose_to_llm"],
        "output_as_file": payload["output_as_file"],
        "enabled": payload["enabled"],
    }


@router.post("/new", response_class=HTMLResponse)
async def create_custom_agent(
    request: Request,
    agent_id: str = Form(...),  # bare slug; the page prepends custom_
    description: str = Form(""),
    preset_name: str = Form(...),
    system_prompt: str = Form(""),
    user_prompt: str = Form(""),
    parameters: str = Form(""),
    groups: str = Form(""),
    capabilities: str = Form(""),
    expose_to_llm: str = Form(""),
    output_as_file: str = Form(""),
    enabled: str = Form(""),
) -> Response:
    templates = request.app.state.templates
    payload = _create_payload(
        agent_id, description, preset_name, system_prompt, user_prompt,
        parameters, groups, capabilities,
        expose_to_llm in ("on", "true", "1"),
        output_as_file in ("on", "true", "1"),
        enabled in ("on", "true", "1"),
    )
    try:
        await upstream(request).admin_request(
            "POST", "/custom-agents",
            access_token=access_token(request), json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "custom_agents/form.html",
            {
                "active_section": "custom_agents",
                "mode": "new",
                "form": _form_echo(payload, agent_id.strip()),
                "presets": await _preset_names(request),
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )
    return redirect_with_flash(
        request, "/admin/custom-agents",
        f"custom agent {payload['agent_id']!r} created",
    )


@router.post("/{agent_id}/edit", response_class=HTMLResponse)
async def update_custom_agent(
    request: Request,
    agent_id: str,  # full custom_<slug> from the path
    description: str = Form(""),
    preset_name: str = Form(...),
    system_prompt: str = Form(""),
    user_prompt: str = Form(""),
    parameters: str = Form(""),
    groups: str = Form(""),
    capabilities: str = Form(""),
    expose_to_llm: str = Form(""),
    output_as_file: str = Form(""),
    enabled: str = Form(""),
) -> Response:
    templates = request.app.state.templates
    slug = agent_id[len("custom_"):] if agent_id.startswith("custom_") else agent_id
    # Reuse the create shaper, then drop the immutable agent_id for the PATCH.
    payload = _create_payload(
        slug, description, preset_name, system_prompt, user_prompt,
        parameters, groups, capabilities,
        expose_to_llm in ("on", "true", "1"),
        output_as_file in ("on", "true", "1"),
        enabled in ("on", "true", "1"),
    )
    payload.pop("agent_id")
    try:
        await upstream(request).admin_request(
            "PATCH", f"/custom-agents/{agent_id}",
            access_token=access_token(request), json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "custom_agents/form.html",
            {
                "active_section": "custom_agents",
                "mode": "edit",
                "agent": {"agent_id": agent_id},
                "form": _form_echo({**payload, "agent_id": agent_id}, slug),
                "presets": await _preset_names(request),
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )
    return redirect_with_flash(
        request, "/admin/custom-agents",
        f"custom agent {agent_id!r} updated",
    )


# ---------------------------------------------------------------------------
# Remove / Reconnect
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/delete", response_class=HTMLResponse)
async def delete_custom_agent(request: Request, agent_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "DELETE", f"/custom-agents/{agent_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/custom-agents", detail_message(exc),
        )
    return redirect_with_flash(
        request, "/admin/custom-agents", f"custom agent {agent_id!r} deleted",
    )


@router.post("/{agent_id}/reconnect", response_class=HTMLResponse)
async def reconnect_custom_agent(request: Request, agent_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "POST", f"/custom-agents/{agent_id}/reconnect",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/custom-agents", detail_message(exc),
        )
    return redirect_with_flash(
        request,
        "/admin/custom-agents",
        f"reconnect requested for {agent_id!r} — bridge will re-onboard on next poll",
    )
