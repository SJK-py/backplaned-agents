"""Phase 1 end-to-end: Telegram message → orchestrator → reply.

Proves the vertical slice composed: the real ChatbotGateway resolves
identity, writes the user turn, and injects a root task over a live
router (`spawn_root_for_user`); the real orchestrator agent admits it,
rebuilds context from suite history, calls the LLM (stubbed at the
router so no provider key is needed), persists its reply, and returns
it; the gateway relays it to (fake) Telegram.

Requires both TEST_DB_URL (router) and SUITE_DATABASE_URL (suite),
schemas applied. SUITE_DATABASE_URL must be set before import so the
orchestrator module reads it.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.agents.orchestrator import ORCHESTRATOR_AGENT_ID
from bp_agents.agents.orchestrator.agent import agent as orchestrator_agent
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.types import AgentInfo
from bp_router.llm.service import LlmResponse, TokenUsage
from bp_sdk import Agent
from bp_sdk.settings import AgentConfig
from bp_sdk.testing import TestRouter

_REPLY = "Hello from the orchestrator!"


class _StubAdapter:
    """Router-side ProviderAdapter stand-in — returns a fixed reply so
    the orchestrator's `ctx.llm.generate` works without a provider key."""

    provider_name = "stub"
    concrete_model = "stub-model"

    async def generate(self, messages, **kw):
        return LlmResponse(
            text=_REPLY, usage=TokenUsage(input_tokens=1, output_tokens=1)
        )

    async def embed(self, text):
        return [[0.0]]

    async def count_tokens(self, messages):
        return 1


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


async def _wait_connected(agent: Agent) -> None:
    for _ in range(100):
        await asyncio.sleep(0.05)
        if agent._dispatcher and agent._dispatcher.transport.is_connected:
            return
    raise AssertionError(f"{agent.info.agent_id} never connected")


def test_phase1_message_round_trip(
    test_db_url: str, suite_db_url: str, tmp_path
) -> None:
    async def _drive() -> None:
        suite = SuiteSettings(database_url=suite_db_url)
        suite_pool = await open_pool(suite)
        async with TestRouter(db_url=test_db_url) as router:
            # Stub the router LLM: any preset resolves to the stub adapter.
            svc = router._app.state.bp.llm_service
            svc._build_adapter = lambda resolved: _StubAdapter()
            svc._adapters.clear()

            # Register both agents and point the orchestrator module's
            # agent at this router.
            orch_token = await router.register_agent(orchestrator_agent.info)
            channel_info = AgentInfo(
                agent_id="chatbot",
                description="e2e channel",
                groups=["channel", "inbound"],
                capabilities=["channel.telegram"],
                hidden=True,
            )
            chan_token = await router.register_agent(channel_info)

            orchestrator_agent.config.router_url = router.ws_url
            orchestrator_agent.config.auth_token = orch_token
            orchestrator_agent.config.state_dir = tmp_path / "orch"
            orchestrator_agent.config.embedded = False

            channel = Agent(
                info=channel_info,
                config=AgentConfig(
                    embedded=False, router_url=router.ws_url,
                    state_dir=tmp_path / "chan", auth_token=chan_token,
                    pending_acks_timeout_s=10.0, pending_results_timeout_s=15.0,
                ),
            )

            # Reset the shared module-global agent's stop event (a prior
            # e2e in the same process may have set it on aclose).
            orchestrator_agent._stop_event = asyncio.Event()
            orch_run = asyncio.create_task(orchestrator_agent.run_async())
            chan_run = asyncio.create_task(channel.run_async())
            try:
                await _wait_connected(orchestrator_agent)
                await _wait_connected(channel)

                # Provision the user: a router user + owned session, plus
                # the suite-side identity the gateway resolves.
                user = await router.create_user(level="tier0")
                session_id = await router.open_session(user_id=user.user_id)
                async with suite_pool.acquire() as conn:
                    await conn.execute(
                        "TRUNCATE TABLE session_history, session_info, "
                        "user_config, suite_platform_mappings RESTART IDENTITY"
                    )
                    await queries.upsert_platform_mapping(
                        conn, platform="telegram", chat_id="tg-e2e",
                        user_id=user.user_id,
                    )
                    await queries.create_user_config(
                        conn, user_id=user.user_id, default_session_id=session_id
                    )
                    await queries.create_session_info(
                        conn, session_id=session_id, user_id=user.user_id,
                        channel="chatbot_telegram", chat_id="tg-e2e",
                    )

                tg = _FakeTelegram()
                gateway = ChatbotGateway(
                    dispatcher=channel, pool=suite_pool, telegram=tg,
                    result_timeout_s=20.0,
                )

                await gateway.handle_update("tg-e2e", "hello there")

                # The orchestrator's stubbed reply was relayed.
                assert tg.sent == [("tg-e2e", _REPLY)]

                # History holds the user turn (channel) + assistant turn
                # (orchestrator), both on the orchestrator thread.
                async with suite_pool.acquire() as conn:
                    rows = await queries.reload_incumbent(
                        conn, session_id=session_id,
                        agent_id=ORCHESTRATOR_AGENT_ID,
                    )
                assert [(r.role, r.message) for r in rows] == [
                    ("user", "hello there"),
                    ("assistant", _REPLY),
                ]
            finally:
                await channel.aclose()
                await orchestrator_agent.aclose()
                await asyncio.wait_for(chan_run, timeout=5.0)
                await asyncio.wait_for(orch_run, timeout=5.0)
        await suite_pool.close()

    asyncio.run(_drive())
