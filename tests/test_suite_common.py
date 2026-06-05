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
    make_send_file_tool,
    peer_tool_specs,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.common.loop import _FINAL_ANSWER_NUDGE
from bp_agents.common.progress import (
    LOOP_PROGRESS_KEY,
    PROGRESS_PRODUCER_KEY,
    LoopProgress,
)
from bp_agents.db.models import UserConfigRow
from bp_protocol.frames import ProgressFrame, ResultFrame
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


class _StubStream:
    """Minimal SpawnStream: async-iterates canned child frames, then exposes
    the terminal result; an async context manager like the real one."""

    def __init__(self, frames, result) -> None:
        self._frames = list(frames)
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def result(self):
        # async to match the real SpawnStream.result (bp_sdk/peers.py) — the
        # caller must `await` it. A sync stub here masked a missing await in
        # loop.py's streaming branch (coroutine passed to
        # tool_response_from_result → 'coroutine' object has no attribute
        # 'output').
        return self._result


class _StubPeers:
    def __init__(self, *, catalog=None, spawn_result=None, child_frames=None) -> None:
        self._catalog = catalog or {}
        self._spawn_result = spawn_result
        self._child_frames = child_frames or []
        self.spawned: list[ToolCall] = []

    def visible(self, *, for_user_level=None):
        return self._catalog

    async def spawn_from_tool_call(self, tc, *, stream=False, **kw):
        self.spawned.append(tc)
        if stream:
            return _StubStream(self._child_frames, self._spawn_result)
        return self._spawn_result


class _StubFiles:
    """Minimal FileStash stand-in for the file-tool dispatch path."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def llm_ref(self, name: str) -> dict:
        self.reads.append(name)
        return {"file_ref": {"name": name}}

    async def list(self, *, persistent=False, query=None):
        return []

    async def write(self, name, text, *, persistent=False):
        return name


class _StubCtx:
    def __init__(self, llm, peers, progress, files=None) -> None:
        self.llm = llm
        self.peers = peers
        self.progress = progress
        self.files = files


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


def _failed_result_frame(code: str, message: str = "") -> ResultFrame:
    # A FAILED child: output is None, the reason lives in `error`.
    return ResultFrame(
        agent_id="echo",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id="tsk_child",
        status=TaskStatus.FAILED,
        status_code=404,
        output=None,
        error={"code": code, "message": message},
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


def test_text_output_synthesizes_message_when_files_but_no_text() -> None:
    # Harness safeguard: a model that send_file's then stops with no text must
    # not deliver a bare/empty reply — supply a minimal accompanying line.
    out = text_output(None, files=["report.pdf"])
    assert "report.pdf" in out.content
    assert out.files == ["report.pdf"]
    # Empty/whitespace content is treated the same as None.
    assert text_output("   ", files=["a.txt", "b.csv"]).content
    assert "a.txt" in text_output("", files=["a.txt", "b.csv"]).content


def test_text_output_keeps_real_text_with_files() -> None:
    # When the model DID write a reply, it's preserved verbatim (no synthesis).
    out = text_output("Here's your export.", files=["x.csv"])
    assert out.content == "Here's your export."


def test_text_output_no_files_no_synthesis() -> None:
    # No files → empty content stays empty (a deliberate no-op turn is allowed).
    assert text_output(None).content is None
    assert text_output("").content == ""


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


def test_failed_peer_call_surfaces_error_to_model() -> None:
    """A FAILED subagent result (output=None, reason in `error`) must reach the
    model as a non-empty tool response naming the code/message — otherwise the
    model gets a blank result and can't recover or tell the user."""
    round1 = LlmResponse(
        text="", tool_calls=[ToolCall(id="c1", name="call_kb", args={})],
    )
    round2 = LlmResponse(text="sorry, that file wasn't found", tool_calls=[])
    llm = _StubLlm([round1, round2])
    peers = _StubPeers(
        spawn_result=_failed_result_frame("not_found", "file operation failed: not_found"),
    )
    ctx = _StubCtx(llm, peers, _StubProgress())

    messages: list[Message] = [Message(role="user", content="store notes.md in my KB")]
    asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages, preset="default", use_peer_tools=False,
        )
    )
    tool_msg = next(m for m in messages if m.role == "tool" and m.name == "call_kb")
    # The model sees the failure + the actionable code, not an empty string.
    assert tool_msg.content
    assert "not succeed" in tool_msg.content
    assert "not_found" in tool_msg.content


