"""R8 MEDIUM: router hot-path lookups / per-frame import (3 batched).

Fresh-eyes R8 performance pass — three avoidable per-event costs
on genuinely hot paths:

#3 `frames_total` was re-imported via a function-local
   `from bp_router.observability.metrics import frames_total` on
   EVERY inbound (`dispatch_frame`) and outbound (`_send_loop`)
   frame — an import statement + try/except frame setup on the
   absolute hottest path. Hoisted to the module-level
   `from bp_router.observability import metrics` pattern tasks.py
   already uses.

#5 `_abort_router_side_llm_tasks` scanned every live socket ×
   every in-flight LLM task (O(M·K)) on every `cancel_task`
   (recursive cancel trees + the deadline sweep make cancel
   frequent). Now an O(1) lookup per cancelled task_id against a
   `state.llm_tasks_by_task_id` index populated + pruned in
   `dispatch._handle_llm_request`.

#6 `reject_all_for` scanned the ENTIRE global pending-acks map on
   EVERY disconnect, running a predicate per entry, even though
   the caller only ever needs its own socket's small
   `inflight_correlations` set. Replaced with `reject_ids` —
   O(this_socket_inflight) instead of O(all_pending).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

# ===========================================================================
# #3 per-frame metric import hoisted to module level
# ===========================================================================


def test_dispatch_frame_no_perframe_metric_import() -> None:
    from bp_router import dispatch

    src = inspect.getsource(dispatch.dispatch_frame)
    # The function-local import is gone from the hot path.
    assert "from bp_router.observability.metrics import frames_total" not in src
    # Increment goes through the module-level `metrics` handle.
    assert "metrics.frames_total.labels(" in src


def test_send_loop_no_perframe_metric_import() -> None:
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._send_loop)
    assert "from bp_router.observability.metrics import frames_total" not in src
    assert "metrics.frames_total.labels(" in src


def test_metrics_imported_at_module_level_in_both() -> None:
    from bp_router import dispatch, ws_hub

    assert "from bp_router.observability import metrics" in inspect.getsource(
        inspect.getmodule(dispatch)
    )
    assert "from bp_router.observability import metrics" in inspect.getsource(
        inspect.getmodule(ws_hub)
    )


def test_frames_total_still_increments_correctly() -> None:
    """Behavioural: the hoist must not break the actual counter.
    Increment via the module handle and read the value back."""
    pytest.importorskip("prometheus_client")
    from bp_router.observability import metrics

    before = _counter_value(metrics.frames_total, direction="recv", type="probe_t")
    metrics.frames_total.labels(direction="recv", type="probe_t").inc()
    after = _counter_value(metrics.frames_total, direction="recv", type="probe_t")
    assert after == before + 1


def _counter_value(counter, **labels):  # type: ignore[no-untyped-def]
    try:
        return counter.labels(**labels)._value.get()  # type: ignore[attr-defined]
    except Exception:
        return 0.0


# ===========================================================================
# #5 O(1) llm-task-by-task_id index
# ===========================================================================


def test_dispatch_populates_and_prunes_llm_task_index() -> None:
    """Source pin: `_handle_llm_request` adds the Task to
    `state.llm_tasks_by_task_id[task_id]` at creation and the
    done-callback discards it (dropping the key when the set
    empties) so the index stays self-bounded."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_llm_request)
    # Population at stamp time.
    assert "llm_tasks_by_task_id" in src
    assert "setdefault(tid, set()).add(task)" in src
    # Pruning in the done-callback: discard by identity + drop the
    # empty key.
    assert "bucket.discard(_t)" in src
    assert "idx.pop(tid, None)" in src


def test_app_initialises_llm_task_index() -> None:
    """`build_app`/lifespan must create `state.llm_tasks_by_task_id`
    as a dict so the index is present from boot."""
    from bp_router import app as app_mod

    src = inspect.getsource(app_mod)
    assert "state.llm_tasks_by_task_id = {}" in src


def test_abort_helper_uses_index_not_socket_scan() -> None:
    """Source pin: the helper must consult
    `state.llm_tasks_by_task_id`, NOT scan
    `socket_registry._live` (the pre-R8 O(M·K) path)."""
    from bp_router import tasks

    src = inspect.getsource(tasks._abort_router_side_llm_tasks)
    assert "llm_tasks_by_task_id" in src
    # The pre-R8 socket scan is gone.
    assert "socket_registry" not in src
    assert "_live" not in src
    assert ".llm_tasks.items()" not in src


# ===========================================================================
# #6 reject_ids: targeted per-socket rejection
# ===========================================================================


def test_reject_ids_only_touches_given_ids() -> None:
    from bp_router.correlation import PendingAcks

    async def _run() -> None:
        pa = PendingAcks()
        futs = {cid: pa.register(cid) for cid in ("a", "b", "c", "d")}

        # Reject only a and c.
        n = pa.reject_ids(["a", "c"])
        assert n == 2
        assert futs["a"].done() and isinstance(
            futs["a"].exception(), ConnectionError
        )
        assert futs["c"].done()
        # b and d untouched, still pending and still in the map.
        assert not futs["b"].done()
        assert not futs["d"].done()
        assert "b" in pa._pending and "d" in pa._pending
        assert "a" not in pa._pending and "c" not in pa._pending

        for f in futs.values():
            if not f.done():
                f.cancel()

    asyncio.run(_run())


def test_reject_ids_skips_unknown_ids() -> None:
    """Already-acked / reaped ids are skipped silently (reject
    returns False for a missing key)."""
    from bp_router.correlation import PendingAcks

    pa = PendingAcks()
    assert pa.reject_ids(["nonexistent", "also-gone"]) == 0


def test_reject_all_for_removed() -> None:
    """The O(total_pending) `reject_all_for` is gone — replaced by
    the targeted `reject_ids`. Pin so it isn't reintroduced."""
    from bp_router.correlation import PendingAcks

    assert not hasattr(PendingAcks, "reject_all_for")
    assert hasattr(PendingAcks, "reject_ids")


def test_disconnect_uses_reject_ids_with_socket_set() -> None:
    """Source pin: the disconnect path passes the socket's own
    `inflight_correlations` set to `reject_ids` (not a global-scan
    predicate)."""
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub)
    assert "reject_ids(" in src
    assert "reject_all_for(" not in src
    # The socket's own set is what's passed.
    assert "entry.inflight_correlations" in src
    # The reject_ids call is fed the socket's own inflight set.
    reject_call = src.index("reject_ids(")
    nearby = src[reject_call:reject_call + 120]
    assert "inflight_correlations" in nearby
