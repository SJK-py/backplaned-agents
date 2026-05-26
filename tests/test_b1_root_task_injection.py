"""B1 root-task injection — `Agent.spawn_root_for_user` / `await_root_result`.

The agent-suite channel/gateway needs to inject a user turn as a
*parentless* task carrying the END USER's `(user_id, session_id)` over
its own WS — something `peers.spawn` can't do (it's handler-bound and
inherits `parent_task_id` from the running task). This exercises the
SDK helper end-to-end against a live router:

  gateway.spawn_root_for_user(dest, payload, user_id=…, session_id=…)
     → router admits the parentless task (caller = gateway agent)
     → dest handler runs, streams progress, returns a Result
     → gateway.await_root_result(task_id) gets the terminal frame.

Mirrors `test_smoke_e2e.py`'s harness (TestRouter + real SDK agents
over WebSocket; `asyncio.run` driver so the file works on CI matrices
without pytest-asyncio).
"""

from __future__ import annotations

import asyncio

from bp_protocol.frames import ProgressFrame
from bp_protocol.types import AgentInfo, AgentOutput, LLMData, TaskStatus
from bp_sdk import Agent, SpawnRejected, TaskContext
from bp_sdk.settings import AgentConfig
from bp_sdk.testing import TestRouter


def _agent_over_ws(info: AgentInfo, *, router: TestRouter, token: str, state_dir):
    return Agent(
        info=info,
        config=AgentConfig(
            embedded=False,
            router_url=router.ws_url,
            state_dir=state_dir,
            auth_token=token,
            pending_acks_timeout_s=10.0,
            pending_results_timeout_s=15.0,
        ),
    )


async def _wait_connected(agent: Agent) -> None:
    for _ in range(100):
        await asyncio.sleep(0.05)
        if agent._dispatcher and agent._dispatcher.transport.is_connected:
            return
    raise AssertionError(f"{agent.info.agent_id} never connected")


def test_spawn_root_for_user_round_trip(test_db_url: str, tmp_path) -> None:
    """Happy path: a gateway injects a parentless task on behalf of a
    user; the destination's Result + Progress fan back to the gateway."""

    async def _drive() -> None:
        dest_info = AgentInfo(
            agent_id="echo_dest",
            description="Echoes the prompt back, uppercased.",
            capabilities=["text.transform.uppercase"],
        )
        gateway_info = AgentInfo(
            agent_id="gateway_chan",
            description="Gateway that injects root tasks for users.",
            groups=["channel"],
            capabilities=["channel.telegram"],
        )

        async with TestRouter(db_url=test_db_url) as router:
            dest_token = await router.register_agent(dest_info)
            gw_token = await router.register_agent(gateway_info)

            dest = _agent_over_ws(
                dest_info, router=router, token=dest_token,
                state_dir=tmp_path / "dest_state",
            )
            gateway = _agent_over_ws(
                gateway_info, router=router, token=gw_token,
                state_dir=tmp_path / "gw_state",
            )

            @dest.handler
            async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
                # Root task ⇒ delivered with no parent.
                assert ctx.parent_task_id is None
                ctx.progress.status("working")
                return AgentOutput(content=payload.prompt.upper())

            dest_run = asyncio.create_task(dest.run_async())
            gw_run = asyncio.create_task(gateway.run_async())

            try:
                await _wait_connected(dest)
                await _wait_connected(gateway)

                user = await router.create_user(level="tier0")
                session_id = await router.open_session(user_id=user.user_id)

                task_id = await gateway.spawn_root_for_user(
                    "echo_dest",
                    LLMData(prompt="hello b1"),
                    user_id=user.user_id,
                    session_id=session_id,
                    ack_timeout_s=10.0,
                )
                assert isinstance(task_id, str) and task_id

                seen: list[ProgressFrame] = []
                result = await gateway.await_root_result(
                    task_id,
                    timeout_s=15.0,
                    on_progress=seen.append,
                )

                assert result.status == TaskStatus.SUCCEEDED
                assert result.status_code == 200
                assert result.output is not None
                assert result.output.content == "HELLO B1"
                # Root task: the terminal frame carries no parent.
                assert result.parent_task_id is None
                # Progress fanned back to the admitting gateway.
                assert any(pf.event == "status" for pf in seen)
            finally:
                await gateway.aclose()
                await dest.aclose()
                await asyncio.wait_for(gw_run, timeout=5.0)
                await asyncio.wait_for(dest_run, timeout=5.0)

    asyncio.run(_drive())


def test_spawn_root_for_user_rejects_unknown_session(
    test_db_url: str, tmp_path
) -> None:
    """A `(user_id, session_id)` that isn't a real owned session is
    refused at admit — surfaced as `SpawnRejected`, not a hang."""

    async def _drive() -> None:
        gateway_info = AgentInfo(
            agent_id="gateway_chan",
            description="Gateway.",
            groups=["channel"],
            capabilities=["channel.telegram"],
        )
        dest_info = AgentInfo(
            agent_id="echo_dest",
            description="Echo.",
            capabilities=["text.transform.uppercase"],
        )

        async with TestRouter(db_url=test_db_url) as router:
            dest_token = await router.register_agent(dest_info)
            gw_token = await router.register_agent(gateway_info)

            dest = _agent_over_ws(
                dest_info, router=router, token=dest_token,
                state_dir=tmp_path / "dest_state",
            )
            gateway = _agent_over_ws(
                gateway_info, router=router, token=gw_token,
                state_dir=tmp_path / "gw_state",
            )

            @dest.handler
            async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
                return AgentOutput(content=payload.prompt.upper())

            dest_run = asyncio.create_task(dest.run_async())
            gw_run = asyncio.create_task(gateway.run_async())

            try:
                await _wait_connected(dest)
                await _wait_connected(gateway)

                user = await router.create_user(level="tier0")

                raised = False
                try:
                    await gateway.spawn_root_for_user(
                        "echo_dest",
                        LLMData(prompt="nope"),
                        user_id=user.user_id,
                        session_id="ses_does_not_exist",
                        ack_timeout_s=10.0,
                    )
                except SpawnRejected:
                    raised = True
                assert raised, "expected SpawnRejected for an unknown session"
            finally:
                await gateway.aclose()
                await dest.aclose()
                await asyncio.wait_for(gw_run, timeout=5.0)
                await asyncio.wait_for(dest_run, timeout=5.0)

    asyncio.run(_drive())
