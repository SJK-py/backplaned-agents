"""R11 HIGH: concurrent same-key spawn races the idempotency INSERT.

`find_idempotent` (step 1) is a non-locking read with no
INSERT-ON-CONFLICT backstop. The canonical post-ack_timeout retry
legitimately lands on a *second socket / worker*, so two admits
for the same (user_id, idempotency_key) don't serialise: both miss
step 1, the second's `create_task` INSERT hits
`tasks_idempotency_unique` and raised a raw
`asyncpg.UniqueViolationError` → `_handle_new_task`'s bare except
→ `internal_error` ack. The idempotency key failed in exactly the
race it exists for.

Fix: the create_task `except asyncpg.UniqueViolationError`
re-resolves against the winner's committed row (fresh connection —
the current txn is aborted by the violation) via the shared
`_idempotent_admit_result`, so the loser joins / replays just like
step 1 would have. Also folds in the LOW defensive: a malformed
stored `output` no longer turns a replay into `internal_error`.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime
from typing import Any

import pytest

from bp_protocol.frames import NewTaskFrame
from bp_protocol.types import TaskPriority, TaskState, TaskStatus


def _row(*, state: TaskState, status_code: int | None, output: Any) -> Any:
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
        output=output,
        error={"code": "x"},
    )


def _frame() -> NewTaskFrame:
    return NewTaskFrame(
        agent_id="agt_caller", trace_id="a" * 32, span_id="b" * 16,
        destination_agent_id="agt_worker", user_id="usr_alice",
        session_id="ses_1", idempotency_key="K",
    )


# ---------------------------------------------------------------------------
# _idempotent_admit_result — shared resolver (used by step 1 AND the race)
# ---------------------------------------------------------------------------


def test_resolver_terminal_replays_faithful_status_code() -> None:
    from bp_router.tasks import AdmitResult, _idempotent_admit_result

    # CANCELLED stores NULL status_code → faithful 499 (not 0).
    out = _idempotent_admit_result(
        _row(state=TaskState.CANCELLED, status_code=None, output=None), _frame()
    )
    assert isinstance(out, AdmitResult)
    assert out.task_id == "tsk_x"
    assert out.replay_result is not None
    assert out.replay_result.status == TaskStatus.CANCELLED
    assert out.replay_result.status_code == 499
    # Replay correlates to the RETRY's trace/span.
    assert out.replay_result.trace_id == "a" * 32


def test_resolver_non_terminal_joins_live_task() -> None:
    from bp_router.tasks import _idempotent_admit_result

    out = _idempotent_admit_result(
        _row(state=TaskState.RUNNING, status_code=None, output=None), _frame()
    )
    assert out.task_id == "tsk_x"
    assert out.replay_result is None  # join, no replay


def test_resolver_tolerates_malformed_stored_output() -> None:
    """LOW defensive: a malformed `output` blob must NOT make the
    replay raise (→ internal_error). It degrades to output=None,
    keeping the terminal status/error faithful."""
    from bp_router.tasks import _idempotent_admit_result

    out = _idempotent_admit_result(
        _row(
            state=TaskState.SUCCEEDED,
            status_code=200,
            # AgentOutput.model_validate will reject this shape.
            output={"content": {"not": "a string"}, "bogus": object()},
        ),
        _frame(),
    )
    assert out.replay_result is not None
    assert out.replay_result.status == TaskStatus.SUCCEEDED
    assert out.replay_result.output is None  # degraded, not raised


def test_safe_rehydrate_output_paths() -> None:
    from bp_protocol.types import AgentOutput
    from bp_router.tasks import _safe_rehydrate_output

    assert _safe_rehydrate_output(None) is None
    good = _safe_rehydrate_output({"content": "hi"})
    assert isinstance(good, AgentOutput) and good.content == "hi"
    assert _safe_rehydrate_output({"content": object()}) is None  # malformed


# ---------------------------------------------------------------------------
# Structural pins — the race handler exists and is correctly shaped
# ---------------------------------------------------------------------------


def _admit_src() -> str:
    from bp_router import tasks

    return textwrap.dedent(inspect.getsource(tasks.admit_task))


def test_step1_idempotency_delegates_to_shared_resolver() -> None:
    """Step 1 must call `_idempotent_admit_result` (not inline a
    second copy of the reconstruct/join logic) so it can't diverge
    from the race handler."""
    pytest.importorskip("fastapi")
    src = _admit_src()
    # No inline `ResultFrame(` reconstruction left in admit_task —
    # it lives only in the shared helper now.
    assert "_idempotent_admit_result(" in src
    tree = ast.parse(src).body[0]
    inline_resultframe = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "ResultFrame"
    ]
    assert not inline_resultframe, (
        "admit_task must not reconstruct ResultFrame inline — "
        "use the shared _idempotent_admit_result"
    )


def test_create_task_has_unique_violation_race_handler() -> None:
    """The create_task `try` must have an
    `except asyncpg.UniqueViolationError` that: gates on the
    idempotency constraint (else re-raise), re-looks-up via a fresh
    pool.acquire (the current txn is aborted), re-raises if the row
    isn't visible, else returns `_idempotent_admit_result`."""
    pytest.importorskip("fastapi")
    src = _admit_src()
    tree = ast.parse(src).body[0]

    handler = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            t = h.type
            is_uv = (
                isinstance(t, ast.Attribute)
                and t.attr == "UniqueViolationError"
            )
            if is_uv:
                handler = h
    assert handler is not None, "no except asyncpg.UniqueViolationError"

    hsrc = ast.get_source_segment(src, handler) or ""
    # Gates on the specific constraint, re-raises otherwise.
    assert "tasks_idempotency_unique" in hsrc
    assert "constraint_name" in hsrc
    # Fresh connection re-lookup (current txn aborted).
    assert "pool.acquire()" in hsrc
    assert "find_idempotent" in hsrc
    # Bare re-raise present (non-idempotency UV / racer-None
    # defensive) AND a delegated resolve.
    assert any(
        isinstance(n, ast.Raise) and n.exc is None
        for n in ast.walk(handler)
    ), "must bare-re-raise non-idempotency / invisible-row cases"
    assert "_idempotent_admit_result(" in hsrc


def test_asyncpg_imported_for_the_handler() -> None:
    """The `except asyncpg.UniqueViolationError` needs `asyncpg`
    bound when the exception matches — admit_task imports it
    (function-local, matching the api/admin.py pattern)."""
    pytest.importorskip("fastapi")
    src = _admit_src()
    assert "import asyncpg" in src
