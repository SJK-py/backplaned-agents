"""SDK buffers ProgressFrames that arrive BEFORE `subscribe_progress`.

R6 third-pass review (HIGH): `peers.spawn(.., stream=True)` calls
`subscribe_progress` only AFTER the spawn Ack lands. The router
can begin streaming ProgressFrames immediately after admit, so a
race window exists where `_handle_progress` finds no subscriber
and silently drops the frame on the floor. The SpawnStream
consumer would then see fewer ProgressFrames than the router
emitted.

R6 fix: a small per-task buffer in the dispatcher catches frames
arriving before `subscribe_progress`. `subscribe_progress` drains
the buffer into the new queue. The terminal Result frame drops
the buffer (no subscriber will ever arrive on the non-stream
path).
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest


def _make_dispatcher() -> object:
    """Build a Dispatcher without any transport wiring."""
    pytest.importorskip("pydantic")
    from bp_sdk import dispatch

    agent = MagicMock()
    agent.info.agent_id = "agt_test"
    agent.config.pending_buffer_window_s = 0.05
    agent.config.pending_buffer_max_size = 64
    agent.config.pending_acks_timeout_s = 5.0
    agent.config.pending_results_timeout_s = 5.0
    agent.config.recv_consecutive_failures_max = 4

    transport = MagicMock()
    return dispatch.Dispatcher(agent, transport)


def _make_progress_frame(task_id: str, content: str = "x") -> object:
    pytest.importorskip("pydantic")
    from bp_protocol.frames import ProgressFrame

    return ProgressFrame(
        agent_id="agt_x",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id=task_id,
        event="chunk",
        content=content,
    )


def test_subscribe_drains_pre_subscribe_buffer() -> None:
    """Functional pin for the bug: frames that arrived BEFORE the
    subscribe land in the new queue, in arrival order."""
    pytest.importorskip("pydantic")

    async def _run() -> None:
        disp = _make_dispatcher()

        # Three progress frames arrive while there's no subscriber.
        for i in range(3):
            await disp._handle_progress(
                _make_progress_frame("task_abc", f"chunk_{i}")
            )

        # NOW subscribe — should drain the buffered frames.
        queue = disp.subscribe_progress("task_abc")
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())

        assert [f.content for f in out] == ["chunk_0", "chunk_1", "chunk_2"]
        # Buffer was popped on subscribe.
        assert "task_abc" not in disp._pending_progress_buffer

    asyncio.run(_run())


def test_progress_after_subscribe_bypasses_buffer() -> None:
    """Sanity: once subscribed, frames flow direct to the queue
    and do NOT touch the buffer."""
    pytest.importorskip("pydantic")

    async def _run() -> None:
        disp = _make_dispatcher()
        queue = disp.subscribe_progress("task_xyz")
        await disp._handle_progress(_make_progress_frame("task_xyz", "live"))

        assert queue.qsize() == 1
        # Buffer never populated.
        assert "task_xyz" not in disp._pending_progress_buffer

    asyncio.run(_run())


def test_per_task_buffer_cap_drops_excess() -> None:
    """Bounded buffer: past the per-task cap, additional frames
    drop with a warning rather than growing unbounded."""
    pytest.importorskip("pydantic")
    from bp_sdk.dispatch import Dispatcher

    async def _run() -> None:
        disp = _make_dispatcher()
        cap = Dispatcher._PROGRESS_BUFFER_PER_TASK
        for i in range(cap + 5):
            await disp._handle_progress(
                _make_progress_frame("task_overflow", f"chunk_{i}")
            )
        # Only the first `cap` frames survive.
        assert len(disp._pending_progress_buffer["task_overflow"]) == cap

    asyncio.run(_run())


def test_total_task_cap_evicts_oldest_orphan_fifo() -> None:
    """The total-task cap stays bounded, but R9 changed the policy
    from "drop the NEW task_id" to "evict the OLDEST and admit the
    new one" (FIFO via dict insertion order). The old policy let a
    burst of never-subscribed task_ids permanently pin all 256
    slots until each task's unrelated Result landed, starving every
    legitimate to-be-subscribed task. FIFO eviction keeps the
    buffer bounded AND lets an orphan occupy a slot only until
    `_PROGRESS_BUFFER_MAX_TASKS` newer task_ids appear."""
    pytest.importorskip("pydantic")
    from bp_sdk.dispatch import Dispatcher

    async def _run() -> None:
        disp = _make_dispatcher()
        cap = Dispatcher._PROGRESS_BUFFER_MAX_TASKS
        for i in range(cap):
            await disp._handle_progress(_make_progress_frame(f"task_{i}"))
        assert len(disp._pending_progress_buffer) == cap
        # task_0 is the oldest (first buffered).
        assert "task_0" in disp._pending_progress_buffer

        # One more distinct task_id: buffer stays at the cap, the
        # NEW one is admitted, the OLDEST (task_0) is evicted.
        await disp._handle_progress(_make_progress_frame("task_overflow"))
        assert len(disp._pending_progress_buffer) == cap
        assert "task_overflow" in disp._pending_progress_buffer
        assert "task_0" not in disp._pending_progress_buffer
        # The next-oldest survivor is still present.
        assert "task_1" in disp._pending_progress_buffer

        # A second overflow evicts the new oldest (task_1).
        await disp._handle_progress(_make_progress_frame("task_overflow2"))
        assert len(disp._pending_progress_buffer) == cap
        assert "task_overflow2" in disp._pending_progress_buffer
        assert "task_1" not in disp._pending_progress_buffer

    asyncio.run(_run())


def test_result_frame_drops_pre_subscribe_buffer() -> None:
    """Terminal Result clears the buffer. A non-stream spawner
    will never subscribe; the buffered frames would otherwise
    leak in `_pending_progress_buffer` indefinitely."""
    pytest.importorskip("pydantic")
    from bp_protocol.frames import ResultFrame

    async def _run() -> None:
        disp = _make_dispatcher()
        await disp._handle_progress(_make_progress_frame("task_done"))
        assert "task_done" in disp._pending_progress_buffer

        # Result arrives. Dispatch through _dispatch so we hit the
        # pop logic on the ResultFrame branch.
        result = ResultFrame(
            agent_id="agt_x",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="task_done",
            correlation_id="c1",
            status="succeeded",
            status_code=200,
        )
        await disp._dispatch(result)

        assert "task_done" not in disp._pending_progress_buffer

    asyncio.run(_run())


def test_subscribe_after_result_does_not_replay() -> None:
    """A late subscribe (e.g. spawner retried subscribing) after
    Result already arrived must NOT replay the (now-flushed)
    buffer. Confirms the Result-side drop happens correctly."""
    pytest.importorskip("pydantic")
    from bp_protocol.frames import ResultFrame

    async def _run() -> None:
        disp = _make_dispatcher()
        await disp._handle_progress(_make_progress_frame("task_x"))

        result = ResultFrame(
            agent_id="agt_x",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="task_x",
            correlation_id="c2",
            status="succeeded",
            status_code=200,
        )
        await disp._dispatch(result)

        queue = disp.subscribe_progress("task_x")
        assert queue.empty()

    asyncio.run(_run())


def test_subscribe_with_maxsize_respects_queue_cap_on_drain() -> None:
    """If the buffer is bigger than the subscriber's queue maxsize,
    drain stops at the cap and logs. The bounded buffer keeps the
    invariant that no path can build an unbounded asyncio.Queue."""
    pytest.importorskip("pydantic")

    async def _run() -> None:
        disp = _make_dispatcher()
        # Buffer 5 frames, then subscribe with maxsize=3.
        for i in range(5):
            await disp._handle_progress(
                _make_progress_frame("task_y", f"c{i}")
            )

        queue = disp.subscribe_progress("task_y", maxsize=3)
        # Queue receives 3, drain stops.
        assert queue.qsize() == 3

    asyncio.run(_run())


def test_source_pin_handle_progress_buffers_on_miss() -> None:
    """Source pin: `_handle_progress` calls `_buffer_pending_progress`
    when there's no subscriber. A regression that reverts to silent
    drop fails this pin."""
    pytest.importorskip("pydantic")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._handle_progress)
    assert "_buffer_pending_progress" in src


def test_source_pin_subscribe_drains_buffer() -> None:
    """Source pin: `subscribe_progress` drains the buffer into the
    new queue."""
    pytest.importorskip("pydantic")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher.subscribe_progress)
    assert "_pending_progress_buffer.pop" in src


# ---------------------------------------------------------------------------
# Wait-only spawn (stream=False): progress is dropped, not buffered.
# A pending RESULT with no progress subscriber means the caller opted out of
# progress, so chatty subagents (research/web-search) must not flood the
# per-task buffer cap.
# ---------------------------------------------------------------------------


def test_wait_only_spawn_progress_is_dropped_not_buffered() -> None:
    async def _run() -> None:
        disp = _make_dispatcher()
        # Simulate a wait-only spawn: a pending Result, no progress subscriber.
        disp.pending_results.register("task_waitonly")
        for i in range(5):
            await disp._handle_progress(
                _make_progress_frame("task_waitonly", f"c{i}")
            )
        # Dropped silently — never buffered.
        assert "task_waitonly" not in disp._pending_progress_buffer

    asyncio.run(_run())


def test_no_subscriber_and_no_pending_result_still_buffers() -> None:
    """The pre-subscribe race (streamed spawn) is unaffected: with neither a
    subscriber nor a pending result yet, frames buffer for the imminent
    `subscribe_progress` to drain."""
    async def _run() -> None:
        disp = _make_dispatcher()
        await disp._handle_progress(_make_progress_frame("task_race", "c0"))
        assert "task_race" in disp._pending_progress_buffer

    asyncio.run(_run())


def test_pending_map_contains() -> None:
    from bp_sdk.correlation import PendingMap

    async def _run() -> None:
        pm = PendingMap(default_timeout_s=5.0)
        assert "k" not in pm
        pm.register("k")
        assert "k" in pm
        pm.resolve("k", object())
        assert "k" not in pm

    asyncio.run(_run())

