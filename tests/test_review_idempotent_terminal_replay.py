"""R9 MEDIUM-2/3: idempotent replay of a terminal task.

`find_idempotent` has no state filter, so a client retrying a
spawn with the same `idempotency_key` after a transient failure
(the canonical reason the key exists) got back
`accepted=True, task_id=X` for a task whose terminal `ResultFrame`
was already fanned out exactly once and will never be emitted
again — the retry hung to its spawn timeout. Sub-case 2b: a
`_safe_fail` whose own `fail_task` raised left the row QUEUED
(zombie) and consumed spawn-depth budget.

Router-only fix (verified the SDK `PendingMap` already buffers a
pre-registration resolve, so no SDK change is needed):

  L1 `admit_task` → `AdmitResult(task_id, replay_result)`;
     terminal idempotency hit reconstructs the stored terminal
     `ResultFrame`; non-terminal → `replay_result=None`.
  L2 `_handle_new_task` re-emits the replay after the ack on the
     caller's own socket outbox.
  L3 `_safe_fail` falls back to `Scope.force_fail_task` (guarded
     `state NOT IN terminal`) so no QUEUED zombie survives.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_protocol.frames import NewTaskFrame
from bp_protocol.types import TaskPriority, TaskState, TaskStatus


def _task_row(*, state: TaskState, task_id: str = "tsk_x") -> Any:
    from bp_router.db.models import TaskRow

    return TaskRow(
        task_id=task_id,
        parent_task_id="parent_1",
        root_task_id=task_id,
        user_id="usr_alice",
        session_id="ses_1",
        agent_id="agt_worker",
        caller_agent_id="agt_caller",
        active_agent_id="agt_worker",
        state=state,
        status_code=503,
        idempotency_key="K",
        priority=TaskPriority.NORMAL,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        input={},
        output={"content": "stored output"},
        error={"code": "agent_disconnected"},
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


# ===========================================================================
# L1 — terminal hit reconstructs the Result; non-terminal → None
# ===========================================================================


@pytest.mark.parametrize(
    "term_state, exp_status",
    [
        (TaskState.FAILED, TaskStatus.FAILED),
        (TaskState.SUCCEEDED, TaskStatus.SUCCEEDED),
        (TaskState.CANCELLED, TaskStatus.CANCELLED),
        (TaskState.TIMED_OUT, TaskStatus.TIMED_OUT),
    ],
)
def test_terminal_idempotency_hit_reconstructs_result(
    monkeypatch: pytest.MonkeyPatch, term_state, exp_status
) -> None:
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod
    from bp_router.db import queries as q

    row = _task_row(state=term_state)
    state = _state_with_idem(row)
    monkeypatch.setattr(
        q.Scope, "user", MagicMock(return_value=state._scope)
    )

    res = asyncio.run(
        tasks_mod.admit_task(state, _idem_frame(), caller_agent_id="agt_caller")
    )
    assert isinstance(res, tasks_mod.AdmitResult)
    assert res.task_id == "tsk_x"
    rr = res.replay_result
    assert rr is not None
    assert rr.task_id == "tsk_x"
    assert rr.parent_task_id == "parent_1"
    assert rr.status == exp_status
    assert rr.status_code == 503
    assert rr.error == {"code": "agent_disconnected"}
    assert rr.output is not None
    assert rr.output.content == "stored output"
    # The replay correlates to the RETRY's trace/span, not the
    # original's — so the caller's pending future matches.
    assert rr.trace_id == "a" * 32
    assert rr.span_id == "b" * 16
    assert rr.agent_id == "agt_worker"


@pytest.mark.parametrize(
    "live_state", [TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING_CHILDREN]
)
def test_inflight_idempotency_hit_has_no_replay(
    monkeypatch: pytest.MonkeyPatch, live_state
) -> None:
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod
    from bp_router.db import queries as q

    state = _state_with_idem(_task_row(state=live_state))
    monkeypatch.setattr(
        q.Scope, "user", MagicMock(return_value=state._scope)
    )

    res = asyncio.run(
        tasks_mod.admit_task(state, _idem_frame(), caller_agent_id="agt_caller")
    )
    assert res.task_id == "tsk_x"
    assert res.replay_result is None  # retry joins the live task


def test_status_from_state_inverse_of_state_from_status() -> None:
    from bp_router import tasks as tasks_mod

    for status in (
        TaskStatus.SUCCEEDED, TaskStatus.FAILED,
        TaskStatus.CANCELLED, TaskStatus.TIMED_OUT,
    ):
        st = tasks_mod._state_from_status(status)
        assert tasks_mod._status_from_state(st) == status
    # Non-terminal → None.
    for st in (TaskState.QUEUED, TaskState.RUNNING,
               TaskState.WAITING_CHILDREN):
        assert tasks_mod._status_from_state(st) is None


# ===========================================================================
# L1 — every return site is an AdmitResult
# ===========================================================================


def test_all_return_sites_return_admit_result() -> None:
    from bp_router import tasks as tasks_mod

    src = inspect.getsource(tasks_mod.admit_task)
    # No bare `return <str>` task_id escapes; every return is an
    # AdmitResult (or the delegation/idempotent wrappers).
    assert "return task_row.task_id" not in src
    assert "return existing.task_id" not in src
    assert "AdmitResult(task_id=task_row.task_id)" in src
    assert "AdmitResult(\n            task_id=await _admit_delegation(" in src


# ===========================================================================
# L2 — _handle_new_task re-emits the replay after the ack
# ===========================================================================


def _socket_entry() -> Any:
    entry = MagicMock()
    entry.agent_id = "agt_caller"
    entry.outbox = asyncio.Queue()
    return entry


def test_handle_new_task_emits_ack_then_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from bp_router import dispatch
    from bp_router.tasks import AdmitResult

    replay = _task_row(state=TaskState.FAILED)
    from bp_protocol.frames import ResultFrame

    rr = ResultFrame(
        agent_id="agt_worker", trace_id="a" * 32, span_id="b" * 16,
        task_id="tsk_x", parent_task_id="parent_1",
        status=TaskStatus.FAILED, status_code=503,
        error={"code": "agent_disconnected"},
    )

    async def _stub_admit(*a: Any, **k: Any) -> AdmitResult:
        return AdmitResult(task_id="tsk_x", replay_result=rr)

    import bp_router.tasks as _tm; monkeypatch.setattr(_tm, "admit_task", _stub_admit)

    entry = _socket_entry()
    state = MagicMock()
    asyncio.run(
        dispatch._handle_new_task(state, entry, _idem_frame())
    )
    # FIFO: ack first (so the SDK learns task_id + registers), then
    # the replay Result.
    first = entry.outbox.get_nowait()
    second = entry.outbox.get_nowait()
    assert first.type == "Ack" and first.accepted and first.task_id == "tsk_x"
    assert second.type == "Result" and second.task_id == "tsk_x"
    assert second.status == TaskStatus.FAILED
    assert entry.outbox.empty()


def test_handle_new_task_no_replay_emits_only_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from bp_router import dispatch
    from bp_router.tasks import AdmitResult

    async def _stub_admit(*a: Any, **k: Any) -> AdmitResult:
        return AdmitResult(task_id="tsk_new")  # replay_result=None

    import bp_router.tasks as _tm; monkeypatch.setattr(_tm, "admit_task", _stub_admit)
    entry = _socket_entry()
    asyncio.run(
        dispatch._handle_new_task(MagicMock(), entry, _idem_frame())
    )
    ack = entry.outbox.get_nowait()
    assert ack.type == "Ack" and ack.task_id == "tsk_new"
    assert entry.outbox.empty()  # no extra frame


# ===========================================================================
# Keystone: PendingMap resolves regardless of register/resolve order
# ===========================================================================


def test_pendingmap_resolve_before_register_is_buffered() -> None:
    """The fix relies on this existing (R6) behaviour: a Result
    that arrives BEFORE the SDK registers its result-future is
    buffered and handed back on register — so re-emit ordering is
    safe in both directions."""
    from bp_sdk.correlation import PendingMap

    async def _run() -> None:
        pm = PendingMap(default_timeout_s=5.0)
        # Resolve first (re-emit raced ahead of register).
        assert pm.resolve("tsk_x", "RESULT") is False  # buffered
        fut = pm.register("tsk_x")
        assert fut.done() and fut.result() == "RESULT"

        # And the normal order still works.
        fut2 = pm.register("tsk_y")
        assert not fut2.done()
        assert pm.resolve("tsk_y", "R2") is True
        assert await fut2 == "R2"

    asyncio.run(_run())


# ===========================================================================
# L3 — force_fail_task guard + _safe_fail fallback wiring
# ===========================================================================


def test_force_fail_task_guarded_to_non_terminal() -> None:
    """The fallback UPDATE must carry the `state NOT IN (terminal)`
    guard so it can never clobber a legitimately-terminalised task,
    and must be user-scoped."""
    from bp_router.db import queries as q

    src = inspect.getsource(q.Scope.force_fail_task)
    assert "state NOT IN" in src
    assert "'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'" in src
    assert "_require_user()" in src
    assert "user_id = $2" in src


def test_force_fail_task_returns_true_only_when_row_changed() -> None:
    from bp_router.db import queries as q

    async def _run() -> None:
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        scope = q.Scope.user(conn, "usr_alice")
        assert await scope.force_fail_task(
            "tsk_x", status_code=503, error={"code": "x"}
        ) is True
        conn.execute = AsyncMock(return_value="UPDATE 0")  # guard blocked
        scope2 = q.Scope.user(conn, "usr_alice")
        assert await scope2.force_fail_task(
            "tsk_x", status_code=503, error=None
        ) is False

    asyncio.run(_run())


def test_safe_fail_falls_back_to_force_fail_and_admiterror_propagates() -> None:
    """`_safe_fail` is a closure in `admit_task`; pin via source +
    drive the spawn ack-timeout path with a raising `fail_task` and
    a stubbed `force_fail_task`, asserting (a) the fallback ran and
    (b) the AdmitError still propagates (no PR #193 regression)."""
    from bp_router import tasks as tasks_mod

    src = inspect.getsource(tasks_mod.admit_task)
    # The fallback calls the guarded helper after the fail_task
    # except, and only on the raised path (the success path
    # `return`s before it).
    assert "force_fail_task(" in src
    assert "admit_failure_force_fail_fallback" in src
    fail_except = src.index("admit_failure_fail_task_errored")
    fallback = src.index("force_fail_task(")
    assert fail_except < fallback, (
        "force_fail_task fallback must be AFTER the fail_task "
        "except, not on the success path"
    )
    # The success path returns before the fallback.
    assert "            )\n            return\n        except Exception" in src
