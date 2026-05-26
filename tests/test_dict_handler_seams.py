"""dict-input handler seams under the unified mode model.

MCP-bridge-style forwarders accept payloads as plain `dict` instead
of typed Pydantic models. Under unified mode dispatch:

  1. `Agent._make_registered` accepts `dict` — but a dict handler
     has no model name, so an explicit `@handler(mode=...)` is
     REQUIRED (no silent default).
  2. There is NO structural `_pick_handler_by_payload` fallback —
     a dict handler is just a normal mode resolved O(1) by key.
  3. `Dispatcher._handle_new_task` still skips Pydantic validation
     when the resolved handler's input model is `dict`.
  4. Schema auto-derive publishes `{mode: null}` for a dict mode
     (explicit "router admits without payload validation", not a
     silent gap); operator-pin still wins.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from bp_protocol.types import AgentInfo
from bp_sdk.agent import Agent


class _Typed(BaseModel):
    foo: str


# ===========================================================================
# Seam 1 — dict accepted, but needs an explicit mode
# ===========================================================================


def test_dict_handler_requires_explicit_mode() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_1", description="d"))

    with pytest.raises(TypeError, match="explicit @handler\\(mode="):
        @agent.handler
        async def bare(ctx, payload: dict): ...

    @agent.handler(mode="call")
    async def call(ctx, payload: dict):
        return None

    assert agent._handlers_by_mode["call"].input_model is dict


def test_handler_rejects_list_payload() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_2a", description="d"))
    with pytest.raises(TypeError, match="must be `dict` or a Pydantic BaseModel"):
        @agent.handler(mode="x")
        async def bad(ctx, payload: list): ...


def test_handler_rejects_int_payload() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_2b", description="d"))
    with pytest.raises(TypeError, match="must be `dict` or a Pydantic BaseModel"):
        @agent.handler(mode="x")
        async def bad(ctx, payload: int): ...


def test_handler_rejects_str_payload() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_2c", description="d"))
    with pytest.raises(TypeError, match="must be `dict` or a Pydantic BaseModel"):
        @agent.handler(mode="x")
        async def bad(ctx, payload: str): ...


# ===========================================================================
# Seam 2 — dict mode resolved by key, no structural fallback
# ===========================================================================


def test_pick_handler_by_payload_removed() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    assert not hasattr(Dispatcher, "_pick_handler_by_payload")


def test_dict_and_typed_modes_coexist_resolved_by_key() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_3", description="d"))

    @agent.handler
    async def typed_path(ctx, payload: _Typed): ...

    @agent.handler(mode="raw")
    async def dict_path(ctx, payload: dict): ...

    assert agent.resolve_handler(mode="_Typed").fn is typed_path
    assert agent.resolve_handler(mode="raw").fn is dict_path
    # No structural guessing: an unknown mode is just None.
    assert agent.resolve_handler(mode="other") is None


def test_sole_dict_handler_resolves_for_mode_none() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_4", description="d"))

    @agent.handler(mode="call")
    async def only(ctx, payload: dict): ...

    assert agent.resolve_handler(mode=None).fn is only
    assert agent.resolve_handler(mode="call").fn is only


# ===========================================================================
# Seam 3 — _handle_new_task passes dict payload through
# ===========================================================================


def test_handle_new_task_branches_on_dict_input_model() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    src = inspect.getsource(Dispatcher._handle_new_task)
    assert "if handler.input_model is dict:" in src
    assert "payload = frame.payload" in src


# ===========================================================================
# Seam 4 — accepts_schema map: dict mode → null
# ===========================================================================


def test_dict_mode_publishes_null_typed_mode_publishes_schema() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_5", description="d"))

    @agent.handler
    async def typed_path(ctx, payload: _Typed): ...

    @agent.handler(mode="raw")
    async def dict_path(ctx, payload: dict): ...

    schema = agent.info.accepts_schema
    assert set(schema) == {"_Typed", "raw"}
    assert schema["raw"] is None  # dict mode → explicit no-validation
    assert schema["_Typed"]["properties"].keys() == {"foo"}
    assert "oneOf" not in schema


def test_dict_only_agent_publishes_null_mode_entry() -> None:
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_6", description="d"))

    @agent.handler(mode="call")
    async def dict_only(ctx, payload: dict): ...

    # Explicit `{"call": None}` — visible "no payload validation",
    # not a silent absent schema.
    assert agent.info.accepts_schema == {"call": None}


def test_operator_pin_survives_dict_handler_registration() -> None:
    pytest.importorskip("fastapi")
    pinned = {"call": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }}
    agent = Agent(info=AgentInfo(
        agent_id="t_dict_7", description="d", accepts_schema=pinned,
    ))

    @agent.handler(mode="call")
    async def call(ctx, payload: dict): ...

    assert agent.info.accepts_schema == pinned


def test_dict_handler_can_be_tool_false() -> None:
    """Symmetry: a dict mode can be control-plane (`tool=False`) —
    listed in non_tool_modes, still routed/validated."""
    pytest.importorskip("fastapi")
    agent = Agent(info=AgentInfo(agent_id="t_dict_8", description="d"))

    @agent.handler(mode="cmd", tool=False)
    async def cmd(ctx, payload: dict): ...

    assert agent._handlers_by_mode["cmd"].input_model is dict
    assert agent._handlers_by_mode["cmd"].tool is False
    assert agent.info.non_tool_modes == ["cmd"]
    assert agent.info.accepts_schema == {"cmd": None}
