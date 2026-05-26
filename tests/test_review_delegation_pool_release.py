"""R8 MEDIUM #1: `_admit_delegation` releases the DB connection across
the L1 ack, and the `complete_task` wrong-agent drop is observable.

Pre-R8 `_admit_delegation` held a pooled connection + an open
transaction + a `SELECT … FOR UPDATE` row lock across the
up-to-30s `deliver_frame(await_ack=True)`. ~10 concurrent
delegations to slow/dead destinations exhausted the default
10-conn pool and stalled every other router DB operation.

The fix splits it into three phases: validate under a short-lived
lock → release → ack with no connection held → re-lock →
re-validate ("still non-terminal, still mine?" — the only
concurrent mutator during the ack window is cancel/timeout,
because the delegating socket is frozen and no other agent can
delegate the task) → flip. The optimistic
`reassign_active_agent` WHERE-guard backstops the flip.

The refactor slightly widens the rare window where the delegate
reports a Result between its ack and the Phase-C flip commit;
`complete_task` drops it (it isn't the active executor yet) and
the task hangs until the deadline sweep. That silent drop is now
counted via `result_from_wrong_agent_total{reporter=...}`.
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

from bp_protocol.frames import AckFrame, NewTaskFrame
from bp_protocol.types import TaskPriority, TaskState

# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_delegation.py's shape)
# ---------------------------------------------------------------------------


def _make_task_row(
    *,
    task_id: str = "tsk_x",
    state: TaskState = TaskState.RUNNING,
    active_agent_id: str = "agt_l0",
    agent_id: str = "agt_l0",
) -> Any:
    from bp_router.db.models import TaskRow

    return TaskRow(
        task_id=task_id,
        parent_task_id=None,
        root_task_id=task_id,
        user_id="usr_alice",
        session_id="ses_1",
        agent_id=agent_id,
        caller_agent_id="agt_caller",
        active_agent_id=active_agent_id,
        state=state,
        priority=TaskPriority.NORMAL,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        input={},
    )


def _make_frame(*, task_id: str = "tsk_x", destination: str = "agt_l1") -> NewTaskFrame:
    return NewTaskFrame(
        agent_id="agt_l0",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id=task_id,
        destination_agent_id=destination,
        user_id="usr_alice",
        session_id="ses_1",
        payload={"msg": "hand-off"},
    )


class _InstrumentedPool:
    """Tracks live connection checkouts. `live` is the number of
    `async with pool.acquire()` blocks currently entered."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.live = 0
        self.max_live = 0
        self.acquires = 0

    def acquire(self):  # type: ignore[no-untyped-def]
        pool = self

        class _Ctx:
            async def __aenter__(self_):  # noqa: N805
                pool.live += 1
                pool.acquires += 1
                pool.max_live = max(pool.max_live, pool.live)
                return pool._conn

            async def __aexit__(self_, *exc):  # noqa: N805
                pool.live -= 1
                return False

        return _Ctx()


def _make_state(
    *,
    task_row: Any,
    pool: Any | None = None,
    scope: Any | None = None,
) -> Any:
    state = MagicMock()
    state.settings = MagicMock()
    state.settings.pending_ack_timeout_s = 5.0
    state.settings.task_delegation_max_depth = 32

    if scope is None:
        scope = MagicMock()
        scope.lock_task_for_delegation = AsyncMock(return_value=task_row)
        scope.reassign_active_agent = AsyncMock(return_value=True)
        scope.insert_task_event = AsyncMock()
        scope.list_delegation_destinations = AsyncMock(return_value=[])

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    if pool is None:
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.db_pool = pool
    state._scope = scope
    state._conn = conn
    return state


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    state: Any,
    *,
    caller: Any,
    callee: Any,
    deliver: Any,
) -> None:
    from bp_router import acl as acl_mod
    from bp_router import tasks as tasks_mod
    from bp_router.db import queries as q_mod

    async def _get_agent(_c: Any, agent_id: str) -> Any:
        return caller if agent_id == caller.agent_id else callee

    monkeypatch.setattr(q_mod, "get_agent", _get_agent)
    monkeypatch.setattr(q_mod.Scope, "user", MagicMock(return_value=state._scope))

    async def _sl(_s: Any, _u: str) -> str:
        return "tier0"

    monkeypatch.setattr(tasks_mod, "_session_level", _sl)
    monkeypatch.setattr(
        acl_mod, "is_allowed_for",
        lambda *a, **k: MagicMock(allow=True, rule_name="ok"),
    )
    monkeypatch.setattr(
        tasks_mod, "is_allowed_for",
        lambda *a, **k: MagicMock(allow=True, rule_name="ok"),
    )

    async def _append_audit(*a: Any, **k: Any) -> None:
        pass

    monkeypatch.setattr(q_mod, "append_audit_event", _append_audit)
    monkeypatch.setattr(tasks_mod, "deliver_frame", deliver)


