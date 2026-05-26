"""R10 MED/LOW: residuals from the R9 #200 idempotent-replay audit.

Nit A (LOW, real fix): `admit_task`'s idempotent-replay
   reconstruction used `existing.status_code or 0`. Router-
   synthesised terminals leave `tasks.status_code` NULL —
   notably CANCELLED (`cancel_task` transitions without a
   status_code; `task_transition`'s COALESCE keeps the NULL) —
   yet the ORIGINAL one-shot fan-out delivered 499. The `… or 0`
   made the replay UNFAITHFUL: the retrying caller saw
   status_code=0 where the original saw 499. Fixed to a faithful
   per-terminal-status default.

Nit B (MED, verified benign — documented + pinned, NOT a
   behavioural change): the non-locking `find_idempotent` read
   means a replay can rarely coincide with the original task's
   one-shot fan-out on the same agent socket → the SDK sees two
   terminal frames for one task_id. `PendingMap` absorbs this by
   construction: the first `resolve` pops the pending future; the
   second finds nothing and goes to the bounded, self-expiring
   `_buffered` keyed by the unique dead task_id — never
   mis-delivered, never unbounded. This file pins that property
   so the dispatch.py safety argument can't silently regress
   (adding router-side dedup would be unwarranted machinery).
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_protocol.frames import NewTaskFrame
from bp_protocol.types import TaskPriority, TaskState, TaskStatus

# ---------------------------------------------------------------------------
# harness (mirrors tests/test_review_idempotent_terminal_replay.py)
# ---------------------------------------------------------------------------


def _row(*, state: TaskState, status_code: int | None) -> Any:
    from bp_router.db.models import TaskRow

    return TaskRow(
        task_id="tsk_x",
        parent_task_id="parent_1",
        root_task_id="tsk_x",
        user_id="usr_alice",
        session_id="ses_1",
        agent_id="agt_worker",
        caller_agent_id="agt_caller",
        active_agent_id="agt_worker",
        state=state,
        status_code=status_code,
        idempotency_key="K",
        priority=TaskPriority.NORMAL,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        input={},
        output=None,
        error={"code": "cancelled"},
    )


def _idem_frame() -> NewTaskFrame:
    return NewTaskFrame(
        agent_id="agt_caller",
        trace_id="a" * 32,
        span_id="b" * 16,
        destination_agent_id="agt_worker",
        user_id="usr_alice",
        session_id="ses_1",
        idempotency_key="K",
    )


def _state_with_idem(existing_row: Any) -> Any:
    state = MagicMock()
    scope = MagicMock()
    scope.find_idempotent = AsyncMock(return_value=existing_row)
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.db_pool = pool
    state._scope = scope
    return state


def _admit(state: Any) -> Any:
    from bp_router import tasks as tasks_mod
    from bp_router.db import queries as q

    return asyncio.run(
        _admit_with_patch(tasks_mod, q, state)
    )


async def _admit_with_patch(tasks_mod, q, state):  # type: ignore[no-untyped-def]
    import unittest.mock as m

    with m.patch.object(q.Scope, "user", MagicMock(return_value=state._scope)):
        return await tasks_mod.admit_task(
            state, _idem_frame(), caller_agent_id="agt_caller"
        )


# ===========================================================================
# Nit A — faithful status_code for NULL-status-code terminals
# ===========================================================================


def test_cancelled_null_status_code_replays_499_not_zero() -> None:
    """The headline bug: a CANCELLED task stores NULL status_code
    but the original cancel fan-out delivered 499. The replay must
    say 499, never the old `… or 0` artifact."""
    pytest.importorskip("fastapi")
    state = _state_with_idem(_row(state=TaskState.CANCELLED, status_code=None))
    res = _admit(state)
    assert res.replay_result is not None
    assert res.replay_result.status == TaskStatus.CANCELLED
    assert res.replay_result.status_code == 499
    assert res.replay_result.status_code != 0


@pytest.mark.parametrize(
    "term_state, expected_code",
    [
        (TaskState.CANCELLED, 499),
        (TaskState.TIMED_OUT, 504),
        (TaskState.FAILED, 500),
        (TaskState.SUCCEEDED, 200),
    ],
)
def test_null_status_code_uses_canonical_default(
    term_state, expected_code
) -> None:
    """Every terminal status has a faithful default mirroring the
    original synthetic fan-out — none collapse to 0."""
    pytest.importorskip("fastapi")
    state = _state_with_idem(_row(state=term_state, status_code=None))
    res = _admit(state)
    assert res.replay_result is not None
    assert res.replay_result.status_code == expected_code


def test_stored_status_code_is_preserved_not_overridden() -> None:
    """The not-NULL path is unchanged: an agent/`fail_task`
    terminal that persisted a real code replays THAT code, never
    the default-map value (regression guard on the normal path)."""
    pytest.importorskip("fastapi")
    # FAILED default is 500; a stored 503 must win.
    state = _state_with_idem(_row(state=TaskState.FAILED, status_code=503))
    res = _admit(state)
    assert res.replay_result is not None
    assert res.replay_result.status_code == 503


def test_default_map_total_over_terminal_status_set() -> None:
    """`_DEFAULT_TERMINAL_STATUS_CODE` must cover every terminal
    `TaskStatus` `_STATUS_FROM_STATE` can yield — otherwise a
    NULL-status terminal of a forgotten status KeyErrors (loud,
    acceptable) or, worse, a future `.get(..., 0)` silently
    reintroduces the bug. Pin totality now."""
    from bp_router.tasks import (
        _DEFAULT_TERMINAL_STATUS_CODE,
        _STATUS_FROM_STATE,
    )

    terminal_statuses = set(_STATUS_FROM_STATE.values())
    assert terminal_statuses <= set(_DEFAULT_TERMINAL_STATUS_CODE)
    # And no default is the falsy 0 sentinel the fix removed.
    assert all(c != 0 for c in _DEFAULT_TERMINAL_STATUS_CODE.values())


def test_replay_reconstruction_has_no_or_zero_coercion() -> None:
    """AST guard: the `status_code=` keyword in the replay
    `ResultFrame(...)` must not be a `<x> or 0` BoolOp — the exact
    unfaithful pattern this fix removed."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    src = textwrap.dedent(inspect.getsource(tasks_mod.admit_task))
    tree = ast.parse(src)

    offending = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)):
            continue
        for kw in node.keywords:
            if kw.arg != "status_code":
                continue
            for sub in ast.walk(kw.value):
                if (
                    isinstance(sub, ast.BoolOp)
                    and isinstance(sub.op, ast.Or)
                    and any(
                        isinstance(v, ast.Constant) and v.value == 0
                        for v in sub.values
                    )
                ):
                    offending.append(ast.dump(sub))
    assert not offending, f"`status_code= … or 0` reintroduced: {offending}"


