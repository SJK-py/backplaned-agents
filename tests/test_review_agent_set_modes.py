"""Tests for `Agent.set_modes` — the runtime mode-replacement API
the MCP bridge calls on `tools/list_changed` (one Agent per server,
one mode per tool).

Five layers:

  * Validation: rejects bad mode names / non-async handlers / arity.
  * Atomic replace: handler dict, `accepts_schema`, `non_tool_modes`
    swapped wholesale; previously-registered modes that aren't in the
    new map are dropped.
  * Routing: `resolve_handler(mode=...)` reads the new dict on next
    dispatch; an evicted mode returns `None` (no_handler at the
    dispatcher).
  * Pre-connect: pure in-process mutation; no AgentInfoUpdate
    broadcast attempted.
  * Post-connect: AgentInfoUpdate emitted with the new accepts_schema
    + non_tool_modes via `update_info`.

The bridge's reconcile path is tested in `tests/test_phase10b_mcp_bridge.py`
and friends; this file pins the SDK contract those tests depend on.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_protocol.types import AgentInfo
from bp_sdk.agent import Agent
from bp_sdk.settings import AgentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(*, agent_id: str = "test_agent") -> Agent:
    info = AgentInfo(
        agent_id=agent_id,
        description="set_modes test agent",
        groups=["test"],
        capabilities=["test.set_modes"],
    )
    return Agent(
        info=info,
        config=AgentConfig(embedded=True, router_url="ws://test/v1/agent"),
    )


async def _noop_handler(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
    return {}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_set_modes_rejects_empty_mode_name() -> None:
    agent = _make_agent()
    with pytest.raises(TypeError, match="non-empty"):
        asyncio.run(agent.set_modes({"": (_noop_handler, None)}))


def test_set_modes_rejects_non_async_handler() -> None:
    agent = _make_agent()

    def sync_handler(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    with pytest.raises(TypeError, match="async"):
        asyncio.run(
            agent.set_modes({"foo": (sync_handler, None)})  # type: ignore[dict-item]
        )


def test_set_modes_rejects_unary_handler() -> None:
    agent = _make_agent()

    async def too_few(ctx) -> dict:  # type: ignore[no-untyped-def]
        return {}

    with pytest.raises(TypeError, match=r"\(ctx, payload\)"):
        asyncio.run(
            agent.set_modes({"foo": (too_few, None)})  # type: ignore[dict-item]
        )


def test_set_modes_validation_fails_atomically() -> None:
    """A bad handler in the middle of the map raises and leaves the
    agent's CURRENT mode set unchanged — partial replacement would
    leave the catalog inconsistent with the handler dict."""
    agent = _make_agent()

    @agent.handler(mode="initial")
    async def initial(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    before = dict(agent.registered_handlers)
    before_accepts = agent.info.accepts_schema

    async def good(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    def bad(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    with pytest.raises(TypeError):
        asyncio.run(agent.set_modes(
            {
                "good": (good, {"type": "object"}),
                "bad": (bad, {"type": "object"}),  # type: ignore[dict-item]
            }
        ))

    assert agent.registered_handlers == before
    assert agent.info.accepts_schema == before_accepts


# ---------------------------------------------------------------------------
# Atomic replace
# ---------------------------------------------------------------------------


def test_set_modes_replaces_handlers_wholesale() -> None:
    """Previously-registered modes that aren't in the new map are
    dropped. The bridge case: a tool that disappeared from
    `tools/list` must stop dispatching."""
    agent = _make_agent()

    @agent.handler(mode="old_tool")
    async def old(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    assert "old_tool" in agent.registered_handlers

    async def new_tool_fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes(
        {"new_tool": (new_tool_fn, {"type": "object"})}
    ))

    assert set(agent.registered_handlers.keys()) == {"new_tool"}


def test_set_modes_publishes_accepts_schema_map() -> None:
    agent = _make_agent()

    async def fn_a(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    async def fn_b(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    schema_a = {"type": "object", "properties": {"q": {"type": "string"}}}
    schema_b = {"type": "object", "additionalProperties": True}
    asyncio.run(agent.set_modes(
        {"search": (fn_a, schema_a), "fetch": (fn_b, schema_b)}
    ))

    assert agent.info.accepts_schema == {
        "search": schema_a,
        "fetch": schema_b,
    }


def test_set_modes_publishes_none_schema_for_unvalidated_mode() -> None:
    """A `None` schema means "router admits this mode without payload
    validation" — matches the dict-input semantics of
    `_republish_schemas`. The bridge uses this when an MCP tool
    advertises no input_schema."""
    agent = _make_agent()

    async def fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes({"no_schema_tool": (fn, None)}))
    assert agent.info.accepts_schema == {"no_schema_tool": None}


def test_set_modes_non_tool_modes_flag() -> None:
    """A mode listed in `non_tool_modes` is registered with
    `tool=False` so `build_tools` excludes it (control-plane / hidden-
    per-mode case)."""
    agent = _make_agent()

    async def regular(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    async def control(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes(
        {"regular": (regular, None), "ctrl": (control, None)},
        non_tool_modes=["ctrl"],
    ))

    assert agent.registered_handlers["regular"].tool is True
    assert agent.registered_handlers["ctrl"].tool is False
    assert agent.info.non_tool_modes == ["ctrl"]


def test_set_modes_empty_clears_everything() -> None:
    """A zero-tool MCP server reconciles to an empty mode set —
    valid catalog state (the agent itself stays connected, ready to
    accept future modes)."""
    agent = _make_agent()

    @agent.handler(mode="something")
    async def something(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes({}))
    assert agent.registered_handlers == {}
    assert agent.info.accepts_schema == {}
    assert agent.info.non_tool_modes == []


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_set_modes_evicted_mode_returns_no_handler() -> None:
    """`resolve_handler(mode=...)` returns `None` for a mode that
    USED TO BE registered but isn't anymore. The dispatcher surfaces
    this as `no_handler`."""
    agent = _make_agent()

    @agent.handler(mode="gone")
    async def gone(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    assert agent.resolve_handler(mode="gone") is not None
    asyncio.run(agent.set_modes({}))
    assert agent.resolve_handler(mode="gone") is None


def test_set_modes_new_mode_routes() -> None:
    agent = _make_agent()

    async def fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True}

    asyncio.run(agent.set_modes({"fresh": (fn, None)}))
    reg = agent.resolve_handler(mode="fresh")
    assert reg is not None
    assert reg.fn is fn
    assert reg.input_model is dict
    assert reg.tool is True


# ---------------------------------------------------------------------------
# Pre-connect: no broadcast
# ---------------------------------------------------------------------------


def test_set_modes_pre_connect_does_not_broadcast() -> None:
    """`_dispatcher is None` until run_async lands. set_modes pre-
    connect must mutate in-memory and return without trying to send
    an AgentInfoUpdate (which would raise the "agent not connected"
    error from `update_info`)."""
    agent = _make_agent()
    assert agent._dispatcher is None  # baseline

    async def fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes({"early": (fn, None)}))
    assert agent.info.accepts_schema == {"early": None}


# ---------------------------------------------------------------------------
# Post-connect: broadcasts via update_info
# ---------------------------------------------------------------------------


def test_set_modes_post_connect_broadcasts_via_update_info() -> None:
    """When the dispatcher is attached, `set_modes` calls
    `update_info(accepts_schema=..., non_tool_modes=...)` so the
    catalog learns about the new shape."""
    agent = _make_agent()
    # Pretend we're connected — `update_info`'s guard is just
    # `self._dispatcher is None`.
    agent._dispatcher = MagicMock()
    agent.update_info = AsyncMock()  # type: ignore[method-assign]

    async def fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes(
        {"a": (fn, {"type": "object"})},
        non_tool_modes=[],
    ))

    agent.update_info.assert_awaited_once_with(
        accepts_schema={"a": {"type": "object"}},
        non_tool_modes=[],
    )


def test_set_modes_post_connect_passes_empty_map() -> None:
    """An empty modes dict is a real patch ("clear my tools"), not
    an all-None update — `update_info` accepts it (the empty dict
    is not None)."""
    agent = _make_agent()
    agent._dispatcher = MagicMock()
    agent.update_info = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(agent.set_modes({}))
    agent.update_info.assert_awaited_once_with(
        accepts_schema={},
        non_tool_modes=[],
    )


def test_set_modes_state_settled_before_broadcast() -> None:
    """In-memory swap must happen BEFORE the wire ack. A frame for
    a new mode that arrives during the round-trip (the router
    already accepted accepts_schema={new_mode: ...} during the ack
    — it can re-dispatch a queued frame for that mode) routes
    consistently because resolve_handler reads the post-swap dict."""
    agent = _make_agent()
    agent._dispatcher = MagicMock()

    seen_handlers: dict[str, object] = {}

    async def fake_update_info(**kwargs: object) -> None:
        # During the broadcast, the handler dict is already the new one.
        seen_handlers.update(agent.registered_handlers)

    agent.update_info = fake_update_info  # type: ignore[assignment, method-assign]

    async def fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes({"new_during_ack": (fn, None)}))
    assert "new_during_ack" in seen_handlers


# ---------------------------------------------------------------------------
# Operator-pin discipline
# ---------------------------------------------------------------------------


def test_set_modes_pins_accepts_schema_against_decorator_republish() -> None:
    """After `set_modes`, a subsequent `@agent.handler` decoration
    MUST NOT clobber the schema map (the bridge's pin is exactly
    what existed pre-this-refactor — operator intent via the
    explicit set_modes call). The decorator's `_republish_schemas`
    skips the field because it's in `_operator_pinned_schema_fields`.

    This is the regression guard for: "bridge calls set_modes, then
    something inside the agent decorates a new handler, and the
    pinned MCP schemas get auto-derived away into `{mode: None}`."
    """
    agent = _make_agent()

    async def fn(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes(
        {"existing": (fn, {"type": "object", "title": "Pinned"})}
    ))
    assert agent.info.accepts_schema == {
        "existing": {"type": "object", "title": "Pinned"}
    }

    # A late decorator should add its mode without nuking the
    # pinned schema. (This is the same mechanism `_operator_pinned_schema_fields`
    # provides for AgentInfo-constructor pins.)
    @agent.handler(mode="late_added")
    async def late(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    # The pinned schema for "existing" must survive the decorator's
    # `_republish_schemas`. The newly-added mode isn't in the pinned
    # map yet — the decorator handles its own append in the future
    # is a follow-up; here we only pin against the auto-republish
    # WIPING the existing pinned entry.
    assert "existing" in (agent.info.accepts_schema or {})
    assert agent.info.accepts_schema["existing"] == {
        "type": "object", "title": "Pinned"
    }


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_set_modes_is_async() -> None:
    assert inspect.iscoroutinefunction(Agent.set_modes)


def test_set_modes_signature() -> None:
    """Source pin: signature stays exactly as documented in the
    design doc. The bridge's reconcile-flow code reads from this
    contract."""
    sig = inspect.signature(Agent.set_modes)
    params = list(sig.parameters.values())
    assert params[0].name == "self"
    assert params[1].name == "modes"
    # non_tool_modes is keyword-only after the bare `*`.
    nt = next(p for p in params if p.name == "non_tool_modes")
    assert nt.kind == inspect.Parameter.KEYWORD_ONLY
    assert nt.default is None