_CALLER = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
_CALLEE = MagicMock(agent_id="agt_l1", groups=[], capabilities=[], status="active")


# ===========================================================================
# 1. Core proof: connection released during the ack
# ===========================================================================


def test_connection_released_during_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row()
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn
    pool = _InstrumentedPool(conn)
    state = _make_state(task_row=task_row, pool=pool)

    observed: dict[str, int] = {}

    async def _deliver(_s, agent_id, frame, *, await_ack, timeout_s=None):  # type: ignore[no-untyped-def]
        # The whole point: Phase A must have RELEASED its connection
        # before we get here. Pre-fix, `live` would be 1 (Phase A's
        # conn still checked out across this await).
        observed["live_during_ack"] = pool.live
        return AckFrame(
            agent_id="agt_l1", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id="x", accepted=True,
        )

    _patch(monkeypatch, state, caller=_CALLER, callee=_CALLEE, deliver=_deliver)

    out = asyncio.run(
        tasks_mod._admit_delegation(state, _make_frame(), caller_agent_id="agt_l0")
    )
    assert out == "tsk_x"
    assert observed["live_during_ack"] == 0, (
        "a pooled connection was still checked out during the ack "
        "wait — the pool-exhaustion bug is back"
    )
    # Three acquires total: caller/callee lookup, Phase A, Phase C —
    # and never more than ONE concurrently.
    assert pool.acquires == 3
    assert pool.max_live == 1


# ===========================================================================
# 2. Source pin: deliver_frame is NOT lexically inside a transaction
# ===========================================================================


def test_deliver_frame_not_inside_transaction_block() -> None:
    from bp_router import tasks as tasks_mod

    src = textwrap.dedent(inspect.getsource(tasks_mod._admit_delegation))
    tree = ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)

    # Find every `await deliver_frame(...)` call node and assert NONE
    # of them has an `async with` (the pool.acquire / conn.transaction
    # blocks) as an ancestor within the function body.
    def _await_deliver_nodes(node: ast.AST) -> list[ast.AST]:
        out: list[ast.AST] = []
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "deliver_frame"
            ):
                out.append(n)
        return out

    deliver_calls = _await_deliver_nodes(fn)
    assert deliver_calls, "deliver_frame call not found — test stale"

    # Walk the AST tracking AsyncWith ancestry; the deliver_frame call
    # must be reachable WITHOUT passing through an AsyncWith.
    def _outside_async_with(node: ast.AST, inside_with: bool) -> bool:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "deliver_frame"
        ):
            return not inside_with
        for child in ast.iter_child_nodes(node):
            now_inside = inside_with or isinstance(node, ast.AsyncWith)
            if _outside_async_with(child, now_inside):
                return True
        return False

    assert _outside_async_with(fn, False), (
        "deliver_frame(await_ack=True) is nested inside an "
        "`async with` (pool.acquire/conn.transaction) — it must run "
        "with NO connection held (Phase B)"
    )


