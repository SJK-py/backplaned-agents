"""bp_admin.pages.code_agents — operator-authored Python agent config UI.

Mounts under `/admin/code-agents`. Wraps:
  - GET    /v1/admin/code-agents
  - GET    /v1/admin/code-agents/{agent_id}
  - POST   /v1/admin/code-agents
  - PATCH  /v1/admin/code-agents/{agent_id}
  - DELETE /v1/admin/code-agents/{agent_id}
  - POST   /v1/admin/code-agents/{agent_id}/reconnect

This UI manages config only; the `bp_mcp_bridge` process is the runtime
that turns each row into one live agent (`code_<slug>`, a single mode whose
handler runs the operator's function in a uid-dropped subprocess).

Mirrors `custom_agents.py` deliberately — same parse helpers, same
form-echo-on-error shape — so an operator moving between the two kinds
meets the same page. What differs is a code textarea instead of prompt
textareas, typed parameters, and the secret-reference rows.

See `docs/design/bridge-python-code-agents.md`.
"""

from __future__ import annotations

import json
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

# Offered in the parameter editor's type dropdown. Mirrors the router's
# `_CODE_PARAM_TYPES`; the router is the authority and rejects anything else.
PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")

_STARTER_CODE = '''\
def run(params, context):
    """Called once per invocation.

    params  — the declared parameters, already validated against the schema.
    context — {"task_id", "user_id", "session_id", "agent_id"}, informational.

    Return a string (used as-is) or any JSON-serialisable value.
    Secrets you declared below arrive as environment variables.
    """
    return "hello from " + context["agent_id"]
'''


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


def _parse_parameters_json(raw: str) -> list[dict[str, Any]]:
    """Parse the parameter editor's hidden JSON field into normalised dicts.
    Blank-name rows are dropped; an unknown `type` falls back to `string` and
    the router has the final say. A malformed blob degrades to an empty list
    rather than 500-ing."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        ptype = str(item.get("type", "string")).strip() or "string"
        out.append({
            "name": name,
            "type": ptype if ptype in PARAM_TYPES else "string",
            "description": str(item.get("description", "")).strip(),
            "required": bool(item.get("required", True)),
        })
    return out


def _parse_secret_refs_json(raw: str) -> dict[str, str]:
    """Parse the secret-reference editor into `{ENV_NAME: "env://VAR"}`.

    A bare `VAR` is normalised to `env://VAR`: operators reach for the plain
    name, and silently accepting it as a LITERAL secret would be exactly the
    mistake the router refuses. Normalising means the refusal only fires for
    something that genuinely looks like a pasted value."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return {}
    out: dict[str, str] = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        ref = str(item.get("ref", "")).strip()
        if not name or not ref:
            continue
        if not ref.startswith(("env://", "secret://")):
            ref = f"env://{ref}"
        out[name] = ref
    return out


def _secret_rows(refs: dict[str, str]) -> list[dict[str, str]]:
    """`{ENV_NAME: ref}` → the editor's row list (order-stable by name)."""
    return [{"name": k, "ref": v} for k, v in sorted(refs.items())]


