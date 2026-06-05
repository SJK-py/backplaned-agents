"""Delegation lifecycle — orchestrator hand-off / hand-back, l1 delegated
turn, and the channel's delegated_to maintenance.

Agent-side pieces use a scripted stub LLM + stub peers (records
delegate/visible) + a live suite DB. The gateway delegated_to logic is
driven directly with synthetic ResultFrames.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.agents.deep_reasoning.agent import _CONFIG as _DR_CONFIG
from bp_agents.agents.l1_common import L1Config, run_delegated_turn, run_subagent
from bp_agents.agents.orchestrator.agent import (
    run_orchestrator_end_delegation,
    run_orchestrator_message,
)
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, LLMData, TaskStatus
from bp_sdk import LlmResponse, ToolCall  # noqa: E402
from bp_sdk.peers import SpawnRejected

_L1 = "deep_reasoning"
_CATALOG = {
    _L1: {
        "description": "reasoning",
        "groups": ["l1"],
        "capabilities": ["assistant.reasoning"],
        "accepts_schema": {"LLMData": {"type": "object", "properties": {}}},
        "non_tool_modes": ["on_delegation", "delegated_message"],
        "callable_user_levels": ["tier0"],
    }
}


class _StubLlm:
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)

    async def generate(self, messages, **kw) -> LlmResponse:
        return self._responses.pop(0) if self._responses else LlmResponse(text="done")


class _StubProgress:
    async def emit(self, *a, **k) -> None:
        return None


class _StubPeers:
    def __init__(self) -> None:
        self.delegations: list[tuple] = []

    def visible(self, *, for_user_level=None):
        return _CATALOG

    async def delegate(self, dest, payload, *, mode=None, **kw) -> None:
        self.delegations.append((dest, payload, mode))


class _StubFiles:
    def __init__(self, session=()) -> None:
        self._session = list(session)

    async def list(self, *, persistent=False, query=None):
        return [] if persistent else list(self._session)


class _Ctx:
    def __init__(self, llm, peers, *, user_id="usr_a", session_id="ses_1",
                 delegating=None, files=None):
        self.llm = llm
        self.peers = peers
        self.progress = _StubProgress()
        self.user_id = user_id
        self.session_id = session_id
        self.user_level = "tier0"
        self.delegating_agent_id = delegating
        self.files = files


def _settings(url: str) -> SuiteSettings:
    return SuiteSettings(database_url=url)


async def _reset(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="chatbot_telegram"
        )


def test_orchestrator_hand_off(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            peers = _StubPeers()
            # The model elects to hand off.
            llm = _StubLlm([
                LlmResponse(text="", tool_calls=[ToolCall(
                    id="c1", name="hand_off",
                    args={"agent_id": _L1, "instruction": "reason about X"},
                )]),
            ])
            ctx = _Ctx(llm, peers)
            out = await run_orchestrator_message(
                ctx, MessagePayload(prompt="help me think"),
                pool=pool, settings=_settings(suite_db_url),
            )
            # Result-less (router drops it; the delegate terminates the task).
            assert out.content is None or out.content == ""
            # Delegated to the l1 with on_delegation.
            assert len(peers.delegations) == 1
            dest, payload, mode = peers.delegations[0]
            assert dest == _L1 and mode == "on_delegation"
            assert isinstance(payload, LLMData)
            # The delegate_prompt seed row landed in the l1's thread.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
                orch = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
            assert rows and rows[0].role == "user"
            assert "reason about X" in rows[0].message
            # Hand-off closed the orchestrator's open turn with a hidden
            # `assistant` marker attributing the work to the delegate.
            assert orch and orch[-1].role == "assistant" and orch[-1].hidden
            assert f"Delegated to {_L1}" in orch[-1].message
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_message_surfaces_send_file(suite_db_url: str) -> None:
    """When the model calls `send_file`, the name rides out on
    `AgentOutput.files` so the channel delivers it to the user."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            # Round 1: model attaches a file; round 2: final text.
            llm = _StubLlm([
                LlmResponse(text="", tool_calls=[ToolCall(
                    id="c1", name="send_file", args={"name": "report.pdf"},
                )]),
                LlmResponse(text="Here's your report."),
            ])
            ctx = _Ctx(llm, _StubPeers(), files=_StubFiles(session=["report.pdf"]))
            out = await run_orchestrator_message(
                ctx, MessagePayload(prompt="make me a report"),
                pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "Here's your report."
            assert out.files == ["report.pdf"]
        finally:
            await pool.close()

    asyncio.run(_drive())


class _RejectingPeers(_StubPeers):
    """delegate() admit fails — exercises the F1 hand-off fallback."""

    async def delegate(self, dest, payload, *, mode=None, **kw) -> None:
        self.delegations.append((dest, payload, mode))
        raise SpawnRejected("router rejected delegate: unavailable", reason="unavailable")


def test_orchestrator_hand_off_fallback_on_admit_failure(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            peers = _RejectingPeers()
            # First call elects hand-off; the re-run (after the failed admit)
            # answers the user directly.
            llm = _StubLlm([
                LlmResponse(text="", tool_calls=[ToolCall(
                    id="c1", name="hand_off",
                    args={"agent_id": _L1, "instruction": "reason about X"},
                )]),
                LlmResponse(text="Here's my direct answer."),
            ])
            ctx = _Ctx(llm, peers)
            out = await run_orchestrator_message(
                ctx, MessagePayload(prompt="help me think"),
                pool=pool, settings=_settings(suite_db_url),
            )
            # F1: the orchestrator produced a real (non-empty) Result itself.
            assert out.content == "Here's my direct answer."
            assert len(peers.delegations) == 1  # one (failed) attempt

            async with pool.acquire() as conn:
                # The orchestrator persisted its own assistant turn.
                orch_rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                # The orphan seed row in the l1 thread was retired.
                l1_rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
            assert [r.role for r in orch_rows][-1] == "assistant"
            assert orch_rows[-1].message == "Here's my direct answer."
            assert l1_rows == []
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_l1_subagent_current_time_uses_user_timezone(suite_db_url: str) -> None:
    """The l1 `current_time` tool reads the user's tz (threaded through the
    local-tools factory), not the default."""

    class _TzLlm:
        def __init__(self) -> None:
            self.calls = 0
            self.tool_results: list = []

        async def generate(self, messages, **kw) -> LlmResponse:
            self.calls += 1
            for m in messages:
                if getattr(m, "role", None) == "tool":
                    self.tool_results.append(m.content)
            if self.calls == 1:
                return LlmResponse(text="", tool_calls=[
                    ToolCall(id="t", name="current_time", args={})
                ])
            return LlmResponse(text="done")

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_user_config(
                    conn, user_id="usr_a", default_session_id="ses_1",
                    timezone="Asia/Seoul",
                )
            llm = _TzLlm()
            out = await run_subagent(
                _Ctx(llm, _StubPeers()), LLMData(prompt="what time is it?"),
                config=_DR_CONFIG, pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "done"
            assert any("Asia/Seoul" in tr for tr in llm.tool_results)
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_l1_delegated_turn_normal_and_end(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            # Seed the delegate thread (the orchestrator's seed row).
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="## Delegated task\nthink",
                )
            cfg = L1Config(agent_id=_L1, subagent_system="s", delegation_system="d")

            # Normal subsequent turn → appends assistant row, no hand-back.
            peers = _StubPeers()
            ctx = _Ctx(_StubLlm([LlmResponse(text="here is my reasoning")]), peers)
            out = await run_delegated_turn(
                ctx, config=cfg, pool=pool, settings=_settings(suite_db_url),
                first_turn=False,
            )
            assert out.content == "here is my reasoning"
            assert peers.delegations == []
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
            assert [r.role for r in rows][-1] == "assistant"

            # end_delegation on a subsequent turn → hands back, result-less.
            peers2 = _StubPeers()
            ctx2 = _Ctx(_StubLlm([LlmResponse(text="", tool_calls=[ToolCall(
                id="c2", name="end_delegation",
                args={"delegation_summary": "did it", "exit_reason": "done"},
            )])]), peers2)
            out2 = await run_delegated_turn(
                ctx2, config=cfg, pool=pool, settings=_settings(suite_db_url),
                first_turn=False,
            )
            assert out2.content is None or out2.content == ""
            assert len(peers2.delegations) == 1
            dest, _payload, mode = peers2.delegations[0]
            assert dest == "orchestrator" and mode == "end_delegation"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_l1_first_turn_never_hands_back(suite_db_url: str) -> None:
    """The first (on_delegation) turn is not offered `end_delegation`, so it
    cannot hand back — handing back `T` to the orchestrator (its originator)
    would be a router-rejected cycle. Even a model that hallucinates the tool
    gets an "unknown tool" response and the turn completes normally."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="## Delegated task\nthink",
                )
            cfg = L1Config(agent_id=_L1, subagent_system="s", delegation_system="d")
            peers = _StubPeers()
            # The model "tries" to end on its first turn; the tool isn't
            # advertised, so it isn't terminal and isn't dispatched as a
            # hand-back. The stub then falls through to a plain "done".
            ctx = _Ctx(_StubLlm([LlmResponse(text="", tool_calls=[ToolCall(
                id="c1", name="end_delegation",
                args={"delegation_summary": "x", "exit_reason": "y"},
            )])]), peers)
            out = await run_delegated_turn(
                ctx, config=cfg, pool=pool, settings=_settings(suite_db_url),
                first_turn=True,
            )
            assert peers.delegations == []           # no hand-back on T
            assert out.content == "done"             # turn terminates T itself
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
            assert [r.role for r in rows][-1] == "assistant"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_end_delegation_recap(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                # Post-hand-off orchestrator thread: the open user prompt was
                # already closed by the hidden "Delegated to …" marker.
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="user", message="do the thing",
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="assistant", message=f"Delegated to {_L1}.", hidden=True,
                )
                # An l1 episode to retire.
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="seed",
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="assistant", message="work",
                )
            ctx = _Ctx(_StubLlm([]), _StubPeers(), delegating=_L1)
            out = await run_orchestrator_end_delegation(
                ctx,
                {"delegation_summary": "solved it", "exit_reason": "done"},
                pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == ""
            async with pool.acquire() as conn:
                main = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                # The delegate episode was retired (no incumbent rows).
                l1_rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
            assert l1_rows == []
            # Recap is a hidden `user` row (results as external input); a hidden
            # `assistant` "Acknowledged." closes it — the thread alternates.
            assert [r.role for r in main] == ["user", "assistant", "user", "assistant"]
            recap, ack = main[-2], main[-1]
            assert recap.role == "user" and recap.hidden and "solved it" in recap.message
            assert ack.role == "assistant" and ack.hidden and ack.message == "Acknowledged."
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_l1_end_delegation_auto_forwards_user_message(suite_db_url: str) -> None:
    """end_delegation fires for out-of-remit messages, so the user's current
    message is unanswered. The l1 forwards it as `user_prompt` automatically so
    the orchestrator answers it instead of returning "(no response)"."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="## Delegated task\nthink",
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="assistant", message="reasoned",
                )
                # The current (out-of-remit) user message that triggered the turn.
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="what's the weather in Paris?",
                )
            cfg = L1Config(agent_id=_L1, subagent_system="s", delegation_system="d")
            peers = _StubPeers()
            ctx = _Ctx(_StubLlm([LlmResponse(text="", tool_calls=[ToolCall(
                id="c1", name="end_delegation",
                args={"delegation_summary": "done reasoning", "exit_reason": "off-topic"},
            )])]), peers)
            await run_delegated_turn(
                ctx, config=cfg, pool=pool, settings=_settings(suite_db_url),
                first_turn=False,
            )
            assert len(peers.delegations) == 1
            _dest, payload, _mode = peers.delegations[0]
            assert payload["user_prompt"] == "what's the weather in Paris?"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_l1_end_delegation_explicit_user_prompt_wins(suite_db_url: str) -> None:
    """A `user_prompt` the model deliberately set is NOT overwritten by the
    auto-forward fallback."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="actual user message",
                )
            cfg = L1Config(agent_id=_L1, subagent_system="s", delegation_system="d")
            peers = _StubPeers()
            ctx = _Ctx(_StubLlm([LlmResponse(text="", tool_calls=[ToolCall(
                id="c1", name="end_delegation",
                args={"delegation_summary": "s", "exit_reason": "r",
                      "user_prompt": "please book a flight"},
            )])]), peers)
            await run_delegated_turn(
                ctx, config=cfg, pool=pool, settings=_settings(suite_db_url),
                first_turn=False,
            )
            _dest, payload, _mode = peers.delegations[0]
            assert payload["user_prompt"] == "please book a flight"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_l1_end_delegation_carries_queued_file(suite_db_url: str) -> None:
    """A file queued via send_file in the same turn as end_delegation rides
    through to the orchestrator on the payload — no longer dropped."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=_L1,
                    role="user", message="make me a chart",
                )
            cfg = L1Config(agent_id=_L1, subagent_system="s", delegation_system="d")
            peers = _StubPeers()
            # Round 1: queue the file; round 2: hand back.
            ctx = _Ctx(
                _StubLlm([
                    LlmResponse(text="", tool_calls=[ToolCall(
                        id="c1", name="send_file", args={"name": "chart.png"},
                    )]),
                    LlmResponse(text="", tool_calls=[ToolCall(
                        id="c2", name="end_delegation",
                        args={"delegation_summary": "made it", "exit_reason": "handing back"},
                    )]),
                ]),
                peers,
                files=_StubFiles(session=["chart.png"]),
            )
            await run_delegated_turn(
                ctx, config=cfg, pool=pool, settings=_settings(suite_db_url),
                first_turn=False,
            )
            _dest, payload, _mode = peers.delegations[0]
            assert payload["files"] == ["chart.png"]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_end_delegation_delivers_handback_files(suite_db_url: str) -> None:
    """With no follow-up prompt, the orchestrator still delivers files the
    specialist queued before handing back (as if it called send_file)."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="user", message="do the thing",
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="assistant", message=f"Delegated to {_L1}.", hidden=True,
                )
            ctx = _Ctx(_StubLlm([]), _StubPeers(), delegating=_L1)
            out = await run_orchestrator_end_delegation(
                ctx,
                {"delegation_summary": "made it", "exit_reason": "done",
                 "files": ["chart.png"]},
                pool=pool, settings=_settings(suite_db_url),
            )
            assert out.files == ["chart.png"]
            assert out.content  # safeguard supplies an accompanying line
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_end_delegation_merges_files_with_followup(suite_db_url: str) -> None:
    """When the hand-back carries BOTH a follow-up prompt and files, the
    orchestrator answers the prompt AND delivers the files."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="user", message="earlier", )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="assistant", message=f"Delegated to {_L1}.", hidden=True,
                )
            # The orchestrator's loop answers inline (no hand_off).
            ctx = _Ctx(_StubLlm([LlmResponse(text="Here you go.")]),
                       _StubPeers(), delegating=_L1)
            out = await run_orchestrator_end_delegation(
                ctx,
                {"delegation_summary": "s", "exit_reason": "r",
                 "user_prompt": "and send me the chart", "files": ["chart.png"]},
                pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "Here you go."
            assert "chart.png" in out.files
        finally:
            await pool.close()

    asyncio.run(_drive())


def _result(agent_id: str, status: TaskStatus = TaskStatus.SUCCEEDED) -> ResultFrame:
    return ResultFrame(
        agent_id=agent_id, trace_id="0" * 32, span_id="0" * 16,
        task_id="t", status=status, status_code=200,
        output=AgentOutput(content="x"),
    )


def test_gateway_delegated_to_maintenance(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            gw = ChatbotGateway(dispatcher=None, pool=pool, telegram=None)

            # Hand-off: dispatched orchestrator, delegate produced result.
            await gw._core._update_delegation("ses_1", "orchestrator", _result(_L1))
            async with pool.acquire() as conn:
                assert (await queries.get_session_info(conn, "ses_1")).delegated_to == _L1

            # Hand-back: dispatched delegate, orchestrator produced result.
            await gw._core._update_delegation("ses_1", _L1, _result("orchestrator"))
            async with pool.acquire() as conn:
                assert (await queries.get_session_info(conn, "ses_1")).delegated_to is None

            # F2: a delegated turn FAILED → revert to orchestrator.
            async with pool.acquire() as conn:
                await queries.update_session_info(conn, "ses_1", delegated_to=_L1)
            await gw._core._update_delegation("ses_1", _L1, _result(_L1, TaskStatus.FAILED))
            async with pool.acquire() as conn:
                assert (await queries.get_session_info(conn, "ses_1")).delegated_to is None
        finally:
            await pool.close()

    asyncio.run(_drive())
