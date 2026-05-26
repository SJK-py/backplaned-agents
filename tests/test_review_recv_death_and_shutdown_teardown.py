"""Audit HIGH-1 + HIGH-2: recv-death exit code & shutdown teardown.

HIGH-1  `_recv_loop` `return`ed after `recv_consecutive_failures_max`
   consecutive failures. `run_until`'s `asyncio.wait` can't tell a
   give-up from a clean stop, so `run_async`/`run()` exited 0 — a
   fleet on `systemd Restart=on-failure` never restarted a
   permanently-dead agent. Now `_recv_loop` raises
   `TransportPermanentlyFailed`, `run_until` re-raises it, and
   `Agent.run()` maps it to `SystemExit(1)`.

HIGH-2  `run_until` tracked only the recv loop; the two correlation
   reapers were orphaned on shutdown ("Task was destroyed but it is
   pending") and the pending maps were never rejected, so any
   peer/LLM/ack future not covered by the in-flight drain hung to
   its full correlation timeout. Teardown now runs in a `finally`:
   stop both reapers + `reject_all` both maps.
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
        config=SimpleNamespace(recv_consecutive_failures_max=max_fail)
    )
    disp.pending_acks = PendingMap(default_timeout_s=60.0)
    disp.pending_results = PendingMap(default_timeout_s=60.0)
    disp._active = {}
    disp._loops = []
    return disp


class _AlwaysFailRecv:
    def __init__(self) -> None:
        self.calls = 0

    async def recv(self) -> Any:
        self.calls += 1
        raise RuntimeError(f"transport bug {self.calls}")


class _BlockRecv:
    """recv() parks forever — simulates a healthy idle socket."""

    async def recv(self) -> Any:
        await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# HIGH-1 — permanent recv death surfaces, non-zero exit
# ---------------------------------------------------------------------------


def test_recv_loop_raises_terminal_not_silent_return() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.errors import TransportPermanentlyFailed

    disp = _dispatcher(_AlwaysFailRecv(), max_fail=2)

    async def _drive() -> None:
        # Patch the loop's backoff sleep so the test is instant.
        import bp_sdk.dispatch as d

        real = asyncio.sleep

        async def _fast(_s: float) -> None:
            await real(0)

        d.asyncio.sleep = _fast  # type: ignore[assignment]
        try:
            await disp._recv_loop()
        finally:
            d.asyncio.sleep = real  # type: ignore[assignment]

    with pytest.raises(TransportPermanentlyFailed):
        asyncio.run(_drive())


def test_run_until_reraises_recv_death_after_teardown() -> None:
    """run_until must propagate the terminal error AND still run the
    HIGH-2 teardown (reapers stopped, pending maps rejected)."""
    pytest.importorskip("fastapi")
    from bp_sdk.errors import TransportError, TransportPermanentlyFailed

    disp = _dispatcher(_AlwaysFailRecv(), max_fail=2)

    async def _drive() -> None:
        import bp_sdk.dispatch as d

        real = asyncio.sleep

        async def _fast(_s: float) -> None:
            await real(0)

        d.asyncio.sleep = _fast  # type: ignore[assignment]
        # An in-flight peer await that the in-flight drain does NOT
        # cover (no active handler) — pre-fix it hung to the full
        # correlation timeout; now reject_all fails it fast.
        fut = disp.pending_acks.register("corr-1")
        stop = asyncio.Event()  # never set → recv death is the exit
        try:
            with pytest.raises(TransportPermanentlyFailed):
                await disp.run_until(stop)
        finally:
            d.asyncio.sleep = real  # type: ignore[assignment]

        # HIGH-2: reaper torn down (not orphaned) ...
        assert disp.pending_acks._reaper is None
        assert disp.pending_results._reaper is None
        # ... and the abandoned future was rejected, not left hanging.
        assert fut.done()
        assert isinstance(fut.exception(), TransportError)

    asyncio.run(_drive())


def test_agent_run_exits_nonzero_on_permanent_transport_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.errors import TransportPermanentlyFailed

    agent = Agent(info=AgentInfo(agent_id="agt_x", description="d"))

    async def _boom() -> None:
        raise TransportPermanentlyFailed("recv loop gave up")

    monkeypatch.setattr(agent, "run_async", _boom)

    with pytest.raises(SystemExit) as ei:
        agent.run()
    assert ei.value.code == 1


# ---------------------------------------------------------------------------
# HIGH-2 — clean stop still tears everything down (no raise)
# ---------------------------------------------------------------------------


def test_clean_stop_tears_down_without_raising() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.errors import TransportError

    disp = _dispatcher(_BlockRecv())

    async def _drive() -> None:
        fut = disp.pending_results.register("corr-2")
        stop = asyncio.Event()
        stop.set()  # external clean stop wins the race
        # No raise on the clean path.
        await disp.run_until(stop)

        assert disp.pending_acks._reaper is None
        assert disp.pending_results._reaper is None
        # Pending maps rejected on clean shutdown too.
        assert fut.done()
        assert isinstance(fut.exception(), TransportError)
        # recv loop was cancelled (it was parked in recv()).
        assert disp._loops[0].cancelled() or disp._loops[0].done()

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Source pins
# ---------------------------------------------------------------------------


def test_recv_loop_raises_terminal_on_cap_sourcepin() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._recv_loop)
    # The cap branch raises the terminal error (behaviour that it
    # RAISES rather than bare-returns is pinned by
    # test_recv_loop_raises_terminal_not_silent_return).
    assert "raise TransportPermanentlyFailed(" in src
    assert "recv_loop_giving_up" in src


def test_run_until_finally_teardown_sourcepin() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher.run_until)
    assert "finally:" in src
    assert "stop_reaper()" in src
    assert src.count("reject_all(") == 2
    assert "raise recv_death" in src


def test_run_translates_terminal_to_systemexit_sourcepin() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.agent import Agent

    src = inspect.getsource(Agent.run)
    assert "except TransportPermanentlyFailed" in src
    assert "SystemExit(1)" in src
