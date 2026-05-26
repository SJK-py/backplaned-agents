"""End-to-end smoke test exercising the happy path.

Boots an in-process bp_router (TestRouter), stands up a real external
agent (bp_sdk.Agent over WebSocket), drives admit_task → NewTask
delivery → handler invocation → Result fan-out via TestRouter.call(),
and asserts the round-trip output.

Skipped when TEST_DB_URL is not set. The test driver wraps the
async body in `asyncio.run(_drive())` so the file works on CI
matrices without `pytest-asyncio` installed (review item Test-H1
flagged a regression where the previous `pytestmark =
pytest.mark.asyncio` shape silently no-op'd the test when the
plugin was absent — pytest collected the coroutine, never awaited
it, and reported PASSED).
"""

from __future__ import annotations

import asyncio

from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext
from bp_sdk.settings import AgentConfig
from bp_sdk.testing import TestRouter


def test_echo_handler_round_trip(test_db_url: str, tmp_path) -> None:
    """Sync entry that drives the async body via `asyncio.run`. The
    `test_db_url` fixture handles skip-when-unset; once we're past
    that, this is a real integration test against a live router."""

    async def _drive() -> None:
        # No `accepts_schema` pin: the router now reads `accepts_schema`
        # as a per-mode map `{mode: schema|null}`, so a flat single
        # schema would be parsed as mode names (`type`, `properties`)
        # and admit would reject with `schema_mismatch`. Leaving it
        # unset lets the SDK auto-derive `{"LLMData": <schema>}` from
        # the handler's payload model — the shape real agents publish.
        info = AgentInfo(
            agent_id="echo_uppercaser",
            description="Echoes the prompt back, in uppercase.",
            capabilities=["text.transform.uppercase"],
        )

        user_state_dir = tmp_path / "agent_state"

        async with TestRouter(db_url=test_db_url) as router:
            # Register the agent and grab its JWT.
            token = await router.register_agent(info)

            # Boot the SDK agent against the running router.
            config = AgentConfig(
                embedded=False,
                router_url=router.ws_url,
                state_dir=user_state_dir,
                auth_token=token,
                pending_acks_timeout_s=10.0,
                pending_results_timeout_s=15.0,
            )
            agent = Agent(info=info, config=config)

            @agent.handler
            async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
                return AgentOutput(content=payload.prompt.upper())

            run_task = asyncio.create_task(agent.run_async())

            # Give the agent a moment to connect.
            for _ in range(50):
                await asyncio.sleep(0.05)
                if agent._dispatcher and agent._dispatcher.transport.is_connected:
                    break

            try:
                user = await router.create_user(level="tier0")
                result = await router.call(
                    info.agent_id,
                    LLMData(prompt="hello world"),
                    user_id=user.user_id,
                )

                assert result.status.value == "succeeded"
                assert result.status_code == 200
                assert result.output is not None
                assert result.output.content == "HELLO WORLD"
            finally:
                await agent.aclose()
                await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(_drive())