def test_run_llm_loop_forces_final_answer_at_round_limit() -> None:
    """If the model keeps calling tools until `max_rounds`, the loop must
    not return an empty tool-call turn. It makes one final tools-disabled
    generate (nudged) so the user gets a synthesized answer."""
    tool_round = LlmResponse(
        text="",
        tool_calls=[ToolCall(id="c1", name="current_time", args={})],
    )
    final = LlmResponse(text="Here is what I found.", tool_calls=[])
    # max_rounds=2 → two tool rounds, then the forced final generate (3rd).
    llm = _StubLlm([tool_round, tool_round, final])
    ctx = _StubCtx(llm, _StubPeers(), _StubProgress())
    messages: list[Message] = [Message(role="user", content="go")]

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages,
            preset="default",
            local_tools=LocalToolset([make_current_time_tool("UTC")]),
            use_peer_tools=False,
            max_rounds=2,
        )
    )

    # Non-empty synthesized answer, not the empty tool-call turn.
    assert resp.text == "Here is what I found."
    assert len(llm.calls) == 3  # 2 tool rounds + 1 forced final
    # The final generate disabled tools to force text.
    assert llm.calls[-1].get("tools") is None
    # The nudge was injected before the final generate.
    assert any(
        m.role == "user" and m.content == _FINAL_ANSWER_NUDGE for m in messages
    )


def _child_progress(kind: str, tool: str | None, *, producer: str = "research") -> ProgressFrame:
    lp = LoopProgress(kind=kind, tool=tool).model_dump()
    return ProgressFrame(
        agent_id=producer, trace_id="0" * 32, span_id="0" * 16,
        task_id="tsk_child", event=kind, content="",
        metadata={LOOP_PROGRESS_KEY: lp},
    )


def test_run_llm_loop_forwards_subagent_action_progress() -> None:
    """A subagent (peer) call is streamed; its tool_call/tool_result frames
    are re-emitted on the parent's progress tagged with the real producer,
    while its `thinking` heartbeats are dropped."""
    round1 = LlmResponse(
        text="",
        tool_calls=[ToolCall(id="c1", name="call_research", args={"prompt": "x"})],
    )
    round2 = LlmResponse(text="done", tool_calls=[])
    llm = _StubLlm([round1, round2])
    peers = _StubPeers(
        spawn_result=_result_frame("research result"),
        child_frames=[
            _child_progress("thinking", None),
            _child_progress("tool_call", "web_search"),
            _child_progress("tool_result", "web_search"),
        ],
    )
    progress = _StubProgress()
    ctx = _StubCtx(llm, peers, progress)

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=[Message(role="user", content="go")],
            preset="default", use_peer_tools=False,
        )
    )
    assert resp.text == "done"
    assert [tc.name for tc in peers.spawned] == ["call_research"]  # streamed
    # Forwarded frames carry the producer marker; the parent's own loop frames
    # don't. Only the subagent's actions are relayed — its thinking is dropped.
    forwarded = [(e, md) for (e, _c, md) in progress.events if PROGRESS_PRODUCER_KEY in md]
    assert [e for e, _ in forwarded] == ["tool_call", "tool_result"]
    assert all(md[PROGRESS_PRODUCER_KEY] == "research" for _e, md in forwarded)


def test_run_llm_loop_can_disable_subagent_progress() -> None:
    """`forward_subagent_progress=False` keeps the old wait-only spawn (no
    streaming, no relayed frames)."""
    round1 = LlmResponse(
        text="",
        tool_calls=[ToolCall(id="c1", name="call_research", args={})],
    )
    llm = _StubLlm([round1, LlmResponse(text="done", tool_calls=[])])
    peers = _StubPeers(
        spawn_result=_result_frame("r"),
        child_frames=[_child_progress("tool_call", "web_search")],
    )
    progress = _StubProgress()
    ctx = _StubCtx(llm, peers, progress)
    asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=[Message(role="user", content="go")],
            preset="default", use_peer_tools=False,
            forward_subagent_progress=False,
        )
    )
    assert not any(PROGRESS_PRODUCER_KEY in md for (_e, _c, md) in progress.events)


