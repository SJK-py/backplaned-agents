"""Graceful shutdown keeps the recv loop alive through the drain.

Pre-release review: on a clean stop, `run_until`'s `asyncio.wait(...)`
returned with the recv loop still pending, and the code cancelled it BEFORE
`_drain_in_flight(grace_s=30)`. With the recv loop dead, no inbound frames
(LlmResult / child Result / Ack) were dispatched during the grace window, so
any in-flight handler blocked on `await ctx.llm.generate(...)` /
`await ctx.peers.spawn(wait=True)` — the common shapes — could never complete
cooperatively and was hard-cancelled at the 30s deadline.

Fix: the recv loop is kept running through the drain (cancelled only in the
`finally`); a `_draining` flag stops admitting NEW tasks. A dead transport
can deliver nothing, so its drain uses grace 0.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest


def _dispatcher(transport: Any, *, max_fail: int = 2):  # type: ignore[no-untyped-def]
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch
    from bp_sdk.correlation import PendingMap

    disp = dispatch.Dispatcher.__new__(dispatch.Dispatcher)
    disp.transport = transport  # type: ignore[assignment]
    disp.agent = SimpleNamespace(  # type: ignore[assignment]
        config=SimpleNamespace(recv_consecutive_failures_max=max_fail),
        info=SimpleNamespace(agent_id="agt_x"),
    )
    disp.pending_acks = PendingMap(default_timeout_s=60.0)
    disp.pending_results = PendingMap(default_timeout_s=60.0)
    disp._active = {}
    disp._loops = []
    disp._draining = False
    return disp


class _BlockRecv:
    """recv() parks forever — a healthy idle socket."""

    async def recv(self) -> Any:
        await asyncio.Event().wait()


class _AlwaysFailRecv:
    def __init__(self) -> None:
        self.calls = 0

    async def recv(self) -> Any:
        self.calls += 1
        raise RuntimeError(f"transport bug {self.calls}")


def test_recv_loop_alive_during_drain_on_clean_stop() -> None:
    """On a clean stop with a live transport, the recv loop must STILL be
    running when `_drain_in_flight` begins (so it can deliver the frames that
    unblock in-flight handlers), and the drain gets the full 30s grace."""
    disp = _dispatcher(_BlockRecv())
    observed: dict[str, Any] = {}

    async def _probe(*, grace_s: float, hard_drain_timeout_s: float = 5.0) -> None:
        recv_loop = disp._loops[0]
        observed["recv_alive_at_drain"] = not recv_loop.done()
        observed["grace"] = grace_s

    disp._drain_in_flight = _probe  # type: ignore[assignment]

    async def _drive() -> None:
        stop = asyncio.Event()
        stop.set()  # external clean stop
        await disp.run_until(stop)

    asyncio.run(_drive())

    assert observed["recv_alive_at_drain"] is True, (
        "recv loop was cancelled before the drain — in-flight handlers can't "
        "receive the frames they're awaiting"
    )
    assert observed["grace"] == 30.0
    assert disp._draining is True  # admission was closed
    # recv loop is cancelled by the finally, after the drain.
    assert disp._loops[0].cancelled() or disp._loops[0].done()


def test_dead_transport_drain_uses_zero_grace() -> None:
    """When the recv loop died (permanent transport failure), the drain can't
    receive anything — so it must not burn the full grace; grace is 0."""
    pytest.importorskip("fastapi")
    from bp_sdk.errors import TransportPermanentlyFailed

    disp = _dispatcher(_AlwaysFailRecv(), max_fail=1)
    observed: dict[str, Any] = {}

    async def _probe(*, grace_s: float, hard_drain_timeout_s: float = 5.0) -> None:
        observed["grace"] = grace_s

    disp._drain_in_flight = _probe  # type: ignore[assignment]

    async def _drive() -> None:
        import bp_sdk.dispatch as d

        real = asyncio.sleep

        async def _fast(_s: float) -> None:
            await real(0)

        d.asyncio.sleep = _fast  # type: ignore[assignment]
        try:
            stop = asyncio.Event()  # never set → recv death is the exit
            with pytest.raises(TransportPermanentlyFailed):
                await disp.run_until(stop)
        finally:
            d.asyncio.sleep = real  # type: ignore[assignment]

    asyncio.run(_drive())
    assert observed["grace"] == 0.0


def test_draining_rejects_new_task() -> None:
    """While draining, a newly-arriving NewTask is refused (accepted=False,
    reason agent_shutting_down) instead of being admitted and then
    hard-cancelled at the deadline."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import NewTaskFrame

    sent: list[Any] = []

    class _CaptureTransport:
        async def send(self, frame: Any) -> None:
            sent.append(frame)

        async def recv(self) -> Any:
            await asyncio.Event().wait()

    disp = _dispatcher(_CaptureTransport())
    disp._draining = True

    frame = NewTaskFrame(
        trace_id="0" * 32, span_id="0" * 16, agent_id="agt_caller",
        destination_agent_id="agt_x", user_id="usr_a", session_id="ses_1",
        correlation_id="corr-xyz",
    )
    asyncio.run(disp._handle_new_task(frame))

    assert len(sent) == 1
    ack = sent[0]
    assert ack.accepted is False
    assert ack.reason == "agent_shutting_down"
    assert ack.ref_correlation_id == "corr-xyz"


# --------------------------------------------------------------------------- #
# Source pins
# --------------------------------------------------------------------------- #


def test_run_until_does_not_cancel_recv_before_drain_sourcepin() -> None:
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher.run_until)
    # The pre-fix `for t in pending: t.cancel()` (which cancelled the recv
    # loop before the drain) must be gone.
    assert "for t in pending" not in src
    assert "self._draining = True" in src
    # Live transport → full grace; dead transport → 0.
    assert "grace_s=30.0 if recv_alive else 0.0" in src


def test_handle_new_task_has_draining_gate_sourcepin() -> None:
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._handle_new_task)
    assert "if self._draining:" in src
    assert "agent_shutting_down" in src
