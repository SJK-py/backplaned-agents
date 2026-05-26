"""Dispatch + tool-schema under the unified mode model.

Routing is an O(1) lookup by `frame.input_mode` — there is no
`_pick_handler_by_payload` structural probe any more (that whole
HIGH-severity broad-except concern is deleted along with the
mechanism). `build_tools` emits ONE tool per (agent, tool-visible
mode); `tools.resolve_tool_name` is the inverse, sharing the exact
same flattening so forward (build) and reverse (dispatch) can't
drift. Per-mode schemas are plain object schemas (no `oneOf`).
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext


class A(BaseModel):
    a: int


class B(BaseModel):
    b: str


# ===========================================================================
# 1. Mode dispatch (replaces _pick_handler_by_payload)
# ===========================================================================


def test_pick_handler_by_payload_is_gone() -> None:
    """The structural-probe resolver and its broad-except hazard are
    deleted — routing is by explicit mode now."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    assert not hasattr(Dispatcher, "_pick_handler_by_payload")
    src = inspect.getsource(Dispatcher._resolve_handler_for)
    assert "resolve_handler(mode=frame.input_mode)" in src


def test_resolve_handler_is_o1_mode_lookup() -> None:
    a = Agent(info=AgentInfo(agent_id="x", description="d"))

    @a.handler
    async def ha(ctx: TaskContext, p: A) -> AgentOutput: ...

    @a.handler(mode="bee")
    async def hb(ctx: TaskContext, p: B) -> AgentOutput: ...

    assert a.resolve_handler(mode="A").fn is ha
    assert a.resolve_handler(mode="bee").fn is hb
    assert a.resolve_handler(mode="missing") is None  # → no_handler
    # No order-dependence: a payload that would structurally validate
    # as BOTH still resolves deterministically by mode key.
    assert a.resolve_handler(mode="A").input_model is A


def test_resolve_none_only_when_unambiguous() -> None:
    a = Agent(info=AgentInfo(agent_id="x", description="d"))

    @a.handler
    async def only(ctx: TaskContext, p: A) -> AgentOutput: ...

    assert a.resolve_handler(mode=None).fn is only

    @a.handler
    async def two(ctx: TaskContext, p: B) -> AgentOutput: ...

    assert a.resolve_handler(mode=None) is None


# ===========================================================================
# 2. _schema_for — per-mode, permissive fallback
# ===========================================================================


def test_schema_for_passes_object_schema_through() -> None:
    from bp_sdk.tools import _schema_for

    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    assert _schema_for(schema) == schema


def test_schema_for_accepts_properties_without_type() -> None:
    from bp_sdk.tools import _schema_for

    schema = {"properties": {"x": {"type": "string"}}}
    assert _schema_for(schema) == schema


def test_schema_for_permissive_on_none_or_garbage() -> None:
    """A `null` per-mode entry (dict-input mode) or an
    unrecognisable value → permissive object so the LLM still gets a
    callable tool."""
    from bp_sdk.tools import _schema_for

    for bad in (None, {"foo": "bar"}, "nope", 42):
        out = _schema_for(bad)
        assert out["type"] == "object"
        assert out["additionalProperties"] is True


# ===========================================================================
# 3. _tool_specs / resolve_tool_name — per-mode tools, round-trip
# ===========================================================================


def _catalog(agent_id: str, accepts_schema, non_tool_modes=None):  # type: ignore[no-untyped-def]
    return {
        agent_id: {
            "description": "d",
            "accepts_schema": accepts_schema,
            "non_tool_modes": non_tool_modes or [],
        }
    }


def test_single_mode_keeps_bare_call_name() -> None:
    from bp_sdk.tools import _tool_specs

    cat = _catalog("svc", {"Only": {"type": "object", "properties": {}}})
    specs = _tool_specs(cat)
    assert len(specs) == 1
    tool_name, agent_id, mode, _params, _desc = specs[0]
    assert tool_name == "call_svc"
    assert (agent_id, mode) == ("svc", "Only")


def test_multi_mode_suffixes_the_mode() -> None:
    from bp_sdk.tools import _tool_specs

    cat = _catalog("svc", {
        "Alpha": {"type": "object", "properties": {}},
        "Beta": {"type": "object", "properties": {}},
    })
    names = {t[0] for t in _tool_specs(cat)}
    assert names == {"call_svc_Alpha", "call_svc_Beta"}


def test_non_tool_modes_excluded() -> None:
    from bp_sdk.tools import _tool_specs

    cat = _catalog(
        "svc",
        {"Pub": {"type": "object", "properties": {}},
         "Secret": {"type": "object", "properties": {}}},
        non_tool_modes=["Secret"],
    )
    specs = _tool_specs(cat)
    # One visible mode left → back-compat bare name.
    assert [(t[0], t[2]) for t in specs] == [("call_svc", "Pub")]


def test_no_schema_map_yields_one_permissive_tool() -> None:
    from bp_sdk.tools import _tool_specs

    for accepts in (None, {}, "legacy"):
        cat = _catalog("svc", accepts)
        specs = _tool_specs(cat)
        assert len(specs) == 1
        tool_name, _aid, mode, params, _d = specs[0]
        assert tool_name == "call_svc"
        assert mode is None
        assert params["additionalProperties"] is True


def test_resolve_tool_name_round_trips_build() -> None:
    from bp_sdk.tools import _tool_specs, resolve_tool_name

    cat = _catalog("svc", {
        "Alpha": {"type": "object", "properties": {}},
        "Beta": {"type": "object", "properties": {}},
    })
    for tool_name, agent_id, mode, _p, _d in _tool_specs(cat):
        assert resolve_tool_name(cat, tool_name) == (agent_id, mode)
    assert resolve_tool_name(cat, "call_hallucinated") is None


# ===========================================================================
# 4. Adapters emit per-mode tools
# ===========================================================================


def test_anthropic_adapter_one_tool_per_mode() -> None:
    from bp_sdk.tools import build_tools

    cat = _catalog("svc", {
        "Alpha": {"type": "object", "properties": {"a": {"type": "integer"}}},
        "Beta": {"type": "object", "properties": {"b": {"type": "string"}}},
    })
    tools = build_tools(cat, provider="anthropic")
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"call_svc_Alpha", "call_svc_Beta"}
    assert by_name["call_svc_Alpha"]["input_schema"]["properties"] == {
        "a": {"type": "integer"}
    }


def test_openai_and_gemini_adapters_per_mode() -> None:
    from bp_sdk.tools import build_tools

    cat = _catalog("svc", {
        "Alpha": {"type": "object", "properties": {}},
        "Beta": {"type": "object", "properties": {}},
    })
    oai = {t["function"]["name"] for t in build_tools(cat, provider="openai")}
    assert oai == {"call_svc_Alpha", "call_svc_Beta"}
    gem = build_tools(cat, provider="gemini")
    decls = gem[0]["function_declarations"]
    assert {d["name"] for d in decls} == {"call_svc_Alpha", "call_svc_Beta"}


def test_gemini_strip_schema_still_collapses_oneof() -> None:
    """`gemini_strip_schema` keeps its defensive oneOf collapse (a
    pinned operator schema could still be a union even though
    auto-derived per-mode schemas never are)."""
    from bp_sdk.tools import gemini_strip_schema

    out = gemini_strip_schema({"oneOf": [{"type": "object"}]})
    assert out == {"type": "object", "properties": {}}