def test_run_llm_loop_emits_progress_for_terminal_tool() -> None:
    """A terminal tool (hand_off / end_delegation) ends the loop but must
    still surface a `tool_call` progress frame — otherwise the delegation
    transitions are invisible in verbose mode."""
    round1 = LlmResponse(
        text="I'll hand this to research.",
        tool_calls=[ToolCall(id="c1", name="hand_off", args={"agent_id": "research"})],
    )
    llm = _StubLlm([round1])
    progress = _StubProgress()
    ctx = _StubCtx(llm, _StubPeers(), progress)
    messages: list[Message] = [Message(role="user", content="research X")]

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages, local_tools=None, use_peer_tools=False,
            extra_tools=[ToolSpec(name="hand_off", description="", parameters={})],
            terminal_tools={"hand_off"},
        )
    )
    # The loop returned on the terminal tool (one round, no dispatch).
    assert any(tc.name == "hand_off" for tc in resp.tool_calls)
    assert len(llm.calls) == 1
    tool_calls = [
        md[LOOP_PROGRESS_KEY]
        for _e, _c, md in progress.events
        if LOOP_PROGRESS_KEY in md and md[LOOP_PROGRESS_KEY]["kind"] == "tool_call"
    ]
    assert any(lp["tool"] == "hand_off" for lp in tool_calls)
    # No tool_result frame — a terminal tool is never dispatched.
    assert not any(
        md[LOOP_PROGRESS_KEY]["kind"] == "tool_result"
        for _e, _c, md in progress.events
        if LOOP_PROGRESS_KEY in md
    )


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


def test_run_llm_loop_file_tools_offered_and_read_feeds_multimodal() -> None:
    # Round 1: the model calls read_file. Round 2: it answers.
    round1 = LlmResponse(
        text="", tool_calls=[ToolCall(id="c1", name="read_file", args={"name": "chart.png"})]
    )
    round2 = LlmResponse(text="I can see the chart.", tool_calls=[])
    llm = _StubLlm([round1, round2])
    files = _StubFiles()
    ctx = _StubCtx(llm, _StubPeers(), _StubProgress(), files=files)
    messages: list[Message] = [Message(role="user", content="describe chart.png")]

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages, local_tools=None, use_peer_tools=False,
            file_tools="full",
        )
    )

    assert resp.text == "I can see the chart."
    # The full bundle was advertised to the model.
    offered = {t.name for t in llm.calls[0]["tools"]}
    assert {"list_session_file", "read_file", "write_file", "delete_file"} <= offered
    # read_file produced a multimodal file_ref tool result (router resolves
    # the bytes on the next turn) — not a text echo.
    tool_msg = next(m for m in messages if m.role == "tool")
    assert tool_msg.content == [{"file_ref": {"name": "chart.png"}}]
    assert files.reads == ["chart.png"]


def test_run_llm_loop_file_tools_absent_when_disabled() -> None:
    # Without file_tools, a file-tool name is unknown (no dispatch, not
    # advertised) — same as any other unknown tool.
    round1 = LlmResponse(
        text="", tool_calls=[ToolCall(id="c1", name="read_file", args={"name": "x"})]
    )
    round2 = LlmResponse(text="recovered", tool_calls=[])
    llm = _StubLlm([round1, round2])
    files = _StubFiles()
    ctx = _StubCtx(llm, _StubPeers(), _StubProgress(), files=files)
    messages: list[Message] = [Message(role="user", content="go")]

    resp = asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages, local_tools=None, use_peer_tools=False,
        )
    )
    assert resp.text == "recovered"
    assert llm.calls[0].get("tools") is None
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "unknown tool" in tool_msg.content
    assert files.reads == []