def _parse_returns_json(raw: str) -> dict[str, Any] | None:
    """The optional output schema. Empty / blank → None (no schema declared);
    a malformed blob is passed through as-is so the ROUTER reports it rather
    than the UI silently dropping what the operator typed."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"__invalid__": text}
    return parsed if isinstance(parsed, dict) else {"__invalid__": text}


def _int_or(raw: str, default: int) -> int:
    """Coerce a form int field; the router validates the range."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_code_agents(request: Request) -> HTMLResponse:
    try:
        agents = await upstream(request).admin_request(
            "GET", "/code-agents", access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=is_htmx(request),
            active_section="code_agents",
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "code_agents/list.html",
        {
            "active_section": "code_agents",
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
        "code": _STARTER_CODE,
        "entrypoint": "run",
        "parameters": [],
        "returns": "",
        "secret_refs": [],
        "timeout_s": 30,
        "memory_mb": 512,
        "groups": "",
        "capabilities": "",
        "expose_to_llm": True,
        "output_as_file": False,
        "enabled": True,
    }


def _form_from_agent(a: dict[str, Any]) -> dict[str, Any]:
    # The stored agent_id is the full `code_<slug>`; the form edits the bare
    # slug (the prefix is fixed + shown next to the input).
    agent_id = a["agent_id"]
    slug = agent_id[len("code_"):] if agent_id.startswith("code_") else agent_id
    returns = a.get("returns")
    return {
        "agent_id": slug,
        "description": a.get("description") or "",
        "code": a.get("code") or "",
        "entrypoint": a.get("entrypoint") or "run",
        "parameters": a.get("parameters") or [],
        "returns": json.dumps(returns, indent=2) if returns else "",
        "secret_refs": _secret_rows(a.get("secret_refs") or {}),
        "timeout_s": a.get("timeout_s", 30),
        "memory_mb": a.get("memory_mb", 512),
        "groups": ", ".join(a.get("groups") or []),
        "capabilities": ", ".join(a.get("capabilities") or []),
        "expose_to_llm": a.get("expose_to_llm", True),
        "output_as_file": a.get("output_as_file", False),
        "enabled": a.get("enabled", True),
    }


@router.get("/new", response_class=HTMLResponse)
async def new_code_agent_form(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "code_agents/form.html",
        {
            "active_section": "code_agents",
            "mode": "new",
            "form": _empty_form(),
            "param_types": PARAM_TYPES,
            "error": None,
        },
    )


@router.get("/{agent_id}/edit", response_class=HTMLResponse)
async def edit_code_agent_form(request: Request, agent_id: str) -> HTMLResponse:
    try:
        agent = await upstream(request).admin_request(
            "GET", f"/code-agents/{agent_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return error_response(
            request, exc, partial=False, active_section="code_agents",
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "code_agents/form.html",
        {
            "active_section": "code_agents",
            "mode": "edit",
            "agent": agent,
            "form": _form_from_agent(agent),
            "param_types": PARAM_TYPES,
            "error": None,
        },
    )


def _create_payload(
    slug: str,
    description: str,
    code: str,
    entrypoint: str,
    parameters_json: str,
    returns_json: str,
    secret_refs_json: str,
    timeout_s: str,
    memory_mb: str,
    groups: str,
    capabilities: str,
    expose_to_llm: bool,
    output_as_file: bool,
    enabled: bool,
) -> dict[str, Any]:
    return {
        "agent_id": f"code_{slug.strip()}",
        "description": description.strip(),
        "code": code,
        "entrypoint": entrypoint.strip() or "run",
        "parameters": _parse_parameters_json(parameters_json),
        "returns": _parse_returns_json(returns_json),
        "secret_refs": _parse_secret_refs_json(secret_refs_json),
        "timeout_s": _int_or(timeout_s, 30),
        "memory_mb": _int_or(memory_mb, 512),
        "groups": _parse_groups(groups),
        "capabilities": _parse_capabilities(capabilities),
        "expose_to_llm": expose_to_llm,
        "output_as_file": output_as_file,
        "enabled": enabled,
    }


def _form_echo(payload: dict[str, Any], slug: str) -> dict[str, Any]:
    """Rebuild the form dict from a rejected payload so the admin's inputs
    survive the re-render — including the code body, which is the one field
    nobody wants to retype."""
    returns = payload.get("returns")
    return {
        "agent_id": slug,
        "description": payload["description"],
        "code": payload["code"],
        "entrypoint": payload["entrypoint"],
        "parameters": payload["parameters"],
        "returns": json.dumps(returns, indent=2) if returns else "",
        "secret_refs": _secret_rows(payload["secret_refs"]),
        "timeout_s": payload["timeout_s"],
        "memory_mb": payload["memory_mb"],
        "groups": ", ".join(payload["groups"]),
        "capabilities": ", ".join(payload["capabilities"]),
        "expose_to_llm": payload["expose_to_llm"],
        "output_as_file": payload["output_as_file"],
        "enabled": payload["enabled"],
    }


_TRUTHY = ("on", "true", "1")


@router.post("/new", response_class=HTMLResponse)
async def create_code_agent(
    request: Request,
    agent_id: str = Form(...),  # bare slug; the page prepends code_
    description: str = Form(""),
    code: str = Form(""),
    entrypoint: str = Form("run"),
    parameters_json: str = Form("[]"),
    returns_json: str = Form(""),
    secret_refs_json: str = Form("[]"),
    timeout_s: str = Form("30"),
    memory_mb: str = Form("512"),
    groups: str = Form(""),
    capabilities: str = Form(""),
    expose_to_llm: str = Form(""),
    output_as_file: str = Form(""),
    enabled: str = Form(""),
) -> Response:
    templates = request.app.state.templates
    payload = _create_payload(
        agent_id, description, code, entrypoint, parameters_json,
        returns_json, secret_refs_json, timeout_s, memory_mb,
        groups, capabilities,
        expose_to_llm in _TRUTHY,
        output_as_file in _TRUTHY,
        enabled in _TRUTHY,
    )
    try:
        await upstream(request).admin_request(
            "POST", "/code-agents",
            access_token=access_token(request), json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "code_agents/form.html",
            {
                "active_section": "code_agents",
                "mode": "new",
                "form": _form_echo(payload, agent_id.strip()),
                "param_types": PARAM_TYPES,
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )
    return redirect_with_flash(
        request, "/admin/code-agents",
        f"code agent {payload['agent_id']!r} created",
    )


@router.post("/{agent_id}/edit", response_class=HTMLResponse)
async def update_code_agent(
    request: Request,
    agent_id: str,  # full code_<slug> from the path
    description: str = Form(""),
    code: str = Form(""),
    entrypoint: str = Form("run"),
    parameters_json: str = Form("[]"),
    returns_json: str = Form(""),
    secret_refs_json: str = Form("[]"),
    timeout_s: str = Form("30"),
    memory_mb: str = Form("512"),
    groups: str = Form(""),
    capabilities: str = Form(""),
    expose_to_llm: str = Form(""),
    output_as_file: str = Form(""),
    enabled: str = Form(""),
) -> Response:
    templates = request.app.state.templates
    slug = agent_id[len("code_"):] if agent_id.startswith("code_") else agent_id
    payload = _create_payload(
        slug, description, code, entrypoint, parameters_json,
        returns_json, secret_refs_json, timeout_s, memory_mb,
        groups, capabilities,
        expose_to_llm in _TRUTHY,
        output_as_file in _TRUTHY,
        enabled in _TRUTHY,
    )
    payload.pop("agent_id")
    # PATCH treats None as "leave alone", so a cleared schema box would be a
    # no-op. `{}` is the router's CLEAR sentinel — send that instead.
    if payload["returns"] is None:
        payload["returns"] = {}
    try:
        await upstream(request).admin_request(
            "PATCH", f"/code-agents/{agent_id}",
            access_token=access_token(request), json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "code_agents/form.html",
            {
                "active_section": "code_agents",
                "mode": "edit",
                "agent": {"agent_id": agent_id},
                "form": _form_echo({**payload, "agent_id": agent_id}, slug),
                "param_types": PARAM_TYPES,
                "error": detail_message(exc),
            },
            status_code=exc.status_code,
        )
    return redirect_with_flash(
        request, "/admin/code-agents", f"code agent {agent_id!r} updated",
    )


# ---------------------------------------------------------------------------
# Remove / Reconnect
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/delete", response_class=HTMLResponse)
async def delete_code_agent(request: Request, agent_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "DELETE", f"/code-agents/{agent_id}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/code-agents", detail_message(exc),
        )
    return redirect_with_flash(
        request, "/admin/code-agents", f"code agent {agent_id!r} deleted",
    )


@router.post("/{agent_id}/reconnect", response_class=HTMLResponse)
async def reconnect_code_agent(request: Request, agent_id: str) -> Response:
    try:
        await upstream(request).admin_request(
            "POST", f"/code-agents/{agent_id}/reconnect",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/code-agents", detail_message(exc),
        )
    return redirect_with_flash(
        request,
        "/admin/code-agents",
        f"reconnect requested for {agent_id!r} — bridge will re-onboard on next poll",
    )
