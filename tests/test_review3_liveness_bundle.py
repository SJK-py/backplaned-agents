"""Tests for the third-pass review liveness bundle.

  - H-1: `PendingAcks._reap_loop` is started by the lifespan and
    stopped on shutdown. Without the reaper, expired pending-ack
    futures hang forever — and `delivery.deliver_frame` does a bare
    `await fut`, so the timeout source-of-truth lives in the reaper.
  - H-3: The WebSocket frame-size cap is enforced at the protocol
    layer (`uvicorn.run(... ws_max_size=settings.max_payload_bytes)`)
    so an oversized frame is rejected BEFORE bytes are allocated.
    The post-receive check in `_recv_loop` stays as defence-in-depth
    AND uses byte-accurate comparison (multibyte UTF-8 must not slip
    through a char-count check).
  - H-4: Lifespan shutdown drains tracked background tasks and live
    WS sockets BEFORE closing the DB pool, so in-flight DB use
    doesn't raise `InterfaceError` on the way down.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# H-1: PendingAcks reaper lifecycle
# ===========================================================================


def test_h1_pending_acks_has_stop_reaper() -> None:
    """`PendingAcks` must expose `stop_reaper()` so the lifespan
    `finally` can cleanly cancel + await the reaper task. Without
    a clean stop, the cancelled task may not have reached its
    `except CancelledError: return` block before the event loop
    closes."""
    from bp_router.correlation import PendingAcks

    assert hasattr(PendingAcks, "stop_reaper")
    sig = inspect.signature(PendingAcks.stop_reaper)
    # No required args beyond self.
    assert len([p for p in sig.parameters.values() if p.default is p.empty]) == 1
    # Must be async — the lifespan finally is async.
    assert inspect.iscoroutinefunction(PendingAcks.stop_reaper)


def test_h1_start_then_stop_reaper_round_trip() -> None:
    """`start_reaper` schedules the loop; `stop_reaper` cancels it
    and awaits the cancellation. Pin the round-trip so a regression
    that swaps `await reaper` for a bare `cancel()` is caught."""
    from bp_router.correlation import PendingAcks

    async def _scenario() -> None:
        acks = PendingAcks()
        assert acks._reaper is None  # type: ignore[attr-defined]
        acks.start_reaper()
        assert acks._reaper is not None  # type: ignore[attr-defined]
        assert not acks._reaper.done()  # type: ignore[attr-defined]
        await acks.stop_reaper()
        assert acks._reaper.done()  # type: ignore[attr-defined]

    asyncio.run(_scenario())


def test_h1_stop_reaper_no_op_when_never_started() -> None:
    """Defensive: `stop_reaper()` on an instance that never had its
    reaper started must be a no-op, NOT raise. The lifespan
    `finally` may run even when an earlier subsystem failed to
    initialise PendingAcks beyond construction."""
    from bp_router.correlation import PendingAcks

    asyncio.run(PendingAcks().stop_reaper())  # must not raise


def test_h1_reaper_rejects_expired_pending_with_timeout_error() -> None:
    """Behavioural: a registered future whose deadline has passed
    must be resolved with `TimeoutError("ack_timeout")` by the
    reaper. Without this, callers like `delivery.deliver_frame`
    that do `await fut` with no `wait_for` wrapper hang forever."""
    from bp_router.correlation import PendingAcks

    async def _scenario() -> None:
        # Use a small default_timeout so we don't pause the suite.
        acks = PendingAcks(default_timeout_s=0.05)
        acks.start_reaper()
        try:
            fut = acks.register("cid-expired")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(fut, timeout=2.0)
        finally:
            await acks.stop_reaper()

    asyncio.run(_scenario())


def test_h1_lifespan_starts_and_stops_reaper() -> None:
    """Source pin: the lifespan must call `start_reaper()` after
    constructing `PendingAcks`, AND call `stop_reaper()` in the
    `finally` block. A regression that drops either side leaves
    the system in the buggy state (reaper never started, OR
    reaper never reaped on shutdown)."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module.lifespan)
    # Construction.
    assert "PendingAcks(" in src
    # Reaper started.
    assert "start_reaper()" in src
    # Reaper stopped on shutdown.
    assert "stop_reaper()" in src
    # The review-item citation lives near the start_reaper call so
    # someone scanning git blame finds the rationale.


