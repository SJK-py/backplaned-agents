"""Delegation lifecycle — orchestrator hand-off / hand-back, l1 delegated
turn, and the channel's delegated_to maintenance.

Agent-side pieces use a scripted stub LLM + stub peers (records
delegate/visible) + a live suite DB. The gateway delegated_to logic is
driven directly with synthetic ResultFrames.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.agents.l1_common import L1Config, run_delegated_turn
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


class _Ctx:
    def __init__(self, llm, peers, *, user_id="usr_a", session_id="ses_1", delegating=None):
        self.llm = llm
        self.peers = peers
        self.progress = _StubProgress()
        self.user_id = user_id
        self.session_id = session_id
        self.user_level = "tier0"
        self.delegating_agent_id = delegating


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
            assert rows and rows[0].role == "user"
            assert "reason about X" in rows[0].message
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

            # Normal turn → appends assistant row, no hand-back.
            peers = _StubPeers()
            ctx = _Ctx(_StubLlm([LlmResponse(text="here is my reasoning")]), peers)
            out = await run_delegated_turn(
                ctx, config=cfg, pool=pool, settings=_settings(suite_db_url)
            )
            assert out.content == "here is my reasoning"
            assert peers.delegations == []
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
            assert [r.role for r in rows][-1] == "assistant"

            # end_delegation → hands back to the orchestrator, result-less.
            peers2 = _StubPeers()
            ctx2 = _Ctx(_StubLlm([LlmResponse(text="", tool_calls=[ToolCall(
                id="c2", name="end_delegation",
                args={"delegation_summary": "did it", "exit_reason": "done"},
            )])]), peers2)
            out2 = await run_delegated_turn(
                ctx2, config=cfg, pool=pool, settings=_settings(suite_db_url)
            )
            assert out2.content is None or out2.content == ""
            assert len(peers2.delegations) == 1
            dest, _payload, mode = peers2.delegations[0]
            assert dest == "orchestrator" and mode == "end_delegation"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_end_delegation_recap(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            # An l1 episode to retire.
            async with pool.acquire() as conn:
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
                # Recap appended to the MAIN thread.
                main = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                # The delegate episode was retired (no incumbent rows).
                l1_rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=_L1
                )
            assert any("solved it" in r.message for r in main)
            assert l1_rows == []
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
            await gw._update_delegation("ses_1", "orchestrator", _result(_L1))
            async with pool.acquire() as conn:
                assert (await queries.get_session_info(conn, "ses_1")).delegated_to == _L1

            # Hand-back: dispatched delegate, orchestrator produced result.
            await gw._update_delegation("ses_1", _L1, _result("orchestrator"))
            async with pool.acquire() as conn:
                assert (await queries.get_session_info(conn, "ses_1")).delegated_to is None

            # F2: a delegated turn FAILED → revert to orchestrator.
            async with pool.acquire() as conn:
                await queries.update_session_info(conn, "ses_1", delegated_to=_L1)
            await gw._update_delegation("ses_1", _L1, _result(_L1, TaskStatus.FAILED))
            async with pool.acquire() as conn:
                assert (await queries.get_session_info(conn, "ses_1")).delegated_to is None
        finally:
            await pool.close()

    asyncio.run(_drive())
