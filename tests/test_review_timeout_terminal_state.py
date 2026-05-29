"""Deadline sweep transitions to TIMED_OUT, not FAILED.

Pre-release blocker: `_sweep_once` routed expired tasks through `fail_task`,
which hard-coded `TaskState.FAILED` for both the task's own transition and
the parent fan-out `status`. No code path ever wrote `TIMED_OUT`, so a
deadline-expired task was reported `failed` (not `timed_out`) to its caller,
in audit/metrics, and on idempotent replay — contradicting `state.md` §1.5,
and leaving the whole `TIMED_OUT` branch of the state table dead.

Fix: `fail_task` gained a `terminal_state` parameter (default `FAILED`) that
drives BOTH the transition and the parent Result status; `_sweep_once` passes
`TaskState.TIMED_OUT`. The replay map (`_STATUS_FROM_STATE` /
`_DEFAULT_TERMINAL_STATUS_CODE`) already handled TIMED_OUT → 504.

Behavioural tests use the same fake-`state` harness as
`test_review_fail_task_cascade_cancel.py`.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _patch_transition(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    from bp_router import tasks

    calls: list[tuple[str, Any]] = []

    async def _transition(*args: Any, **kwargs: Any) -> Any:
        calls.append((args[1], args[2]))  # (task_id, new_state)
        return MagicMock()

    monkeypatch.setattr(tasks, "task_transition", _transition)
    return calls


def _patch_deliver(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    from bp_router import tasks

    delivered: list[tuple[str, Any]] = []

    async def _deliver(state, agent_id, frame, *, await_ack):  # type: ignore[no-untyped-def]
        delivered.append((agent_id, frame))

    monkeypatch.setattr(tasks, "deliver_frame", _deliver)
    return delivered


def _make_state(monkeypatch: pytest.MonkeyPatch, *, parent_task_id, parent_owner):  # type: ignore[no-untyped-def]
    """Fake `state` for a ROOT failing task with NO descendants but a parent
    (so the only fan-out is the upward parent Result)."""
    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool

    scope = MagicMock()
    scope.list_descendants = AsyncMock(return_value=[])

    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    async def _fetchrow(query: str, *args: Any) -> Any:
        if "SELECT parent_task_id" in query:
            return {"parent_task_id": parent_task_id}
        if "SELECT agent_id FROM tasks" in query:
            return {"agent_id": parent_owner}
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
    return state


def _run_fail(monkeypatch, **kw):  # type: ignore[no-untyped-def]
    from bp_router import tasks

    delivered = _patch_deliver(monkeypatch)
    calls = _patch_transition(monkeypatch)
    state = _make_state(
        monkeypatch, parent_task_id="tsk_parent", parent_owner="agt_parent"
    )
    asyncio.run(
        tasks.fail_task(
            state, "tsk_failing", user_id="usr_alice",
            status_code=kw.pop("status_code", 504), reason="x", **kw,
        )
    )
    return calls, delivered


def test_fail_task_defaults_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState, TaskStatus

    calls, delivered = _run_fail(monkeypatch)  # no terminal_state → default
    assert ("tsk_failing", TaskState.FAILED) in calls
    parent = [f for a, f in delivered if a == "agt_parent"]
    assert parent and parent[0].status == TaskStatus.FAILED


def test_fail_task_timed_out_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """terminal_state=TIMED_OUT drives the transition AND the parent status."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState, TaskStatus

    calls, delivered = _run_fail(
        monkeypatch, terminal_state=TaskState.TIMED_OUT, status_code=504
    )
    # The failing task itself goes TIMED_OUT (not FAILED).
    assert ("tsk_failing", TaskState.TIMED_OUT) in calls
    assert ("tsk_failing", TaskState.FAILED) not in calls
    # The parent is told timed_out/504, not failed.
    parent = [f for a, f in delivered if a == "agt_parent"]
    assert parent and parent[0].status == TaskStatus.TIMED_OUT
    assert parent[0].status_code == 504


def test_sweep_passes_timed_out_terminal_state() -> None:
    """Source pin: the deadline sweep routes fail_task to TIMED_OUT."""
    from bp_router import tasks

    src = inspect.getsource(tasks._sweep_once)
    assert "terminal_state=TaskState.TIMED_OUT" in src


def test_fail_task_no_hardcoded_failed_in_transition_or_fanout() -> None:
    """Regression guard: fail_task must use `terminal_state` for the task's
    own transition and the parent fan-out status (no literal FAILED there)."""
    from bp_router import tasks

    src = inspect.getsource(tasks.fail_task)
    assert "_STATUS_FROM_STATE[terminal_state]" in src
    # The only FAILED literal left should be the default param value.
    assert src.count("TaskState.FAILED") == 1
    assert "status=TaskStatus.FAILED" not in src