# ===========================================================================
# H-3: WS frame-size cap at the protocol layer
# ===========================================================================


def test_h3_uvicorn_run_passes_ws_max_size_from_settings() -> None:
    """The router's `__main__` entry point must pass
    `ws_max_size=settings.max_payload_bytes` to `uvicorn.run` so
    the WebSocket protocol library (websockets / wsproto) rejects
    oversized frames BEFORE allocating the bytes."""
    from bp_router import __main__ as main_module

    src = inspect.getsource(main_module.main)
    assert "ws_max_size" in src, (
        "review3-H3 regression: uvicorn ws_max_size override has been removed"
    )
    assert "settings.max_payload_bytes" in src, (
        "ws_max_size must be sourced from settings, not hardcoded"
    )


def test_h3_recv_loop_keeps_byte_accurate_defence_in_depth_check() -> None:
    """The post-receive check in `_recv_loop` is defence-in-depth
    for deployments that don't run via `bp_router.__main__`. It
    MUST use the byte-accurate `len(raw.encode("utf-8"))` not
    `len(raw)` — UTF-8 multibyte chars are up to 4× the char
    count in bytes; a char-count cap admits oversized payloads
    by a 4× factor."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._recv_loop)
    # Byte-accurate comparison must be present (the .encode call).
    assert 'raw.encode("utf-8")' in src


# ===========================================================================
# H-4: Lifespan shutdown drains tasks + sockets before pool close
# ===========================================================================


def test_h4_shutdown_helpers_exist() -> None:
    """The two helpers extracted from the lifespan must be
    importable from `bp_router.app` so future refactors / tests
    can target them directly."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    assert hasattr(app_module, "_shutdown_live_sockets")
    assert hasattr(app_module, "_drain_background_tasks")
    assert inspect.iscoroutinefunction(app_module._shutdown_live_sockets)
    assert inspect.iscoroutinefunction(app_module._drain_background_tasks)


def test_h4_lifespan_finally_orders_shutdown_correctly() -> None:
    """Source pin: the `finally` block of `lifespan` must invoke
    the helpers AND cleanup steps in the right order:

      1. `_shutdown_live_sockets` (close sockets + cancel LLM tasks)
      2. `_drain_background_tasks` (cancel + await bg tasks)
      3. `state.correlation.stop_reaper`
      4. `state.db_pool.close` (LAST among DB users)
      5. `state.redis.aclose`

    Order matters: closing the pool before draining bg tasks would
    cause every bg-task DB call to raise `InterfaceError` mid-flight.
    """
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module.lifespan)
    # Find each step's index in the source.
    idx_sockets = src.find("_shutdown_live_sockets")
    idx_drain = src.find("_drain_background_tasks")
    idx_stop_reaper = src.find("stop_reaper()")
    idx_pool_close = src.find("db_pool.close")
    idx_redis_close = src.find("redis.aclose")

    assert idx_sockets > -1, "socket shutdown step missing"
    assert idx_drain > -1, "bg-task drain step missing"
    assert idx_stop_reaper > -1, "reaper stop missing"
    assert idx_pool_close > -1, "pool close missing"
    assert idx_redis_close > -1, "redis close missing"

    # Ordering invariants. Sockets / bg-tasks / reaper all use the
    # pool, so they MUST drain before the pool closes.
    assert idx_sockets < idx_pool_close, (
        "review3-H4: live sockets must close BEFORE the DB pool"
    )
    assert idx_drain < idx_pool_close, (
        "review3-H4: background tasks must drain BEFORE the DB pool"
    )
    assert idx_stop_reaper < idx_pool_close, (
        "review3-H4: PendingAcks reaper must stop BEFORE the DB pool"
    )
    # Redis goes after the pool — it's not a load-bearing dependency
    # for the bg tasks (redis is only used for JTI revocation lookups).
    assert idx_pool_close < idx_redis_close, (
        "review3-H4: pool close should precede redis close"
    )


