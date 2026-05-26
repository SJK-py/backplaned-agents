"""bp_agents.common — unit tests (no router / DB; stubbed context).

Covers the loop's tool dispatch (local + peer), the current_time tool,
peer-catalog projection, prompt composition, output/token helpers, and
the LoopProgress payload.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bp_agents.common import (
    LocalTool,
    LocalToolset,
    compose_system_prompt,
    estimate_context_tokens,
    make_current_time_tool,
    peer_tool_specs,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.common.progress import LOOP_PROGRESS_KEY, LoopProgress
from bp_agents.db.models import UserConfigRow
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus
from bp_sdk import LlmResponse, Message, ToolCall, ToolSpec

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubProgress:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event: str, content: str = "", **md) -> None:
        self.events.append((event, content, md))


class _StubLlm:
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def generate(self, messages, **kw) -> LlmResponse:
        self.calls.append(kw)
        return self._responses.pop(0)


class _StubPeers:
    def __init__(self, *, catalog=None, spawn_result=None) -> None:
        self._catalog = catalog or {}
        self._spawn_result = spawn_result
        self.spawned: list[ToolCall] = []

    def visible(self, *, for_user_level=None):
        return self._catalog

    async def spawn_from_tool_call(self, tc):
        self.spawned.append(tc)
        return self._spawn_result


class _StubCtx:
    def __init__(self, llm, peers, progress) -> None:
        self.llm = llm
        self.peers = peers
        self.progress = progress


def _result_frame(content: str, files=None) -> ResultFrame:
    return ResultFrame(
        agent_id="echo",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id="tsk_child",
        status=TaskStatus.SUCCEEDED,
        status_code=200,
        output=AgentOutput(content=content, files=files or []),
    )


def _user_config(**overrides) -> UserConfigRow:
    base = dict(
        user_id="usr_a", full_name="Ada", timezone="Europe/London",
        preset_pro="default", preset_balanced="default", preset_lite="default",
        preset_embedding="default", max_context_token_limit=120_000,
        verbose_default=False, language="en", custom_note="be terse",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return UserConfigRow(**base)


# ---------------------------------------------------------------------------
# output / prompts
# ---------------------------------------------------------------------------


def test_text_output_stamps_context_tokens() -> None:
    out = text_output("hi", context_tokens=42, foo="bar")
    assert out.content == "hi"
    assert out.metadata["context_tokens"] == 42
    assert out.metadata["foo"] == "bar"
    assert text_output("x").metadata == {}


def test_estimate_context_tokens_counts_text_only() -> None:
    msgs = [
        Message(role="system", content="a" * 40),
        Message(role="user", content=[{"text": "b" * 40}, {"file_ref": {"name": "x"}}]),
    ]
    # 40 + 40 chars at ~4 chars/token = 20; file_ref ignored.
    assert estimate_context_tokens(msgs) == 20


def test_compose_system_prompt_and_config_note() -> None:
    note = user_config_note(_user_config())
    assert "Ada" in note and "Europe/London" in note and "be terse" in note
    prompt = compose_system_prompt(
        "You are helpful.", config_note=note, summary="prior chat",
    )
    assert prompt.startswith("You are helpful.")
    assert "About the user" in prompt
    assert "Conversation so far" in prompt
    # Empty config → note is "".
    assert user_config_note(
        _user_config(full_name="", timezone="", language="", custom_note="")
    ) == ""


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def test_current_time_tool() -> None:
    tool = make_current_time_tool("UTC")
    assert tool.spec.name == "current_time"

    async def _run() -> str:
        return await tool.handler(None, {})  # ctx unused by the handler

    out = asyncio.run(_run())
    assert "UTC" in out and ":" in out


def test_local_tool_guards() -> None:
    raised = False
    try:
        LocalTool(spec=ToolSpec(name="call_x", description="", parameters={}),
                  handler=None)  # type: ignore[arg-type]
    except ValueError:
        raised = True
    assert raised

    ts = LocalToolset([make_current_time_tool()])
    dup = False
    try:
        ts.add(make_current_time_tool())
    except ValueError:
        dup = True
    assert dup
    assert ts.has("current_time")
    assert [s.name for s in ts.specs()] == ["current_time"]


def test_peer_tool_specs_projection() -> None:
    catalog = {
        "echo": {
            "description": "Echo agent",
            "capabilities": ["text.echo"],
            "accepts_schema": {
                "LLMData": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                }
            },
            "non_tool_modes": [],
            "callable_user_levels": ["tier0"],
        }
    }
    ctx = _StubCtx(None, _StubPeers(catalog=catalog), _StubProgress())
    specs = peer_tool_specs(ctx)  # type: ignore[arg-type]
    assert [s.name for s in specs] == ["call_echo"]
    assert specs[0].parameters["properties"] == {"prompt": {"type": "string"}}


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------


def test_run_llm_loop_dispatches_local_and_peer_tools() -> None:
    # Round 1: model calls a local tool + a peer tool. Round 2: no tools.
    round1 = LlmResponse(
        text="",
        tool_calls=[
            ToolCall(id="c1", name="current_time", args={}),
            ToolCall(id="c2", name="call_echo", args={"prompt": "hi"}),
        ],
    )
    round2 = LlmResponse(text="all done", tool_calls=[])

    llm = _StubLlm([round1, round2])
    peers = _StubPeers(spawn_result=_result_frame("peer says hi"))
    progress = _StubProgress()
    ctx = _StubCtx(llm, peers, progress)

    messages: list[Message] = [Message(role="user", content="go")]
    local = LocalToolset([make_current_time_tool("UTC")])

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages,
            preset="default",
            local_tools=local,
            use_peer_tools=False,  # stub peers has no catalog
        )
    )

    assert resp.text == "all done"
    # Peer tool dispatched via spawn_from_tool_call.
    assert [tc.name for tc in peers.spawned] == ["call_echo"]
    # Two LLM rounds.
    assert len(llm.calls) == 2
    # Messages now hold: user, assistant(r1), tool(current_time),
    # tool(call_echo), assistant(r2).
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert {m.name for m in tool_msgs} == {"current_time", "call_echo"}
    # Progress emitted structured LoopProgress payloads.
    kinds = [
        md[LOOP_PROGRESS_KEY]["kind"]
        for _e, _c, md in progress.events
        if LOOP_PROGRESS_KEY in md
    ]
    assert "thinking" in kinds and "tool_call" in kinds and "tool_result" in kinds


def test_run_llm_loop_unknown_tool_feeds_error_back() -> None:
    round1 = LlmResponse(
        text="", tool_calls=[ToolCall(id="c1", name="mystery", args={})]
    )
    round2 = LlmResponse(text="recovered", tool_calls=[])
    llm = _StubLlm([round1, round2])
    ctx = _StubCtx(llm, _StubPeers(), _StubProgress())
    messages: list[Message] = [Message(role="user", content="go")]

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages, local_tools=None, use_peer_tools=False,
        )
    )
    assert resp.text == "recovered"
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "unknown tool" in tool_msg.content


def test_loop_progress_model() -> None:
    lp = LoopProgress(kind="tool_call", round=2, tool="call_echo", detail="x")
    dumped = lp.model_dump()
    assert dumped["kind"] == "tool_call"
    assert dumped["tool"] == "call_echo"
