"""Unified mode-dispatch model (replaces the old is_control /
@control_handler / @on_delegation split).

Routing is by an explicit mode name carried on
`NewTaskFrame.input_mode` and keyed in `AgentInfo.accepts_schema`
(`{mode: schema|null}`). Control-plane handlers are just modes
registered with `tool=False` (listed in `AgentInfo.non_tool_modes`,
hidden from `build_tools`). There is no separate control/delegation
registry and no `is_control` flag — these tests pin that contract.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from bp_protocol.frames import NewTaskFrame
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext


class UserMessage(BaseModel):
    prompt: str


class ClearHistory(BaseModel):
    keep_metadata: bool = True


class SetPersona(BaseModel):
    persona: str


def _agent(agent_id: str = "modeagent") -> Agent:
    return Agent(
        info=AgentInfo(
            agent_id=agent_id,
            description="mode dispatch test",
            groups=["t"],
            capabilities=[],
        ),
    )


# ---------------------------------------------------------------------------
# NewTaskFrame: input_mode replaces is_control
# ---------------------------------------------------------------------------


def test_new_task_frame_has_input_mode_default_none() -> None:
    f = NewTaskFrame(
        agent_id="a", trace_id="0" * 32, span_id="0" * 16,
        destination_agent_id="d", user_id="u", session_id="s",
    )
    assert f.input_mode is None


def test_new_task_frame_input_mode_round_trips() -> None:
    from bp_protocol.frames import parse_frame, serialize_frame

    f = NewTaskFrame(
        agent_id="a", trace_id="0" * 32, span_id="0" * 16,
        destination_agent_id="d", user_id="u", session_id="s",
        input_mode="ClearHistory",
    )
    again = parse_frame(serialize_frame(f))
    assert again.input_mode == "ClearHistory"


def test_new_task_frame_rejects_removed_is_control_field() -> None:
    """`is_control` is gone; `_FrameBase` is extra=forbid so a stray
    one is a hard frame_invalid (clean break, no silent ignore)."""
    with pytest.raises(Exception):
        NewTaskFrame(
            agent_id="a", trace_id="0" * 32, span_id="0" * 16,
            destination_agent_id="d", user_id="u", session_id="s",
            is_control=True,
        )


# ---------------------------------------------------------------------------
# AgentInfo: non_tool_modes replaces accepts_control_schema
# ---------------------------------------------------------------------------


def test_agent_info_has_non_tool_modes_no_control_schema() -> None:
    info = AgentInfo(agent_id="x", description="d")
    assert info.non_tool_modes == []
    assert not hasattr(info, "accepts_control_schema")


# ---------------------------------------------------------------------------
# Unified registry: tool=False => non_tool_modes, still dispatched
# ---------------------------------------------------------------------------


def test_no_control_or_delegation_decorators() -> None:
    a = _agent()
    assert not hasattr(a, "control_handler")
    assert not hasattr(a, "on_delegation")


def test_handlers_share_one_registry_keyed_by_mode() -> None:
    a = _agent()

    @a.handler
    async def on_msg(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...

    @a.handler(tool=False)
    async def on_clear(ctx: TaskContext, p: ClearHistory) -> AgentOutput: ...

    assert set(a._handlers_by_mode) == {"UserMessage", "ClearHistory"}
    assert a._handlers_by_mode["UserMessage"].tool is True
    assert a._handlers_by_mode["ClearHistory"].tool is False
    # Control-plane mode IS validated (schema present) but hidden.
    assert set(a.info.accepts_schema) == {"UserMessage", "ClearHistory"}
    assert a.info.non_tool_modes == ["ClearHistory"]


def test_tool_false_mode_excluded_from_build_tools() -> None:
    a = _agent("orch")

    @a.handler
    async def on_msg(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...

    @a.handler(tool=False)
    async def on_clear(ctx: TaskContext, p: ClearHistory) -> AgentOutput: ...

    from bp_sdk.tools import build_tools

    catalog = {
        "orch": {
            "description": "d",
            "accepts_schema": a.info.accepts_schema,
            "non_tool_modes": a.info.non_tool_modes,
        }
    }
    tools = build_tools(catalog, provider="anthropic")
    names = {t["name"] for t in tools}
    # Single tool-visible mode → back-compat bare name, no clear-history.
    assert names == {"call_orch"}


def test_duplicate_mode_raises_at_registration() -> None:
    a = _agent()

    @a.handler(mode="dup")
    async def h1(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...

    with pytest.raises(TypeError, match="duplicate handler mode"):
        @a.handler(mode="dup")
        async def h2(ctx: TaskContext, p: SetPersona) -> AgentOutput: ...


def test_explicit_mode_overrides_default_model_name() -> None:
    a = _agent()

    @a.handler(mode="chat")
    async def h(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...

    assert "chat" in a._handlers_by_mode
    assert "UserMessage" not in a._handlers_by_mode


def test_dict_handler_requires_explicit_mode() -> None:
    a = _agent()
    with pytest.raises(TypeError, match="explicit @handler\\(mode="):
        @a.handler
        async def h(ctx: TaskContext, p: dict) -> AgentOutput: ...

    @a.handler(mode="raw")
    async def ok(ctx: TaskContext, p: dict) -> AgentOutput: ...

    assert a._handlers_by_mode["raw"].input_model is dict
    # dict-mode publishes a null schema (explicit no-validation).
    assert a.info.accepts_schema == {"raw": None}


def test_handler_async_only() -> None:
    a = _agent()
    with pytest.raises(TypeError, match="must be async"):
        @a.handler(mode="x")
        def sync_h(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...  # type: ignore[misc]


def test_handler_rejects_non_pydantic_non_dict_payload() -> None:
    a = _agent()
    with pytest.raises(TypeError, match="must be `dict` or a Pydantic"):
        @a.handler(mode="x")
        async def h(ctx: TaskContext, p: list) -> AgentOutput: ...  # type: ignore[type-var]


# ---------------------------------------------------------------------------
# Resolution: O(1) by mode
# ---------------------------------------------------------------------------


def test_resolve_by_mode_exact() -> None:
    a = _agent()

    @a.handler
    async def m(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...

    @a.handler(tool=False)
    async def c(ctx: TaskContext, p: ClearHistory) -> AgentOutput: ...

    assert a.resolve_handler(mode="ClearHistory").fn is c
    assert a.resolve_handler(mode="UserMessage").fn is m
    assert a.resolve_handler(mode="nope") is None


def test_resolve_none_sole_handler_else_ambiguous() -> None:
    a = _agent()

    @a.handler
    async def only(ctx: TaskContext, p: UserMessage) -> AgentOutput: ...

    assert a.resolve_handler(mode=None).fn is only  # sole → resolves

    @a.handler
    async def second(ctx: TaskContext, p: SetPersona) -> AgentOutput: ...

    assert a.resolve_handler(mode=None) is None  # ambiguous → no_handler


def test_dispatch_resolves_via_input_mode() -> None:
    """`_resolve_handler_for` is a pure mode lookup off the frame."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    src = inspect.getsource(Dispatcher._resolve_handler_for)
    assert "resolve_handler(mode=frame.input_mode)" in src
    assert "is_control" not in src
    assert not hasattr(Dispatcher, "_pick_handler_by_payload")


# ---------------------------------------------------------------------------
# peers.spawn / delegate: mode kwarg, threaded as input_mode
# ---------------------------------------------------------------------------


def test_spawn_and_delegate_have_mode_not_is_control() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    for fn in (PeerClient.spawn, PeerClient.delegate):
        params = set(inspect.signature(fn).parameters)
        assert "mode" in params
        assert "is_control" not in params


def test_spawn_threads_mode_into_input_mode() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    for fn in (PeerClient.spawn, PeerClient.delegate):
        src = inspect.getsource(fn)
        assert "input_mode=mode" in src
        assert "is_control" not in src


# ---------------------------------------------------------------------------
# Router admit: per-mode schema selection
# ---------------------------------------------------------------------------


def test_router_admit_selects_schema_by_mode() -> None:
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks)
    assert 'get("accepts_schema")' in src
    assert "unknown input_mode" in src
    assert "input_mode is" in src  # required when multi-mode
    assert "accepts_control_schema" not in src


def test_router_forwards_input_mode_to_destination() -> None:
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks)
    assert "input_mode=frame.input_mode" in src
    assert "is_control=frame.is_control" not in src