# ===========================================================================
# 3. Re-validate rejects a cancel that lands during the ack window
# ===========================================================================


def test_phase_c_rejects_terminal_after_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router import tasks as tasks_mod

    valid = _make_task_row(state=TaskState.RUNNING)
    cancelled = _make_task_row(state=TaskState.CANCELLED)

    scope = MagicMock()
    # Phase A sees RUNNING; Phase C re-lock sees CANCELLED.
    scope.lock_task_for_delegation = AsyncMock(side_effect=[valid, cancelled])
    scope.reassign_active_agent = AsyncMock(return_value=True)
    scope.insert_task_event = AsyncMock()
    scope.list_delegation_destinations = AsyncMock(return_value=[])
    state = _make_state(task_row=valid, scope=scope)

    async def _deliver(_s, a, f, *, await_ack, timeout_s=None):  # type: ignore[no-untyped-def]
        return AckFrame(
            agent_id="agt_l1", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id="x", accepted=True,
        )

    _patch(monkeypatch, state, caller=_CALLER, callee=_CALLEE, deliver=_deliver)

    with pytest.raises(tasks_mod.AdmitError) as exc:
        asyncio.run(
            tasks_mod._admit_delegation(state, _make_frame(), caller_agent_id="agt_l0")
        )
    assert exc.value.code == "task_terminal"
    # The flip must NOT have run — no delegating onto a cancelled task.
    scope.reassign_active_agent.assert_not_awaited()
    scope.insert_task_event.assert_not_awaited()
    # Both Phase A and Phase C re-locked.
    assert scope.lock_task_for_delegation.await_count == 2


# ===========================================================================
# 4. Re-validate rejects active-agent drift
# ===========================================================================


def test_phase_c_rejects_active_agent_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router import tasks as tasks_mod

    valid = _make_task_row(active_agent_id="agt_l0")
    drifted = _make_task_row(active_agent_id="agt_someone_else")

    scope = MagicMock()
    scope.lock_task_for_delegation = AsyncMock(side_effect=[valid, drifted])
    scope.reassign_active_agent = AsyncMock(return_value=True)
    scope.insert_task_event = AsyncMock()
    scope.list_delegation_destinations = AsyncMock(return_value=[])
    state = _make_state(task_row=valid, scope=scope)

    async def _deliver(_s, a, f, *, await_ack, timeout_s=None):  # type: ignore[no-untyped-def]
        return AckFrame(
            agent_id="agt_l1", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id="x", accepted=True,
        )

    _patch(monkeypatch, state, caller=_CALLER, callee=_CALLEE, deliver=_deliver)

    with pytest.raises(tasks_mod.AdmitError) as exc:
        asyncio.run(
            tasks_mod._admit_delegation(state, _make_frame(), caller_agent_id="agt_l0")
        )
    assert exc.value.code == "not_active_executor"
    scope.reassign_active_agent.assert_not_awaited()


# ===========================================================================
# 5. Happy path unchanged
# ===========================================================================


def test_happy_path_flips_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row()
    scope = MagicMock()
    scope.lock_task_for_delegation = AsyncMock(return_value=task_row)
    scope.reassign_active_agent = AsyncMock(return_value=True)
    scope.insert_task_event = AsyncMock()
    scope.list_delegation_destinations = AsyncMock(return_value=[])
    state = _make_state(task_row=task_row, scope=scope)

    delivered: list[Any] = []

    async def _deliver(_s, agent_id, frame, *, await_ack, timeout_s=None):  # type: ignore[no-untyped-def]
        delivered.append((agent_id, frame))
        return AckFrame(
            agent_id="agt_l1", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id="x", accepted=True,
        )

    _patch(monkeypatch, state, caller=_CALLER, callee=_CALLEE, deliver=_deliver)

    out = asyncio.run(
        tasks_mod._admit_delegation(state, _make_frame(), caller_agent_id="agt_l0")
    )
    assert out == "tsk_x"
    assert len(delivered) == 1
    assert delivered[0][0] == "agt_l1"
    assert delivered[0][1].delegating_agent_id == "agt_l0"
    scope.reassign_active_agent.assert_awaited_once()
    kw = scope.reassign_active_agent.await_args.kwargs
    assert kw["new_active_agent_id"] == "agt_l1"
    assert kw["expected_current_agent_id"] == "agt_l0"
    scope.insert_task_event.assert_awaited_once()
    # Phase A + Phase C each re-locked.
    assert scope.lock_task_for_delegation.await_count == 2


