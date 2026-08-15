"""Shared machinery for the bridge's non-MCP agent kinds.

The bridge hosts three kinds now — MCP servers, custom LLM agents, and
Python code agents. The MCP path is its own shape (an upstream client, a
tool catalog, incremental reconcile); the other two are the same shape
with a different handler body, so what they share lives here rather than
being written twice and drifting.

Two things, both small and both load-bearing:

  * `object_schema` — the operator's parameter list → the single mode's
    `accepts_schema`. Shared so the two kinds cannot disagree about what
    `required` means or whether unknown properties are rejected.
  * `observed_call` — the timing/outcome wrapper around one handler
    invocation. Shared so a new kind is instrumented by construction
    rather than by remembering to be.

See `docs/design/bridge-python-code-agents.md` §11.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from bp_mcp_bridge import metrics

# `kind` label values for the shared non-MCP metrics.
KIND_CUSTOM = "custom"
KIND_CODE = "code"

# JSON-Schema types a parameter may declare. The custom LLM kind pins every
# param to `string` (its values are `$`-substituted into a prompt as inert
# text, so a type would buy nothing); the code kind allows the full set,
# because its handler receives a `dict` and the router already validates the
# payload against this schema at admit.
PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")


def object_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """One JSON-Schema object from an operator parameter list.

    Each entry is `{name, type?, description?, required?}`; `type` defaults
    to `string` (what the custom LLM kind pins every param to).
    `additionalProperties: false` is not optional — it is what makes the
    router reject a payload key the operator never declared, which is the
    difference between a schema and a suggestion.

    `required` is omitted entirely when empty rather than emitted as `[]`:
    some JSON-Schema validators treat an empty `required` array as invalid,
    and an absent key is unambiguous.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in parameters:
        name = p["name"]
        prop: dict[str, Any] = {"type": p.get("type") or "string"}
        desc = p.get("description") or ""
        if desc:
            prop["description"] = desc
        props[name] = prop
        if p.get("required", True):
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


@asynccontextmanager
async def observed_call(kind: str, agent_id: str):  # type: ignore[no-untyped-def]
    """Time one handler invocation and count its outcome.

    Wraps the whole handler body, so the histogram measures what the caller
    actually waited for. An exception is recorded as `failed` and re-raised
    — including `CancelledError`, which is a real outcome for a caller whose
    task was aborted and would otherwise vanish from the counts.
    """
    started = time.monotonic()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        metrics.agent_call_duration_seconds.labels(
            kind=kind, agent_id=agent_id,
        ).observe(time.monotonic() - started)
        metrics.agent_calls_total.labels(
            kind=kind, agent_id=agent_id, outcome=outcome,
        ).inc()
