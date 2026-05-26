"""R-MEDIUM #4: `fail_task` cascade-cancels the descendant subtree.

A task that FAILS or TIMES OUT used to leak its children: nothing
ever consumed their Results, yet they kept running (burning compute /
provider tokens) until their own deadlines fired — and a child
spawned with no deadline ran forever. `fail_task` only ever
propagated UP to the parent.

The fix mirrors `cancel_task`'s proven recursive shape: walk
`list_descendants`, transition each child to CANCELLED in its own
per-tid transaction (an already-terminal child must not roll back
its siblings), fan a synthetic Result(CANCELLED) to each child's
caller + a CancelFrame to its executor, and abort router-side LLM
streams for the cancelled set. It is pool-respecting (re-uses the
caller's `conn` so the deadline sweep / disconnect cleanup keep
their one-conn-per-batch discipline) and runs BEFORE the
`parent_task_id is None` early-return so a ROOT task that times out
still reaps its subtree.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Helpers
# ===========================================================================


def _make_fail_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    descendants: list[Any],
    parent_task_id: str | None,
    owner_for: dict[str, dict[str, Any]],
    parent_owner_agent: str | None = None,
) -> Any:
    """Fake `state` for `fail_task`.

    `parent_task_id` is the failing task's parent (None => root).
    `owner_for` maps descendant task_id -> owner row fields. The
    fake `conn.fetchrow` dispatches on the query shape.
    """
    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool

    scope = MagicMock()
    scope.list_descendants = AsyncMock(return_value=descendants)

    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    async def _fetchrow(query: str, *args: Any) -> Any:
        if "active_agent_id, caller_agent_id, parent_task_id" in query:
            entry = owner_for.get(args[0])
            if entry is None:
                return None
            return {
                "active_agent_id": entry["active_agent_id"],
                "caller_agent_id": entry["caller_agent_id"],
                "parent_task_id": entry["parent_task_id"],
            }
        if "SELECT parent_task_id" in query:
            # The failing task's parent lookup inside _do_db_work.
            return {"parent_task_id": parent_task_id}
        if "SELECT agent_id FROM tasks" in query:
            return (
                {"agent_id": parent_owner_agent}
                if parent_owner_agent is not None
                else None
            )
        if "SELECT user_id FROM tasks" in query:
            return {"user_id": "usr_alice"}
        return None

    conn.fetchrow = _fetchrow
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    from bp_router.db import queries as queries_module

    monkeypatch.setattr(
        queries_module.Scope, "user", MagicMock(return_value=scope)
    )
    return state, conn


def _patch_transition(
    monkeypatch: pytest.MonkeyPatch,
    *,
    illegal_for: set[str] | None = None,
    raise_for: dict[str, BaseException] | None = None,
) -> list[tuple[str, Any]]:
    """Patch `tasks.task_transition`. Records (task_id, state) calls.
    Raises IllegalTransition for any task_id in `illegal_for` (an
    already-terminal descendant losing the race); raises the mapped
    exception for any task_id in `raise_for` (a poison row — an
    UNEXPECTED asyncpg-style error, NOT Illegal/NotFound)."""
    from bp_protocol.types import TaskState
    from bp_router import tasks
    from bp_router.state import IllegalTransition

    illegal_for = illegal_for or set()
    raise_for = raise_for or {}
    calls: list[tuple[str, Any]] = []

    async def _transition(*args: Any, **kwargs: Any) -> Any:
        task_id = args[1]
        new_state = args[2]
        calls.append((task_id, new_state))
        if task_id in raise_for:
            raise raise_for[task_id]
        if task_id in illegal_for:
            raise IllegalTransition(
                task_id, TaskState.SUCCEEDED, new_state
            )
        return MagicMock()

    monkeypatch.setattr(tasks, "task_transition", _transition)
    return calls


def _patch_deliver(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    from bp_router import tasks

    delivered: list[tuple[str, Any]] = []

    async def _deliver(
        state: Any, agent_id: str, frame: Any, *, await_ack: bool
    ) -> None:
        delivered.append((agent_id, frame))

    monkeypatch.setattr(tasks, "deliver_frame", _deliver)
    return delivered


# ===========================================================================
# Cascade behaviour
# ===========================================================================


def test_fail_task_cascades_cancel_to_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing task's two descendants are each transitioned to
    CANCELLED, get a synthetic Result(CANCELLED) to their caller +
    a CancelFrame to their executor — AND the failing task's parent
    still gets its Result(FAILED) (upward propagation unchanged)."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState, TaskStatus
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    calls = _patch_transition(monkeypatch)

    descendants = [
        MagicMock(task_id="tsk_c1"),
        MagicMock(task_id="tsk_gc"),
    ]
    state, _conn = _make_fail_state(
        monkeypatch,
        descendants=descendants,
        parent_task_id="tsk_parent",
        parent_owner_agent="agt_parent",
        owner_for={
            "tsk_c1": {
                "active_agent_id": "agt_c1",
                "caller_agent_id": "agt_failing",
                "parent_task_id": "tsk_failing",
            },
            "tsk_gc": {
                "active_agent_id": "agt_gc",
                "caller_agent_id": "agt_c1",
                "parent_task_id": "tsk_c1",
            },
        },
    )

    asyncio.run(tasks.fail_task(
        state,
        "tsk_failing",
        user_id="usr_alice",
        status_code=504,
        reason="deadline_exceeded",
        error={"code": "deadline_exceeded"},
    ))

    # The failing task FAILED, both descendants CANCELLED.
    assert ("tsk_failing", TaskState.FAILED) in calls
    assert ("tsk_c1", TaskState.CANCELLED) in calls
    assert ("tsk_gc", TaskState.CANCELLED) in calls

    cancel_results = {
        (a, f.task_id, f.parent_task_id)
        for a, f in delivered
        if type(f).__name__ == "ResultFrame"
        and f.status == TaskStatus.CANCELLED
    }
    assert ("agt_failing", "tsk_c1", "tsk_failing") in cancel_results
    assert ("agt_c1", "tsk_gc", "tsk_c1") in cancel_results

    cancel_frames = {
        (a, f.task_id)
        for a, f in delivered
        if type(f).__name__ == "CancelFrame"
    }
    assert ("agt_c1", "tsk_c1") in cancel_frames
    assert ("agt_gc", "tsk_gc") in cancel_frames

    # Upward propagation preserved: parent agent got Result(FAILED).
    failed_to_parent = [
        (a, f) for a, f in delivered
        if a == "agt_parent"
        and type(f).__name__ == "ResultFrame"
        and f.status == TaskStatus.FAILED
    ]
    assert len(failed_to_parent) == 1
    assert failed_to_parent[0][1].task_id == "tsk_failing"


def test_fail_task_root_task_still_cascades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decisive placement pin: a ROOT task (parent_task_id is
    None) that times out STILL cancels its descendants. The cascade
    must run BEFORE the `parent_task_id is None` early-return — a
    regression that puts it after would silently leak every root
    task's subtree."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    calls = _patch_transition(monkeypatch)

    state, _conn = _make_fail_state(
        monkeypatch,
        descendants=[MagicMock(task_id="tsk_child")],
        parent_task_id=None,  # ROOT task
        owner_for={
            "tsk_child": {
                "active_agent_id": "agt_child",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
        },
    )

    asyncio.run(tasks.fail_task(
        state,
        "tsk_root",
        user_id="usr_alice",
        status_code=504,
        reason="deadline_exceeded",
    ))

    assert ("tsk_root", TaskState.FAILED) in calls
    assert ("tsk_child", TaskState.CANCELLED) in calls
    child_frames = {
        type(f).__name__ for a, f in delivered if "child" in repr(f.task_id)
    }
    assert "ResultFrame" in child_frames
    assert "CancelFrame" in child_frames


def test_fail_task_skips_already_terminal_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descendant that already reached a terminal state loses the
    CANCELLED transition race (IllegalTransition) — it must be
    skipped (no synthetic Result/Cancel for it, so no double
    terminal frame) while its siblings are still processed."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    _patch_transition(monkeypatch, illegal_for={"tsk_done"})

    state, _conn = _make_fail_state(
        monkeypatch,
        descendants=[
            MagicMock(task_id="tsk_done"),   # already terminal
            MagicMock(task_id="tsk_live"),
        ],
        parent_task_id=None,
        owner_for={
            "tsk_done": {
                "active_agent_id": "agt_done",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
            "tsk_live": {
                "active_agent_id": "agt_live",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
        },
    )

    asyncio.run(tasks.fail_task(
        state, "tsk_root", user_id="usr_alice",
        status_code=504, reason="deadline_exceeded",
    ))

    by_tid = {f.task_id for _a, f in delivered}
    # The already-terminal descendant got NO frames.
    assert "tsk_done" not in by_tid
    # The live one was cancelled normally.
    assert "tsk_live" in by_tid


def test_fail_task_no_cascade_when_transition_lost_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the failing task's OWN FAILED transition loses the race
    (already terminal — `_do_db_work` returns None), `fail_task`
    must return early and NOT cascade. We did not fail this task,
    so we have no business cancelling its subtree (whoever DID
    terminate it owns that decision)."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    _patch_transition(monkeypatch, illegal_for={"tsk_failing"})

    list_desc = AsyncMock(return_value=[MagicMock(task_id="tsk_child")])
    state, _conn = _make_fail_state(
        monkeypatch,
        descendants=[MagicMock(task_id="tsk_child")],
        parent_task_id="tsk_parent",
        owner_for={
            "tsk_child": {
                "active_agent_id": "agt_child",
                "caller_agent_id": "agt_failing",
                "parent_task_id": "tsk_failing",
            },
        },
    )
    from bp_router.db import queries as queries_module
    queries_module.Scope.user.return_value.list_descendants = list_desc

    asyncio.run(tasks.fail_task(
        state, "tsk_failing", user_id="usr_alice",
        status_code=504, reason="deadline_exceeded",
    ))

    # Lost the race => no subtree walk, no frames at all.
    list_desc.assert_not_awaited()
    assert delivered == []


def test_fail_task_with_conn_does_not_acquire_pool_for_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool discipline: when the deadline sweep / disconnect cleanup
    pass their held `conn`, the cascade MUST re-use it — never
    `pool.acquire()` per descendant (that's the pool-exhaustion bug
    those callers hold one conn to avoid)."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    calls = _patch_transition(monkeypatch)

    state, conn = _make_fail_state(
        monkeypatch,
        descendants=[MagicMock(task_id="tsk_child")],
        parent_task_id=None,
        owner_for={
            "tsk_child": {
                "active_agent_id": "agt_child",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
        },
    )

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(
            "fail_task acquired a pool connection despite being "
            "handed one — breaks the sweep/disconnect pool discipline"
        )

    state.db_pool.acquire = _boom

    asyncio.run(tasks.fail_task(
        state, "tsk_root", user_id="usr_alice",
        status_code=504, reason="deadline_exceeded",
        conn=conn,
    ))

    # Cascade still happened, entirely through the passed conn.
    assert ("tsk_root", TaskState.FAILED) in calls
    assert ("tsk_child", TaskState.CANCELLED) in calls
    assert any(type(f).__name__ == "CancelFrame" for _a, f in delivered)


def test_fail_task_cascade_isolates_poison_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit fix: an UNEXPECTED error (not Illegal/NotFound) on ONE
    descendant must NOT abandon the rest of the subtree. A poison
    middle row logs + continues; the descendants before AND after it
    are still cancelled, and fail_task does not propagate the error
    (it would otherwise bubble through the sweep batch and re-create
    the orphan-leak the cascade exists to prevent)."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    calls = _patch_transition(
        monkeypatch,
        raise_for={"tsk_poison": RuntimeError("asyncpg blew up")},
    )

    state, _conn = _make_fail_state(
        monkeypatch,
        descendants=[
            MagicMock(task_id="tsk_good1"),
            MagicMock(task_id="tsk_poison"),   # middle row errors
            MagicMock(task_id="tsk_good2"),    # MUST still be reached
        ],
        parent_task_id=None,
        owner_for={
            "tsk_good1": {
                "active_agent_id": "agt_g1",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
            "tsk_good2": {
                "active_agent_id": "agt_g2",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
        },
    )

    # Must NOT raise — the poison row is isolated.
    asyncio.run(tasks.fail_task(
        state, "tsk_root", user_id="usr_alice",
        status_code=504, reason="deadline_exceeded",
    ))

    transitioned = {tid for tid, st in calls if st == TaskState.CANCELLED}
    # All three were attempted (proves the loop continued past the
    # poison row to tsk_good2).
    assert transitioned == {"tsk_good1", "tsk_poison", "tsk_good2"}

    framed = {f.task_id for _a, f in delivered}
    # The two healthy descendants were cancelled + fanned out...
    assert "tsk_good1" in framed
    assert "tsk_good2" in framed
    # ...the poison one got no frames (its txn rolled back).
    assert "tsk_poison" not in framed


# ===========================================================================
# Source pins
# ===========================================================================


def test_fail_task_cascade_runs_before_parent_none_return() -> None:
    """The subtree cancel MUST be lexically before the
    `if parent_row_data["parent_task_id"] is None: return` — that is
    what makes a root-task timeout still reap its children."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.fail_task)
    cascade_idx = src.index("list_descendants")
    parent_none_return = src.index(
        'if parent_row_data["parent_task_id"] is None:'
    )
    assert cascade_idx < parent_none_return, (
        "descendant cascade must run before the root-task "
        "early-return, else root timeouts leak their subtree"
    )
    # Uses the shared reason + the proven cancel fan-out shape.
    assert 'reason="parent_failed"' in src
    assert "_abort_router_side_llm_tasks" in src


def test_fail_task_subtree_reuses_passed_conn() -> None:
    """Source pin for the pool discipline: the subtree helper is
    invoked with the passed `conn` when present, only falling back
    to `pool.acquire()` when none was handed in."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.fail_task)
    assert "_cancel_subtree(conn)" in src
    assert "if conn is not None:" in src


def test_fail_task_cascade_has_per_descendant_isolation() -> None:
    """Source pin: the per-`d` body must be wrapped in a broad
    `try/except Exception … continue` (mirroring _sweep_once /
    fail_inflight_for_agent) so one poison descendant can't abandon
    the rest of the subtree."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.fail_task)
    sub = src[src.index("async def _cancel_subtree"):src.index("return plan")]
    # The broad guard + structured event + continue, inside the
    # `for d in descendants` loop.
    assert "for d in descendants:" in sub
    assert "except Exception:" in sub
    assert "fail_task_cascade_row_failed" in sub
    loop_at = sub.index("for d in descendants:")
    guard_at = sub.index("except Exception:", loop_at)
    cont_at = sub.index("continue", guard_at)
    assert loop_at < guard_at < cont_at
