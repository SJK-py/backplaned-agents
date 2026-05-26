"""Tests for the resource-leak review fixes (SDK-H1, SDK-H3, SDK-H4,
WS-H4 / SDK-M5).

All four findings touch the SDK's runtime cleanup contracts.
Tests use stub dispatchers / fake transports — no live router or
filesystem. The full-stack scenarios are covered by integration
tests in `test_smoke_e2e.py` (skipped when no DB is configured).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from bp_sdk.correlation import PendingMap

# ===========================================================================
# SDK-H3: PendingMap._buffered is bounded
# ===========================================================================


def test_pending_map_buffered_is_bounded_at_max_size() -> None:
    """Without a cap, a runaway recv loop can let `_buffered` grow
    until OOM. The cap evicts the oldest insertion when overflowing,
    leaving the newer entries (which are more likely to be claimed
    soon by a `register()` call) intact."""
    pmap = PendingMap(default_timeout_s=30.0)
    # Fill to exactly the cap.
    cap = pmap.BUFFER_MAX_SIZE

    async def _drive() -> None:
        for i in range(cap):
            pmap.resolve(f"cid_{i}", f"value_{i}")
        assert len(pmap._buffered) == cap
        # One more push triggers eviction. The OLDEST (cid_0) goes.
        pmap.resolve("cid_overflow", "later")
        assert len(pmap._buffered) == cap
        assert "cid_0" not in pmap._buffered
        assert "cid_overflow" in pmap._buffered
        # Any subsequent insertion keeps evicting the head.
        pmap.resolve("cid_overflow_2", "even later")
        assert "cid_1" not in pmap._buffered  # next-oldest now gone
        assert "cid_overflow" in pmap._buffered  # still there
        assert "cid_overflow_2" in pmap._buffered

    asyncio.run(_drive())


def test_pending_map_resolve_uses_running_loop() -> None:
    """`asyncio.get_event_loop()` is deprecated in 3.12+ and raises
    DeprecationWarning under stricter configs. Verify the resolve
    path uses `get_running_loop` (which doesn't deprecate) — the
    behavioral signal: resolve doesn't error when called from a
    coroutine context."""
    import warnings

    pmap = PendingMap(default_timeout_s=30.0)

    async def _drive() -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            # This used to emit DeprecationWarning on get_event_loop.
            pmap.resolve("cid_a", "v")
            pmap.reject("cid_b", RuntimeError("x"))

    asyncio.run(_drive())


def test_pending_map_buffered_re_resolve_does_not_evict_others() -> None:
    """Edge case: resolving a correlation_id that's ALREADY buffered
    (e.g. a duplicate frame from the recv loop) should overwrite the
    existing buffer entry, NOT evict an unrelated one. Without this,
    a duplicate at-cap bug would silently drop an unrelated pending
    correlation."""
    pmap = PendingMap(default_timeout_s=30.0)

    async def _drive() -> None:
        for i in range(pmap.BUFFER_MAX_SIZE):
            pmap.resolve(f"cid_{i}", f"value_{i}")
        # Re-resolve cid_0 (already in buffer) at cap. Must NOT evict.
        pmap.resolve("cid_0", "updated_value")
        assert len(pmap._buffered) == pmap.BUFFER_MAX_SIZE
        # All originals still present.
        for i in range(pmap.BUFFER_MAX_SIZE):
            assert f"cid_{i}" in pmap._buffered

    asyncio.run(_drive())


# ===========================================================================
# SDK-H4: FileStash.cleanup is invoked from _run_handler.finally
# ===========================================================================


def test_run_handler_invokes_files_cleanup_on_normal_exit() -> None:
    """Source-level: `_run_handler`'s `finally` calls
    `await ctx.files.cleanup()` (review item SDK-H4). Without this,
    every task's inbox tree (`state_dir/inbox/<task_id>`) leaks
    until process exit."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._run_handler)
    # The finally block must contain a `files.cleanup()` await.
    finally_idx = src.index("finally:")
    after_finally = src[finally_idx:]
    assert "files.cleanup()" in after_finally
    assert "await files.cleanup()" in after_finally
    # And the finally block guards against `ctx.files is None` for
    # the spawn-style frame path that doesn't allocate a manager.
    assert "if files is not None" in after_finally


# ===========================================================================
# WS-H4 / SDK-M5: SpawnStream cleans up _progress_subscribers
# ===========================================================================


def _make_spawn_stream(task_id: str = "tsk_child"):
    """Build a SpawnStream + a stub dispatcher so cleanup behaviour
    can be exercised without spinning up a real recv loop."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import SpawnStream

    dispatcher = MagicMock()
    dispatcher._progress_subscribers = {task_id: asyncio.Queue()}

    # Wire the typed accessor on the mock so `_unsubscribe` actually
    # pops the dict the test inspects.
    def _unsubscribe(tid: str) -> None:
        dispatcher._progress_subscribers.pop(tid, None)

    dispatcher.unsubscribe_progress = _unsubscribe

    async def _make() -> tuple[SpawnStream, dict, Any]:
        loop = asyncio.get_running_loop()
        result_fut = loop.create_future()
        queue = dispatcher._progress_subscribers[task_id]
        stream = SpawnStream(
            task_id=task_id,
            queue=queue,
            result_fut=result_fut,
            dispatcher=dispatcher,
        )
        return stream, dispatcher._progress_subscribers, result_fut

    return _make()


def test_spawn_stream_aclose_unsubscribes() -> None:
    """`aclose()` must pop the dispatcher's
    `_progress_subscribers[task_id]` entry and cancel the result
    future. Idempotent — safe to call twice."""
    pytest.importorskip("fastapi")

    async def _drive() -> None:
        stream, subscribers, result_fut = await _make_spawn_stream()
        assert "tsk_child" in subscribers
        await stream.aclose()
        assert "tsk_child" not in subscribers
        assert result_fut.cancelled()
        # Idempotent — second call is a no-op.
        await stream.aclose()

    asyncio.run(_drive())


def test_spawn_stream_async_with_cleans_up_on_break() -> None:
    """The primary use case for `__aenter__/__aexit__`: a caller
    iterates progress, breaks early, and trusts the context manager
    to release the subscription. Without this, the SDK leaks the
    queue forever (review item WS-H4)."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame

    async def _drive() -> None:
        stream, subscribers, _ = await _make_spawn_stream()
        # Push a couple of progress frames.
        await stream._queue.put(ProgressFrame(
            agent_id="agt_x", trace_id="tr", span_id="sp",
            task_id="tsk_child", event="status",
        ))
        await stream._queue.put(ProgressFrame(
            agent_id="agt_x", trace_id="tr", span_id="sp",
            task_id="tsk_child", event="status",
        ))

        async with stream as s:
            async for _frame in s:
                break  # bail after first chunk
        # On context exit, subscription is released.
        assert "tsk_child" not in subscribers

    asyncio.run(_drive())


def test_spawn_stream_natural_completion_unsubscribes() -> None:
    """When the result lands via `__anext__`, iteration ends and
    `_progress_subscribers` is popped — the previous implementation
    already popped on result, but the test pins the contract."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ResultFrame

    async def _drive() -> None:
        stream, subscribers, _ = await _make_spawn_stream()
        result = ResultFrame(
            agent_id="agt_x", trace_id="tr", span_id="sp",
            task_id="tsk_child", parent_task_id=None,
            status="succeeded", status_code=200,
        )
        await stream._queue.put(result)
        out = []
        async for frame in stream:
            out.append(frame)
        # No frames before the result — but the unsubscribe DID fire.
        assert "tsk_child" not in subscribers

    asyncio.run(_drive())


def test_spawn_stream_result_timeout_unsubscribes() -> None:
    """A `result(timeout_s=N)` that times out must release the
    subscription too — otherwise the dispatcher keeps queueing
    progress into a dead consumer until the agent shuts down."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerCallError

    async def _drive() -> None:
        stream, subscribers, _ = await _make_spawn_stream()
        # No result will arrive — short timeout.
        with pytest.raises(PeerCallError):
            await stream.result(timeout_s=0.01)
        assert "tsk_child" not in subscribers

    asyncio.run(_drive())


# ===========================================================================
# SDK-H1: per-task pending-future tracking + drain on handler exit
# ===========================================================================


def test_register_for_task_tracks_correlation_id() -> None:
    """Behavioral check: `register_for_task` records (pmap, cid)
    under the task's set in `_task_correlations`. Without that, the
    drain on handler exit would have nothing to reject."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    # Stub Dispatcher just enough to call register_for_task.
    disp = Dispatcher.__new__(Dispatcher)
    disp.pending_acks = PendingMap(default_timeout_s=30.0)
    disp.pending_results = PendingMap(default_timeout_s=30.0)
    disp._task_correlations = {}

    async def _drive() -> None:
        fut = disp.register_for_task(
            disp.pending_results, "cid_1", "tsk_handler",
        )
        assert fut is not None
        assert disp._task_correlations["tsk_handler"] == {
            (disp.pending_results, "cid_1"),
        }

    asyncio.run(_drive())


def test_register_for_task_skips_tracking_for_spawn_placeholder() -> None:
    """task_id == '<spawn>' (handler-bootstrap path before the
    router has assigned a task_id) bypasses the tracker — those
    futures fall back to the timeout-reaper path."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    disp = Dispatcher.__new__(Dispatcher)
    disp.pending_acks = PendingMap(default_timeout_s=30.0)
    disp.pending_results = PendingMap(default_timeout_s=30.0)
    disp._task_correlations = {}

    async def _drive() -> None:
        disp.register_for_task(
            disp.pending_results, "cid_x", "<spawn>",
        )
        disp.register_for_task(
            disp.pending_results, "cid_y", None,
        )
        # Neither call enrolled in the tracker.
        assert disp._task_correlations == {}

    asyncio.run(_drive())


def test_drain_task_correlations_rejects_pending_futures() -> None:
    """Core SDK-H1 contract: when a handler exits with futures still
    pending, `_drain_task_correlations` rejects them with
    `HandlerExited` so callers fail fast instead of waiting out
    `correlation_timeout`."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher, HandlerExited

    disp = Dispatcher.__new__(Dispatcher)
    disp.pending_acks = PendingMap(default_timeout_s=30.0)
    disp.pending_results = PendingMap(default_timeout_s=30.0)
    disp._task_correlations = {}

    async def _drive() -> None:
        ack_fut = disp.register_for_task(
            disp.pending_acks, "cid_ack", "tsk_h",
        )
        result_fut = disp.register_for_task(
            disp.pending_results, "cid_result", "tsk_h",
        )
        # Drain.
        rejected = disp._drain_task_correlations(
            "tsk_h", HandlerExited("tsk_h"),
        )
        assert rejected == 2
        # Both futures are now rejected.
        assert ack_fut.done() and ack_fut.exception() is not None
        assert isinstance(ack_fut.exception(), HandlerExited)
        assert result_fut.done() and isinstance(
            result_fut.exception(), HandlerExited
        )
        # The task entry is popped.
        assert "tsk_h" not in disp._task_correlations

    asyncio.run(_drive())


def test_drain_task_correlations_skips_already_resolved() -> None:
    """If a future was already resolved (e.g., the spawn ack arrived
    before the handler raised), drain mustn't push the rejection
    into `_buffered` and create a stale entry. Verify resolved
    futures are skipped."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher, HandlerExited

    disp = Dispatcher.__new__(Dispatcher)
    disp.pending_acks = PendingMap(default_timeout_s=30.0)
    disp.pending_results = PendingMap(default_timeout_s=30.0)
    disp._task_correlations = {}

    async def _drive() -> None:
        fut1 = disp.register_for_task(
            disp.pending_acks, "cid_resolved", "tsk_h",
        )
        fut2 = disp.register_for_task(
            disp.pending_acks, "cid_pending", "tsk_h",
        )
        # Resolve fut1 normally.
        disp.pending_acks.resolve("cid_resolved", "ok")
        # The done-callback is scheduled on the loop; let it run.
        await asyncio.sleep(0)
        # Now drain — only fut2 should be rejected.
        rejected = disp._drain_task_correlations(
            "tsk_h", HandlerExited("tsk_h"),
        )
        assert rejected == 1
        assert fut1.result() == "ok"  # untouched
        assert isinstance(fut2.exception(), HandlerExited)
        # Critically: `_buffered` must NOT contain a stale entry for
        # `cid_resolved` (the bug guard — drain checks `_pending`
        # membership before rejecting).
        assert "cid_resolved" not in disp.pending_acks._buffered

    asyncio.run(_drive())


def test_done_callback_untracks_on_natural_resolve() -> None:
    """When a future resolves normally, the done-callback removes
    the (pmap, cid) entry from the task tracker so a later drain
    has nothing extra to reject."""
    pytest.importorskip("fastapi")
    from bp_sdk.dispatch import Dispatcher

    disp = Dispatcher.__new__(Dispatcher)
    disp.pending_acks = PendingMap(default_timeout_s=30.0)
    disp.pending_results = PendingMap(default_timeout_s=30.0)
    disp._task_correlations = {}

    async def _drive() -> None:
        disp.register_for_task(disp.pending_acks, "cid_one", "tsk_h")
        disp.register_for_task(disp.pending_acks, "cid_two", "tsk_h")
        disp.pending_acks.resolve("cid_one", "value")
        await asyncio.sleep(0)  # let done-callback fire
        # cid_one untracked; cid_two still tracked.
        assert disp._task_correlations["tsk_h"] == {
            (disp.pending_acks, "cid_two"),
        }
        # When the LAST future resolves, the task entry is popped
        # entirely (avoids leaking empty sets).
        disp.pending_acks.resolve("cid_two", "value2")
        await asyncio.sleep(0)
        assert "tsk_h" not in disp._task_correlations

    asyncio.run(_drive())


def test_run_handler_drains_pending_futures_in_finally() -> None:
    """Source-level: `_run_handler`'s `finally` calls
    `_drain_task_correlations` so callers awaiting peer-call
    futures fail fast on handler exception."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._run_handler)
    finally_idx = src.index("finally:")
    after_finally = src[finally_idx:]
    assert "_drain_task_correlations" in after_finally
    assert "HandlerExited" in after_finally


def test_peers_spawn_uses_register_for_task() -> None:
    """Source-level: `peers.spawn` registers ack + result futures via
    `dispatcher.register_for_task` (not raw `pending_*.register`),
    so handler-exit drain catches them."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_sdk import peers

    src = inspect.getsource(peers.PeerClient.spawn)
    # Both registrations go through register_for_task.
    assert src.count("register_for_task") >= 2
    # And the raw register call is gone from the spawn path.
    assert "pending_acks.register(" not in src
    assert "pending_results.register(" not in src
