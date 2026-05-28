"""bp_sdk.tools — Build provider-specific tool schemas from AgentInfo.

Provider adapters register here; new providers are added by registering
a `ToolFormatAdapter`, not by forking a function.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


ToolFormatAdapter = Callable[[dict[str, Any]], list[dict[str, Any]]]
"""Builds the provider-specific tool schemas from `available_destinations`.

Input dict shape from `WelcomeFrame.available_destinations`:
    { agent_id: { description, capabilities, tags, accepts_schema, hidden, ... }, ... }
"""


_ADAPTERS: dict[str, ToolFormatAdapter] = {}


def register_provider(name: str, adapter: ToolFormatAdapter) -> None:
    _ADAPTERS[name] = adapter


def build_tools(
    destinations: dict[str, Any],
    *,
    provider: Literal["anthropic", "openai", "gemini"],
    user_level: str | None = None,
) -> list[dict[str, Any]]:
    """Build tool definitions for an LLM call.

    Excluded:
      - agents with `hidden: true`.
      - if `user_level` is supplied, agents whose `callable_user_levels`
        does not contain it (i.e., the user can't invoke them anyway).
    """
    visible = {
        k: v
        for k, v in destinations.items()
        if not v.get("hidden")
        and (user_level is None or user_level in v.get("callable_user_levels", []))
    }
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return adapter(visible)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


_PERMISSIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


def _schema_for(schema: Any) -> dict[str, Any]:
    """A single mode's parameter schema. `None` (a `dict`-input
    mode — no published shape) or an unrecognisable value falls back
    to a permissive object. Per-mode schemas are plain object
    schemas now (no `oneOf` — routing is by explicit mode, so each
    tool advertises exactly one mode's shape)."""
    if isinstance(schema, dict) and (
        schema.get("type") == "object" or "properties" in schema
    ):
        return schema
    return dict(_PERMISSIVE_SCHEMA)


def _tool_specs(
    destinations: dict[str, Any],
) -> list[tuple[str, str, str | None, dict[str, Any], str]]:
    """Flatten the catalog into ONE tool spec per (agent, tool-
    visible mode): `(tool_name, agent_id, mode, params, description)`.

    `accepts_schema` is `{mode: schema|null}`. Modes listed in the
    agent's `non_tool_modes` (control-plane) are excluded. Tool
    naming: a single tool-visible mode keeps the back-compatible
    `call_<agent_id>` (no churn for the common one-handler agent);
    multi-mode agents get `call_<agent_id>_<mode>`. An agent with no
    per-mode map (legacy / operator-cleared / all-dict-no-schema)
    yields one permissive `call_<agent_id>` tool with `mode=None`
    (the producer's `input_mode=None` then resolves to the sole
    handler).

    Shared by every provider adapter AND `resolve_tool_name`, so the
    forward (build) and reverse (dispatch) mappings can never drift.
    """
    out: list[tuple[str, str, str | None, dict[str, Any], str]] = []
    for agent_id, entry in destinations.items():
        accepts = entry.get("accepts_schema")
        non_tool = set(entry.get("non_tool_modes") or [])
        desc = _description(entry)
        mode_descs = entry.get("mode_descriptions") or {}
        if isinstance(accepts, dict) and accepts:
            modes = [(m, s) for m, s in accepts.items() if m not in non_tool]
            if not modes:
                # The agent HAS a per-mode map but every mode is
                # control-plane (`tool=False` → `non_tool_modes`).
                # It has NO tool-visible surface: emit nothing. NOT
                # the permissive fallback below — that would re-leak
                # the hidden surface as `call_<agent_id>` and, for a
                # single-mode agent, the router would even admit it
                # and run the hidden handler. This is the whole point
                # of `tool=False`.
                continue
        else:
            # No per-mode map at all (legacy / operator-cleared /
            # all-dict-no-schema) — single permissive, mode-agnostic
            # tool so the agent stays callable.
            out.append(
                (_safe_tool_name(agent_id), agent_id, None,
                 dict(_PERMISSIVE_SCHEMA), desc)
            )
            continue
        multi = len(modes) > 1
        for mode, schema in modes:
            tool_name = (
                _safe_tool_name(f"{agent_id}_{mode}")
                if multi
                else _safe_tool_name(agent_id)
            )
            # Per-mode description (from AgentInfo.mode_descriptions) wins
            # over the agent-level one; the capabilities suffix is appended
            # to whichever is used.
            mode_desc = _description(entry, override=mode_descs.get(mode))
            out.append(
                (tool_name, agent_id, mode, _schema_for(schema), mode_desc)
            )
    return out


def resolve_tool_name(
    destinations: dict[str, Any], tool_name: str
) -> tuple[str, str | None] | None:
    """Reverse a build_tools tool name back to `(agent_id, mode)`
    using the SAME flattening as the adapters — no fragile string
    parsing (agent_id and mode both admit `_`). Returns None when
    the name isn't a published tool."""
    for tname, agent_id, mode, _schema, _desc in _tool_specs(destinations):
        if tname == tool_name:
            return agent_id, mode
    return None


def _safe_tool_name(agent_id: str) -> str:
    """call_<agent_id> with characters scrubbed for provider name rules.

    Most providers require [A-Za-z0-9_-]{,64}. Replace illegal chars
    with '_'. Truncate to a sensible length.
    """
    raw = f"call_{agent_id}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]


def _description(entry: dict[str, Any], *, override: str | None = None) -> str:
    desc = override or entry.get("description", "")
    caps = entry.get("capabilities") or []
    if caps:
        desc += f" [capabilities: {', '.join(caps)}]"
    return desc


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _anthropic_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic Messages API tool format:
        { name, description, input_schema: <json schema> }
    One tool per (agent, tool-visible mode).
    """
    return [
        {
            "name": tool_name,
            "description": desc,
            "input_schema": params,
        }
        for tool_name, _agent_id, _mode, params, desc in _tool_specs(
            destinations
        )
    ]


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _openai_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenAI Chat Completions tool format:
        { type: "function", function: { name, description, parameters } }
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": desc,
                "parameters": params,
            },
        }
        for tool_name, _agent_id, _mode, params, desc in _tool_specs(
            destinations
        )
    ]


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _gemini_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """Gemini function-declarations format. The Google client expects:

        { function_declarations: [
            { name, description, parameters: <subset of JSON Schema> }
          ]
        }

    Multiple agents are bundled into a single function_declarations
    array, returned as a single-element list so the caller can append
    other tool blocks (e.g. {google_search:{}}).
    """
    declarations = [
        {
            "name": tool_name,
            "description": desc,
            "parameters": _gemini_strip_schema(params),
        }
        for tool_name, _agent_id, _mode, params, desc in _tool_specs(
            destinations
        )
    ]
    if not declarations:
        return []
    return [{"function_declarations": declarations}]


_GEMINI_ALLOWED_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minimum",
    "maximum",
    "min_items",
    "max_items",
    "min_length",
    "max_length",
}


def gemini_strip_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's function-declaration parameters accept only a subset of
    JSON Schema. Drop unsupported keys recursively and normalize the
    two-element `type: ["X", "null"]` shape that Pydantic emits for
    `Optional[X]` under JSON Schema 2020-12 into Gemini's
    `type=X, nullable=True` form.

    Soft-public: not in `__all__` but importable for provider adapters
    that emit Pydantic-derived schemas to Gemini. The router-side
    provider import expects this name.
    """
    if not isinstance(schema, dict):
        return schema  # type: ignore[return-value]
    # Gemini doesn't support oneOf / anyOf / allOf at the parameters
    # top level. Phase-5 multi-handler agents publish a union schema
    # — flatten to a permissive object so the function declaration
    # validates. Loss of branch-specific structural fidelity is
    # acceptable for the rare multi-handler case; the router's own
    # admission validator still uses the full union schema.
    if any(k in schema for k in ("oneOf", "anyOf", "allOf")):
        return {
            "type": "object",
            "properties": {},
        }
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _GEMINI_ALLOWED_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: gemini_strip_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = gemini_strip_schema(v)
        else:
            out[k] = v
    # `Optional[X]` → JSON Schema 2020-12 emits `type: ["X", "null"]`.
    # Gemini rejects list-typed `type` values; flatten the two-element
    # union with "null" to `type=X, nullable=True`. Longer unions are
    # left alone — Gemini has no representation for them.
    raw_type = out.get("type")
    if isinstance(raw_type, list) and len(raw_type) == 2:
        lower = [t.lower() if isinstance(t, str) else t for t in raw_type]
        if "null" in lower:
            non_null = next(
                (t for t, low in zip(raw_type, lower, strict=False) if low != "null"),
                None,
            )
            if isinstance(non_null, str):
                out["type"] = non_null
                out["nullable"] = True
    return out


# Backward-compatible alias for the previous private name.
_gemini_strip_schema = gemini_strip_schema


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


register_provider("anthropic", _anthropic_adapter)
register_provider("openai", _openai_adapter)
register_provider("gemini", _gemini_adapter)
