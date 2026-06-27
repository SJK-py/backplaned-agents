"""Regression: `Agent.await_root_result` must TIME OUT, not hang, when a
progress callback is supplied (the channel/webapp verbose path).

A silent / timed-out task is settled on the result FUTURE (by the reaper),
not on the progress QUEUE the verbose iteration parks on. Driving the progress
drain alone therefore hangs past the timeout forever — wedging the webapp
session (the turn runner never finishes, the lock stays held, Stop/send can't
recover). The fix drains progress CONCURRENTLY with awaiting the result, so
`stream.result()`'s timeout is the authoritative terminator.
"""

from __future__ import annotations

import asyncio

import pytest

from bp_protocol.types import AgentInfo
from bp_sdk import Agent, ResultTimeout
from bp_sdk.peers import SpawnStream


class _SilentDispatcher:
    """`open_spawn_stream` backed by a queue + result future that NEVER receive
    a terminal frame — a silent / timed-out task."""

    def __init__(self) -> None:
        self.unsubscribed: list[str] = []

    def open_spawn_stream(self, task_id, *, timeout_s=None, maxsize=0):  # noqa: ANN001, ANN201
        fut = asyncio.get_running_loop().create_future()
        return SpawnStream(
            task_id=task_id, queue=asyncio.Queue(), result_fut=fut, dispatcher=self,
        )

    def unsubscribe_progress(self, task_id) -> None:  # noqa: ANN001
        self.unsubscribed.append(task_id)


def _agent() -> Agent:
    agent = Agent(info=AgentInfo(
        agent_id="x", description="d", groups=[], capabilities=[],
    ))
    agent._dispatcher = _SilentDispatcher()  # type: ignore[attr-defined]
    return agent


def test_await_root_result_with_progress_times_out_not_hangs() -> None:
    agent = _agent()
    seen: list = []

    async def _drive() -> None:
        # Guard: if the verbose path hangs (the bug), the OUTER wait_for trips
        # at 5s with asyncio.TimeoutError instead of the expected ResultTimeout.
        await asyncio.wait_for(
            agent.await_root_result(
                "tsk", timeout_s=0.2, on_progress=seen.append
            ),
            timeout=5,
        )

    with pytest.raises(ResultTimeout):
        asyncio.run(_drive())


def test_await_root_result_no_callback_times_out() -> None:
    # The non-verbose path was already correct; assert it too for parity.
    agent = _agent()

    async def _drive() -> None:
        await asyncio.wait_for(
            agent.await_root_result("tsk", timeout_s=0.2), timeout=5
        )

    with pytest.raises(ResultTimeout):
        asyncio.run(_drive())
