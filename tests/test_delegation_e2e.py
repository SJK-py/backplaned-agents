"""Delegation end-to-end over a live router.

Proves the router mechanics the unit tests can't: the orchestrator hands
off (delegate → task reassignment), deep_reasoning becomes the active
executor and produces the terminal Result, and the orchestrator's
now-stale Result is dropped (exactly-one-Result). A message-inspecting
stub adapter drives the two agents' LLM calls (no provider key).

Requires TEST_DB_URL + SUITE_DATABASE_URL (set before import for the
agent modules).
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.deep_reasoning.agent import agent as deep_reasoning_agent
from bp_agents.agents.orchestrator.agent import agent as orchestrator_agent
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.types import AgentInfo, TaskStatus
from bp_router.llm.service import LlmResponse, TokenUsage, ToolCall
from bp_sdk import Agent
from bp_sdk.settings import AgentConfig
from bp_sdk.testing import TestRouter


class _DelegatingAdapter:
    """Orchestrator (general instruction) → hand_off; any specialist → text."""

    provider_name = "stub"
    concrete_model = "stub-model"

    async def generate(self, messages, **kw):
        system = messages[0].content if messages else ""
        if "personal assistant" in system:
            return LlmResponse(
                text="",
                tool_calls=[ToolCall(
                    id="h", name="hand_off",
                    args={"agent_id": "deep_reasoning", "instruction": "reason it out"},
                )],
                usage=TokenUsage(),
            )
        return LlmResponse(text="delegated reasoning result", usage=TokenUsage())

    async def embed(self, text):
        return [[0.0]]

    async def count_tokens(self, messages):
        return 1


async def _wait(agent: Agent) -> None:
    for _ in range(100):
        await asyncio.sleep(0.05)
        if agent._dispatcher and agent._dispatcher.transport.is_connected:
            return
    raise AssertionError(f"{agent.info.agent_id} never connected")


def test_delegation_round_trip(test_db_url: str, suite_db_url: str, tmp_path) -> None:
    async def _drive() -> None:
        suite_pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        async with TestRouter(db_url=test_db_url) as router:
            router._app.state.bp.llm_service._build_adapter = (
                lambda resolved: _DelegatingAdapter()
            )
            router._app.state.bp.llm_service._adapters.clear()

            orch_tok = await router.register_agent(orchestrator_agent.info)
            dr_tok = await router.register_agent(deep_reasoning_agent.info)
            chan_info = AgentInfo(
                agent_id="chatbot", description="c", groups=["channel", "inbound"],
                capabilities=["channel.telegram"], hidden=True,
            )
            chan_tok = await router.register_agent(chan_info)

            for ag, tok, sub in (
                (orchestrator_agent, orch_tok, "orch"),
                (deep_reasoning_agent, dr_tok, "dr"),
            ):
                ag.config.router_url = router.ws_url
                ag.config.auth_token = tok
                ag.config.state_dir = tmp_path / sub
                ag.config.embedded = False

            channel = Agent(info=chan_info, config=AgentConfig(
                embedded=False, router_url=router.ws_url, state_dir=tmp_path / "chan",
                auth_token=chan_tok, pending_acks_timeout_s=10.0,
                pending_results_timeout_s=20.0,
            ))

            # Reset shared module-global agents' stop events (a prior e2e
            # in the same process may have set them on aclose).
            orchestrator_agent._stop_event = asyncio.Event()
            deep_reasoning_agent._stop_event = asyncio.Event()
            runs = [
                asyncio.create_task(orchestrator_agent.run_async()),
                asyncio.create_task(deep_reasoning_agent.run_async()),
                asyncio.create_task(channel.run_async()),
            ]
            try:
                await _wait(orchestrator_agent)
                await _wait(deep_reasoning_agent)
                await _wait(channel)

                user = await router.create_user(level="tier0")
                session_id = await router.open_session(user_id=user.user_id)
                async with suite_pool.acquire() as conn:
                    await conn.execute(
                        "TRUNCATE TABLE session_history, session_info, user_config, "
                        "suite_platform_mappings RESTART IDENTITY"
                    )
                    await queries.create_user_config(
                        conn, user_id=user.user_id, default_session_id=session_id
                    )
                    await queries.create_session_info(
                        conn, session_id=session_id, user_id=user.user_id,
                        channel="chatbot_telegram",
                    )

                task_id = await channel.spawn_root_for_user(
                    "orchestrator", MessagePayload(prompt="help me think"),
                    user_id=user.user_id, session_id=session_id, mode="message",
                )
                result = await channel.await_root_result(task_id, timeout_s=20.0)

                # The delegate produced the terminal Result (hand-off worked
                # + the orchestrator's stale Result was dropped).
                assert result.status == TaskStatus.SUCCEEDED
                assert result.agent_id == "deep_reasoning"
                assert result.output.content == "delegated reasoning result"

                # Seed row + deep_reasoning's assistant turn landed.
                async with suite_pool.acquire() as conn:
                    rows = await queries.reload_incumbent(
                        conn, session_id=session_id, agent_id="deep_reasoning"
                    )
                assert [r.role for r in rows][-1] == "assistant"
            finally:
                await channel.aclose()
                await orchestrator_agent.aclose()
                await deep_reasoning_agent.aclose()
                for t in runs:
                    await asyncio.wait_for(t, timeout=5.0)
        await suite_pool.close()

    asyncio.run(_drive())
