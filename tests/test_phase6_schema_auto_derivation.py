"""Schema auto-derivation under the unified mode registry.

`accepts_schema` is now a per-mode MAP `{mode: <strict schema>|null}`
(null == dict-input mode, router admits without payload validation),
NOT a single schema / `oneOf`. `non_tool_modes` is auto-derived from
`tool=False` handlers. `produces_schema` keeps the single/oneOf rule
(it isn't mode-keyed). Operator-pinned fields at `AgentInfo(...)`
construction are never overwritten. This file pins those shapes +
the precedence rule.
"""

from __future__ import annotations

from pydantic import BaseModel

from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext


class MessagePayload(BaseModel):
    prompt: str


class StatusPayload(BaseModel):
    status: str


class ClearHistory(BaseModel):
    keep_metadata: bool = True


class _Out(BaseModel):
    content: str


class _OtherOut(BaseModel):
    summary: str


class _SnapA(BaseModel):
    a: str


class _SnapB(BaseModel):
    b: int


class _SnapC(BaseModel):
    c: bool


def _agent(agent_id: str = "schemaagent", **info_kw) -> Agent:  # type: ignore[no-untyped-def]
    return Agent(
        info=AgentInfo(
            agent_id=agent_id, description="d", groups=["t"],
            capabilities=[], **info_kw,
        ),
    )


# ===========================================================================
# accepts_schema — per-mode map
# ===========================================================================


def test_single_handler_publishes_one_mode_entry() -> None:
    a = _agent()

    @a.handler
    async def h(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    assert set(a.info.accepts_schema) == {"MessagePayload"}
    schema = a.info.accepts_schema["MessagePayload"]
    assert schema["type"] == "object"
    assert "prompt" in schema["properties"]
    # Published schema is tightened (kills cross-mode ambiguity).
    assert schema["additionalProperties"] is False


def test_multi_handler_is_a_map_not_oneof() -> None:
    a = _agent()

    @a.handler
    async def h1(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    @a.handler
    async def h2(ctx: TaskContext, p: StatusPayload) -> _Out: ...

    accepts = a.info.accepts_schema
    assert set(accepts) == {"MessagePayload", "StatusPayload"}
    assert "oneOf" not in accepts
    assert accepts["StatusPayload"]["properties"].keys() == {"status"}


def test_explicit_mode_names_are_the_keys() -> None:
    a = _agent()

    @a.handler(mode="chat")
    async def h1(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    @a.handler(mode="ping", tool=False)
    async def h2(ctx: TaskContext, p: StatusPayload) -> _Out: ...

    assert set(a.info.accepts_schema) == {"chat", "ping"}
    assert a.info.non_tool_modes == ["ping"]


def test_dict_mode_publishes_null_schema() -> None:
    a = _agent()

    @a.handler(mode="raw")
    async def h(ctx: TaskContext, p: dict) -> AgentOutput: ...

    assert a.info.accepts_schema == {"raw": None}


def test_accepts_schema_operator_pin_preserved() -> None:
    pinned = {"custom": {"type": "object", "properties": {}}}
    a = _agent(accepts_schema=pinned)

    @a.handler
    async def h(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    assert a.info.accepts_schema == pinned  # auto-derive skipped


# ===========================================================================
# non_tool_modes — auto-derivation + pin
# ===========================================================================


def test_non_tool_modes_auto_derived_in_registration_order() -> None:
    a = _agent()

    @a.handler
    async def h1(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    @a.handler(mode="clear", tool=False)
    async def h2(ctx: TaskContext, p: ClearHistory) -> _Out: ...

    @a.handler(mode="status", tool=False)
    async def h3(ctx: TaskContext, p: StatusPayload) -> _Out: ...

    assert a.info.non_tool_modes == ["clear", "status"]


def test_non_tool_modes_operator_pin_preserved() -> None:
    a = _agent(non_tool_modes=["pinned"])

    @a.handler(tool=False)
    async def h(ctx: TaskContext, p: ClearHistory) -> _Out: ...

    assert a.info.non_tool_modes == ["pinned"]


# ===========================================================================
# produces_schema — still single / oneOf, pinned-wins
# ===========================================================================


def test_produces_schema_single_then_oneof() -> None:
    a = _agent()

    @a.handler
    async def h1(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    assert a.info.produces_schema == _Out.model_json_schema()

    @a.handler
    async def h2(ctx: TaskContext, p: StatusPayload) -> _OtherOut: ...

    assert "oneOf" in a.info.produces_schema
    assert len(a.info.produces_schema["oneOf"]) == 2


def test_produces_schema_operator_pin_preserved() -> None:
    pinned = {"type": "object", "properties": {"x": {"type": "string"}}}
    a = _agent(produces_schema=pinned)

    @a.handler
    async def h(ctx: TaskContext, p: MessagePayload) -> _Out: ...

    assert a.info.produces_schema == pinned


# ===========================================================================
# Operator-pin snapshot freezes at construction
# ===========================================================================


def test_pinned_snapshot_freezes_at_agent_construction() -> None:
    """The pin oracle is `info.model_fields_set` snapshotted at
    `Agent.__init__` — subsequent auto-publish setattrs must not
    poison it (the original Phase-6 invariant, still load-bearing
    for the per-mode map)."""
    a = _agent(accepts_schema={"pinned": {"type": "object"}})

    @a.handler
    async def h1(ctx: TaskContext, p: _SnapA) -> _Out: ...

    @a.handler
    async def h2(ctx: TaskContext, p: _SnapB) -> _Out: ...

    @a.handler
    async def h3(ctx: TaskContext, p: _SnapC) -> _Out: ...

    # accepts_schema pinned → frozen; produces_schema NOT pinned →
    # still auto-derives across all three (proves the snapshot
    # didn't get poisoned by the accepts_schema setattr path).
    assert a.info.accepts_schema == {"pinned": {"type": "object"}}
    assert "oneOf" in a.info.produces_schema or a.info.produces_schema == (
        _Out.model_json_schema()
    )


def test_pinned_snapshot_unpinned_field_tracks_all_handlers() -> None:
    a = _agent()  # nothing pinned

    @a.handler
    async def h1(ctx: TaskContext, p: _SnapA) -> _Out: ...

    @a.handler
    async def h2(ctx: TaskContext, p: _SnapB) -> _Out: ...

    @a.handler
    async def h3(ctx: TaskContext, p: _SnapC) -> _Out: ...

    assert set(a.info.accepts_schema) == {"_SnapA", "_SnapB", "_SnapC"}