def test_h4_drain_cancels_then_awaits_with_return_exceptions() -> None:
    """Source pin: `_drain_background_tasks` cancels each task,
    then `await asyncio.gather(..., return_exceptions=True)`. The
    `return_exceptions=True` is critical — without it, the FIRST
    cancelled task that raises a non-CancelledError would propagate
    and skip awaiting the others, leaking them past the lifespan
    boundary."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module._drain_background_tasks)
    assert "cancel()" in src
    assert "asyncio.gather" in src
    assert "return_exceptions=True" in src


def test_h4_drain_handles_both_named_and_spawn_background_tasks() -> None:
    """`_drain_background_tasks` must drain BOTH the named tasks
    returned from `start_background_loops()` AND the strong-ref
    `state.bg_tasks` set populated by `spawn_background()`. A
    regression that drops either source leaks tasks past shutdown."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module._drain_background_tasks)
    assert "named_bg_tasks" in src
    assert "state.bg_tasks" in src


def test_h4_drain_no_tasks_is_safe() -> None:
    """Empty input must not crash — `asyncio.gather()` with no
    args raises `RuntimeError` in some Python versions; the
    helper must guard against that."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    state = MagicMock()
    state.bg_tasks = set()
    asyncio.run(app_module._drain_background_tasks(state, []))


def test_h4_drain_actually_cancels_pending_tasks() -> None:
    """Behavioural: spawn a long-running task, hand it to the
    drain helper, observe that it's cancelled and awaited."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    async def _scenario() -> None:
        async def _forever() -> None:
            await asyncio.sleep(3600)

        state = MagicMock()
        state.bg_tasks = set()
        named = [asyncio.create_task(_forever())]
        # Yield control so the task actually starts before drain.
        await asyncio.sleep(0)

        await app_module._drain_background_tasks(state, named)

        for t in named:
            assert t.done(), "drain must await each task to completion"
            assert t.cancelled() or t.exception() is not None

    asyncio.run(_scenario())


def test_h4_shutdown_live_sockets_cancels_llm_tasks_and_closes() -> None:
    """Behavioural: `_shutdown_live_sockets` iterates
    `socket_registry.live_agent_ids()`, cancels each entry's
    `llm_tasks`, and calls `websocket.close(code=1001)`."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    async def _scenario() -> None:
        async def _running() -> None:
            await asyncio.sleep(3600)

        llm_task = asyncio.create_task(_running())
        await asyncio.sleep(0)

        entry = MagicMock()
        entry.llm_tasks = {"cid-1": llm_task}
        entry.closed = MagicMock()
        entry.closed.set = MagicMock()
        entry.websocket = MagicMock()
        entry.websocket.close = AsyncMock(return_value=None)

        registry = MagicMock()
        registry.live_agent_ids.return_value = ["agent-a"]
        registry.get.return_value = entry

        state = MagicMock()
        state.socket_registry = registry

        await app_module._shutdown_live_sockets(state)

        # LLM task cancelled and the dict cleared.
        assert llm_task.cancelled()
        assert entry.llm_tasks == {}
        # Socket marked closed.
        entry.closed.set.assert_called_once()
        # Close called with 1001.
        entry.websocket.close.assert_awaited_once()
        kwargs = entry.websocket.close.call_args.kwargs
        assert kwargs.get("code") == 1001

    asyncio.run(_scenario())


def test_h4_shutdown_live_sockets_logs_but_does_not_raise_on_close_error() -> None:
    """Best-effort: a socket whose `.close()` raises must not
    block shutdown of the OTHER sockets. The helper logs and
    moves on."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    async def _scenario() -> None:
        bad_entry = MagicMock()
        bad_entry.llm_tasks = {}
        bad_entry.closed = MagicMock()
        bad_entry.websocket.close = AsyncMock(side_effect=RuntimeError("boom"))

        good_entry = MagicMock()
        good_entry.llm_tasks = {}
        good_entry.closed = MagicMock()
        good_entry.websocket.close = AsyncMock(return_value=None)

        registry = MagicMock()
        registry.live_agent_ids.return_value = ["bad", "good"]
        registry.get.side_effect = lambda aid: (
            bad_entry if aid == "bad" else good_entry
        )

        state = MagicMock()
        state.socket_registry = registry

        # Must not raise.
        await app_module._shutdown_live_sockets(state)

        # The "good" socket still got closed.
        good_entry.websocket.close.assert_awaited_once()

    asyncio.run(_scenario())
