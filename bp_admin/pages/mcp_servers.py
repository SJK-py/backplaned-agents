"""bp_admin.pages.mcp_servers — MCP bridge config UI.

Mounts under `/admin/mcp-servers`. Wraps:
  - GET    /v1/admin/mcp-servers
  - GET    /v1/admin/mcp-servers/{server_id}
  - POST   /v1/admin/mcp-servers
  - PATCH  /v1/admin/mcp-servers/{server_id}
  - DELETE /v1/admin/mcp-servers/{server_id}
  - POST   /v1/admin/mcp-servers/{server_id}/refresh-tools

This UI manages config only; the `bp_mcp_bridge` process is the
runtime that turns each row into one live agent (`mcp_<server>`, one mode
per tool). The bridge is not part of the default deployment yet — run it
separately (`python -m bp_mcp_bridge`) to bring configured servers online.
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


TRANSPORT_OPTIONS = ("sse", "streamable_http")
AUTH_KIND_OPTIONS = ("none", "bearer", "header")


def _parse_groups(raw: str) -> list[str]:
    """Comma-separated group list from the form, stripped + deduped
    while preserving order."""
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


def _tool_names(server: dict[str, Any]) -> list[str]:
    """Tool names from the server's `tools_cache` (the full upstream list), for
    rendering the per-tool enable/disable checkboxes. Empty before first
    connect."""
    cache = server.get("tools_cache") or {}
    tools = cache.get("tools") if isinstance(cache, dict) else None
    if not isinstance(tools, list):
        return []
    return [t["name"] for t in tools if isinstance(t, dict) and t.get("name")]


def _tools_count(server: dict[str, Any]) -> int | None:
    """Pull a tools count out of `tools_cache` when present.

    The bridge writes the raw MCP `tools/list` response shape:
        {"tools": [{"name": ..., "description": ..., "inputSchema": ...}, ...]}
    Returns None when the bridge hasn't populated the cache yet
    (admin-side render distinguishes "0 tools" from "not yet
    refreshed")."""
    cache = server.get("tools_cache") or {}
    tools = cache.get("tools") if isinstance(cache, dict) else None
    return len(tools) if isinstance(tools, list) else None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_mcp_servers(request: Request) -> HTMLResponse:
    try:
        servers = await upstream(request).admin_request(
            "GET", "/mcp-servers", access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=is_htmx(request),
            active_section="mcp_servers",
        )

    # Surface a tool count alongside each row.
    for s in servers:
        s["_tools_count"] = _tools_count(s)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "mcp_servers/list.html",
        {
            "active_section": "mcp_servers",
            "servers": servers,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# New / Edit
# ---------------------------------------------------------------------------


def _empty_form() -> dict[str, Any]:
    return {
        "server_id": "",
        "description": "",
        "url": "",
        "transport": "sse",
        "auth_kind": "none",
        "auth_value_ref": "",
        "auth_header_name": "",
        "groups": "",
        "capabilities": "",
        "expose_to_llm": True,
    }


def _form_from_server(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "server_id": s["server_id"],
        "description": s.get("description") or "",
        "url": s["url"],
        "transport": s["transport"],
        "auth_kind": s["auth_kind"],
        "auth_value_ref": s.get("auth_value_ref") or "",
        "auth_header_name": s.get("auth_header_name") or "",
        "groups": ", ".join(s.get("groups") or []),
        "capabilities": ", ".join(s.get("capabilities") or []),
        "expose_to_llm": s.get("expose_to_llm", True),
    }


@router.get("/new", response_class=HTMLResponse)
async def new_mcp_server_form(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "mcp_servers/form.html",
        {
            "active_section": "mcp_servers",
            "mode": "new",
            "form": _empty_form(),
            "tools": [],          # none until the bridge connects
            "disabled_tools": [],
            "transport_options": TRANSPORT_OPTIONS,
            "auth_kind_options": AUTH_KIND_OPTIONS,
            "error": None,
        },
    )


@router.get("/{server_id}/edit", response_class=HTMLResponse)
async def edit_mcp_server_form(request: Request, server_id: str) -> HTMLResponse:
    try:
        server = await upstream(request).admin_request(
            "GET", f"/mcp-servers/{server_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=False, active_section="mcp_servers",
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "mcp_servers/form.html",
        {
            "active_section": "mcp_servers",
            "mode": "edit",
            "server": server,
            "form": _form_from_server(server),
            "tools": _tool_names(server),
            "disabled_tools": server.get("disabled_tools") or [],
            "transport_options": TRANSPORT_OPTIONS,
            "auth_kind_options": AUTH_KIND_OPTIONS,
            "error": None,
        },
    )


def _build_payload(
    server_id: str,
    description: str,
    url: str,
    transport: str,
    auth_kind: str,
    auth_value_ref: str,
    auth_header_name: str,
    groups: str,
    capabilities: str,
    expose_to_llm: bool,
) -> dict[str, Any]:
    """Common form → payload builder. `auth_value_ref` /
    `auth_header_name` are null-when-empty so the auth_kind=none
    case sends them as null (the router CHECK requires it)."""
    return {
        "server_id": server_id.strip(),
        "description": description.strip(),
        "url": url.strip(),
        "transport": transport,
        "auth_kind": auth_kind,
        "auth_value_ref": auth_value_ref.strip() or None,
        "auth_header_name": auth_header_name.strip() or None,
        "groups": _parse_groups(groups),
        "capabilities": _parse_capabilities(capabilities),
        "expose_to_llm": expose_to_llm,
    }


@router.post("/new", response_class=HTMLResponse)
async def create_mcp_server(
    request: Request,
    server_id: str = Form(...),
    description: str = Form(""),
    url: str = Form(...),
    transport: str = Form(...),
    auth_kind: str = Form("none"),
    auth_value_ref: str = Form(""),
    auth_header_name: str = Form(""),
    groups: str = Form(""),
    capabilities: str = Form(""),
    expose_to_llm: str = Form(""),
) -> Response:
    templates = request.app.state.templates
    expose_bool = expose_to_llm in ("on", "true", "1")
    payload = _build_payload(
        server_id, description, url, transport, auth_kind,
        auth_value_ref, auth_header_name, groups, capabilities, expose_bool,
    )
    try:
        await upstream(request).admin_request(
            "POST", "/mcp-servers",
            access_token=access_token(request), json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "mcp_servers/form.html",
            {
                "active_section": "mcp_servers",
                "mode": "new",
                "form": {
                    "server_id": payload["server_id"],
                    "description": payload["description"],
                    "url": payload["url"],
                    "transport": payload["transport"],
                    "auth_kind": payload["auth_kind"],
                    "auth_value_ref": payload["auth_value_ref"] or "",
                    "auth_header_name": payload["auth_header_name"] or "",
                    "groups": ", ".join(payload["groups"]),
                    "capabilities": ", ".join(payload["capabilities"]),
                    "expose_to_llm": payload["expose_to_llm"],
                },
                "tools": [],
                "disabled_tools": [],
                "transport_options": TRANSPORT_OPTIONS,
                "auth_kind_options": AUTH_KIND_OPTIONS,
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )
    return redirect_with_flash(
        request, "/admin/mcp-servers", f"mcp server {payload['server_id']!r} created",
    )


@router.post("/{server_id}/edit", response_class=HTMLResponse)
async def update_mcp_server(
    request: Request,
    server_id: str,
    description: str = Form(""),
    url: str = Form(...),
    transport: str = Form(...),
    auth_kind: str = Form("none"),
    auth_value_ref: str = Form(""),
    auth_header_name: str = Form(""),
    groups: str = Form(""),
    capabilities: str = Form(""),
    expose_to_llm: str = Form(""),
    enabled_tool: list[str] = Form(default=[]),
    all_tools: str = Form(""),
) -> Response:
    """PATCH the server. The form posts ALL fields back; we forward
    only the ones that semantically apply (auth_kind=none gets
    null auth_value_ref / auth_header_name)."""
    templates = request.app.state.templates
    expose_bool = expose_to_llm in ("on", "true", "1")
    # Per-tool toggle: the form submits the FULL tool list (hidden) + a checkbox
    # per ENABLED tool. The disabled set is the complement.
    all_names = [t for t in all_tools.split(",") if t]
    disabled_tools = [t for t in all_names if t not in set(enabled_tool)]
    body: dict[str, Any] = {
        "description": description.strip(),
        "url": url.strip(),
        "transport": transport,
        "auth_kind": auth_kind,
        # auth_kind drives the credential surface; the router's
        # PATCH expects ref/header alongside any auth_kind change.
        "auth_value_ref": auth_value_ref.strip() or None,
        "auth_header_name": auth_header_name.strip() or None,
        "groups": _parse_groups(groups),
        "capabilities": _parse_capabilities(capabilities),
        "expose_to_llm": expose_bool,
        "disabled_tools": disabled_tools,
    }
    try:
        await upstream(request).admin_request(
            "PATCH", f"/mcp-servers/{server_id}",
            access_token=access_token(request), json=body,
        )
    except UpstreamError as exc:
        # Re-render the form with the admin's inputs so they can fix
        # the error without re-typing.
        return templates.TemplateResponse(
            request,
            "mcp_servers/form.html",
            {
                "active_section": "mcp_servers",
                "mode": "edit",
                "server": {"server_id": server_id},
                "form": {
                    "server_id": server_id,
                    "description": body["description"],
                    "url": body["url"],
                    "transport": body["transport"],
                    "auth_kind": body["auth_kind"],
                    "auth_value_ref": body["auth_value_ref"] or "",
                    "auth_header_name": body["auth_header_name"] or "",
                    "groups": ", ".join(body["groups"]),
                    "capabilities": ", ".join(body["capabilities"]),
                    "expose_to_llm": body["expose_to_llm"],
                },
                # Reconstruct the checkbox state from the submission so the
                # admin's toggles survive the re-render.
                "tools": all_names,
                "disabled_tools": disabled_tools,
                "transport_options": TRANSPORT_OPTIONS,
                "auth_kind_options": AUTH_KIND_OPTIONS,
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )
    return redirect_with_flash(
        request, "/admin/mcp-servers", f"mcp server {server_id!r} updated",
    )


# ---------------------------------------------------------------------------
# Remove / Refresh-tools
# ---------------------------------------------------------------------------


@router.post("/{server_id}/delete", response_class=HTMLResponse)
async def delete_mcp_server(request: Request, server_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "DELETE", f"/mcp-servers/{server_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/mcp-servers", detail_message(exc),
        )
    return redirect_with_flash(
        request, "/admin/mcp-servers", f"mcp server {server_id!r} deleted",
    )


@router.post("/{server_id}/refresh-tools", response_class=HTMLResponse)
async def refresh_tools(request: Request, server_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "POST", f"/mcp-servers/{server_id}/refresh-tools",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/mcp-servers", detail_message(exc),
        )
    return redirect_with_flash(
        request,
        "/admin/mcp-servers",
        f"refresh requested for {server_id!r} — bridge will re-fetch tools on next poll",
    )