# ===========================================================================
# Nit B — the double-deliver window is absorbed by PendingMap
# ===========================================================================


def test_pendingmap_absorbs_duplicate_terminal_for_resolved_task() -> None:
    """The property the dispatch.py replay comment relies on: a
    SECOND terminal frame for an already-resolved task_id is
    buffered (not mis-delivered, not crashing), and an unrelated
    waiter is untouched."""
    from bp_sdk.correlation import PendingMap

    async def _run() -> None:
        pm = PendingMap(default_timeout_s=30.0)

        fut = pm.register("tsk_dup")
        # First (real or replay) terminal frame resolves the waiter.
        assert pm.resolve("tsk_dup", "first") is True
        assert fut.done() and fut.result() == "first"

        # Second (the double-deliver) finds no pending entry → goes
        # to the bounded buffer, returns False, does NOT raise and
        # does NOT touch any other waiter.
        other = pm.register("tsk_other")
        assert pm.resolve("tsk_dup", "second") is False
        assert "tsk_dup" in pm._buffered  # parked, bounded
        assert not other.done()  # unrelated waiter unaffected

        # The dead task_id is unique — nothing will ever
        # `register("tsk_dup")` again, so the parked value just
        # ages out via the buffer sweep. Bound exists:
        assert isinstance(PendingMap.BUFFER_MAX_SIZE, int)
        assert PendingMap.BUFFER_MAX_SIZE > 0

    asyncio.run(_run())


def test_dispatch_documents_benign_double_deliver() -> None:
    """Pin the rationale in source: the replay site must explain
    why the window is benign so nobody bolts on router-side dedup
    for a PendingMap-absorbed duplicate."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_new_task)
    assert "Double-deliver window" in src
    assert "_buffer_late_value" in src or "PendingMap" in src