def test_rejected_ack_no_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Destination rejects → AdmitError('rejected'), no flip, and
    Phase C never runs (no second lock)."""
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row()
    scope = MagicMock()
    scope.lock_task_for_delegation = AsyncMock(return_value=task_row)
    scope.reassign_active_agent = AsyncMock(return_value=True)
    scope.insert_task_event = AsyncMock()
    scope.list_delegation_destinations = AsyncMock(return_value=[])
    state = _make_state(task_row=task_row, scope=scope)

    async def _deliver(_s, a, f, *, await_ack, timeout_s=None):  # type: ignore[no-untyped-def]
        return AckFrame(
            agent_id="agt_l1", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id="x", accepted=False, reason="busy",
        )

    _patch(monkeypatch, state, caller=_CALLER, callee=_CALLEE, deliver=_deliver)

    with pytest.raises(tasks_mod.AdmitError) as exc:
        asyncio.run(
            tasks_mod._admit_delegation(state, _make_frame(), caller_agent_id="agt_l0")
        )
    assert exc.value.code == "rejected"
    scope.reassign_active_agent.assert_not_awaited()
    # Only Phase A locked; Phase C unreached because the ack was a
    # rejection.
    assert scope.lock_task_for_delegation.await_count == 1


# ===========================================================================
# 6. #2 residual: complete_task drop is countable
# ===========================================================================


def test_metric_exists_with_bounded_reporter_label() -> None:
    from bp_router.observability import metrics

    m = metrics.result_from_wrong_agent_total
    assert list(m._labelnames) == ["reporter"]  # type: ignore[attr-defined]
    # No agent_id/task_id labels (cardinality discipline).
    assert "agent_id" not in m._labelnames  # type: ignore[attr-defined]
    assert "task_id" not in m._labelnames  # type: ignore[attr-defined]


def test_complete_task_drop_increments_metric_by_reporter() -> None:
    """`complete_task` increments `result_from_wrong_agent_total`
    with reporter=`owning` when the original task agent reports
    late, and `other` for any other mismatch."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ResultFrame
    from bp_protocol.types import TaskStatus
    from bp_router import tasks as tasks_mod
    from bp_router.observability import metrics

    def _val(reporter: str) -> float:
        try:
            return metrics.result_from_wrong_agent_total.labels(
                reporter=reporter
            )._value.get()  # type: ignore[attr-defined]
        except Exception:
            return 0.0

    # Task owned by agt_owner, currently active = agt_active (a
    # delegation moved it). A Result from agt_owner → "owning";
    # from agt_random → "other".
    async def _run(reporter_agent: str) -> None:
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "user_id": "usr_alice",
            "parent_task_id": None,
            "caller_agent_id": "agt_caller",
            "agent_id": "agt_owner",
            "active_agent_id": "agt_active",
        })
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state = MagicMock()
        state.db_pool = pool

        frame = ResultFrame(
            agent_id=reporter_agent,
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="tsk_x",
            status=TaskStatus.SUCCEEDED,
            status_code=200,
        )
        await tasks_mod.complete_task(
            state, frame, reporting_agent_id=reporter_agent
        )

    owning_before = _val("owning")
    other_before = _val("other")

    asyncio.run(_run("agt_owner"))
    assert _val("owning") == owning_before + 1
    assert _val("other") == other_before

    asyncio.run(_run("agt_random"))
    assert _val("other") == other_before + 1