def test_run_llm_loop_populates_progress_detail() -> None:
    long_reason = "z" * 250  # last paragraph, exceeds the 100-char cap
    round1 = LlmResponse(
        text="Looking up the budget now.",
        tool_calls=[ToolCall(id="c1", name="current_time", args={})],
        thought_summary=f"first paragraph\n\n{long_reason}",
    )
    round2 = LlmResponse(text="all done", tool_calls=[])
    llm = _StubLlm([round1, round2])
    progress = _StubProgress()
    ctx = _StubCtx(llm, _StubPeers(), progress)
    messages: list[Message] = [Message(role="user", content="go")]
    local = LocalToolset([make_current_time_tool("UTC")])

    asyncio.run(
        run_llm_loop(
            ctx,  # type: ignore[arg-type]
            messages=messages, preset="default", local_tools=local,
            use_peer_tools=False, detail_chars=100,
        )
    )

    frames = [md[LOOP_PROGRESS_KEY] for _e, _c, md in progress.events if LOOP_PROGRESS_KEY in md]
    by_kind: dict[str, list[dict]] = {}
    for f in frames:
        by_kind.setdefault(f["kind"], []).append(f)

    # thinking detail = last paragraph of thought_summary, trimmed to the
    # last 100 chars (ellipsis-prefixed because it overflowed).
    think_details = [f["detail"] for f in by_kind["thinking"] if f.get("detail")]
    assert think_details == ["…" + long_reason[-100:]]
    # tool_call detail = the accompanying assistant message (short → verbatim).
    assert by_kind["tool_call"][0]["detail"] == "Looking up the budget now."
    # tool_result detail = the tool's output (current_time → carries the tz).
    assert "UTC" in by_kind["tool_result"][0]["detail"]


def test_loop_progress_model() -> None:
    lp = LoopProgress(kind="tool_call", round=2, tool="call_echo", detail="x")
    dumped = lp.model_dump()
    assert dumped["kind"] == "tool_call"
    assert dumped["tool"] == "call_echo"


# ---------------------------------------------------------------------------
# send_file tool — records names for delivery to the user
# ---------------------------------------------------------------------------


class _FilesWith:
    """ctx.files stand-in with a fixed set of session/persistent names."""

    def __init__(self, session=(), persistent=()) -> None:
        self._session = list(session)
        self._persistent = list(persistent)

    async def list(self, *, persistent=False, query=None):
        return list(self._persistent if persistent else self._session)


def _ctx_with_files(files):
    return _StubCtx(_StubLlm([]), _StubPeers(), _StubProgress(), files=files)


def test_send_file_records_existing_name() -> None:
    outbound: list[str] = []
    tool = make_send_file_tool(outbound)
    ctx = _ctx_with_files(_FilesWith(session=["report.pdf"]))
    msg = asyncio.run(
        LocalToolset([tool]).dispatch(ctx, ToolCall(id="c", name="send_file",
                                                    args={"name": "report.pdf"}))
    )
    assert outbound == ["report.pdf"]
    # The tool result reinforces: queued + delivered alongside the reply, so the
    # model still writes a final message. (It no longer forbids end_delegation —
    # a queued file now rides through the hand-back.)
    assert "queued" in msg.content
    assert "alongside your reply" in msg.content


def test_send_file_dedups_and_validates_persist_prefix() -> None:
    outbound: list[str] = []
    ts = LocalToolset([make_send_file_tool(outbound)])
    ctx = _ctx_with_files(_FilesWith(persistent=["notes.md"]))
    # persist/ name is validated against the persistent stash (bare match ok)
    asyncio.run(ts.dispatch(ctx, ToolCall(id="c1", name="send_file",
                                          args={"name": "persist/notes.md"})))
    # a second call with the same name doesn't double-add
    asyncio.run(ts.dispatch(ctx, ToolCall(id="c2", name="send_file",
                                          args={"name": "persist/notes.md"})))
    assert outbound == ["persist/notes.md"]


def test_send_file_rejects_unknown_name() -> None:
    outbound: list[str] = []
    tool = make_send_file_tool(outbound)
    ctx = _ctx_with_files(_FilesWith(session=["other.txt"]))
    msg = asyncio.run(
        LocalToolset([tool]).dispatch(ctx, ToolCall(id="c", name="send_file",
                                                    args={"name": "missing.pdf"}))
    )
    assert outbound == []
    assert "No stash file" in msg.content
