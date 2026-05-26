"""R8 HIGH: delegation cycle / depth detection.

Without this guard, an LLM agent that misuses `delegate(...)` can
bounce `A→B→A→B...` indefinitely. Each hop consumes a dispatcher
PendingMap slot, an ack timeout, a `task_events` row, and an
`audit_log` row, all keyed off the SAME `task_id` (delegation
re-uses the task; only `active_agent_id` flips). The chain is
bounded only by ad-hoc LLM behaviour, which is not a safety
property.

The fix:

1. New setting `task_delegation_max_depth` (default 32, hard cap
   128) — refuse delegation once the chain reaches the cap.
2. New `Scope.list_delegation_destinations(task_id)` query that
   pulls the `to` of every prior `kind='delegated'` task_event.
3. In `_admit_delegation`, inside the FOR UPDATE transaction, scan
   the history and refuse if the new destination has already been
   active on this task (`task_row.agent_id` plus every prior `to`).

Both refusals run BEFORE `deliver_frame` so the destination never
sees the bouncing delegation — the caller gets an immediate
`AdmitError` with code `delegation_cycle` or
`delegation_depth_exceeded`.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_protocol.frames import AckFrame, NewTaskFrame
from bp_protocol.types import TaskPriority, TaskState

# ---------------------------------------------------------------------------
# Helpers (mirrors test_delegation.py's shape)
# ---------------------------------------------------------------------------


def _make_task_row(
    *,
    task_id: str = "tsk_x",
    state: TaskState = TaskState.RUNNING,
    active_agent_id: str = "agt_l0",
    caller_agent_id: str = "agt_caller",
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
        caller_agent_id=caller_agent_id,
        active_agent_id=active_agent_id,
        state=state,
        priority=TaskPriority.NORMAL,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        input={},
    )


def _make_delegation_frame(
    *,
    task_id: str = "tsk_x",
    destination: str = "agt_l1",
) -> NewTaskFrame:
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


def _delegation_state(
    *,
    task_row: Any,
    prior_destinations: list[str] | None = None,
    max_depth: int = 32,
) -> tuple[Any, list[dict[str, Any]]]:
    from bp_router.security.rate_limit import Decision

    state = MagicMock()
    state.settings = MagicMock()
    state.settings.pending_ack_timeout_s = 5.0
    state.settings.quota_admit_rate_per_s = {"tier0": None}
    state.settings.quota_admit_burst = {"tier0": None}
    state.settings.task_delegation_max_depth = max_depth
    state.admit_quota = MagicMock()
    state.admit_quota.try_consume = AsyncMock(
        return_value=Decision(allowed=True, retry_after_s=0.0, tokens_remaining=1.0)
    )

    audit_log: list[dict[str, Any]] = []

    scope = MagicMock()
    scope.lock_task_for_delegation = AsyncMock(return_value=task_row)
    scope.reassign_active_agent = AsyncMock(return_value=True)
    scope.insert_task_event = AsyncMock()
    scope.list_delegation_destinations = AsyncMock(
        return_value=list(prior_destinations or [])
    )

    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn
    state.db_pool = pool

    state._scope = scope
    state._audit_log = audit_log
    state._conn = conn
    return state, audit_log


def _patch_admit_helpers(
    monkeypatch: pytest.MonkeyPatch,
    state: Any,
    *,
    caller_row: Any,
    callee_row: Any,
) -> list[Any]:
    from bp_router import acl as acl_mod
    from bp_router import tasks as tasks_mod
    from bp_router.db import queries as q_mod

    async def _get_agent(_conn: Any, agent_id: str) -> Any:
        return caller_row if agent_id == caller_row.agent_id else callee_row

    monkeypatch.setattr(q_mod, "get_agent", _get_agent)
    monkeypatch.setattr(
        q_mod.Scope, "user", MagicMock(return_value=state._scope)
    )

    async def _session_level(_state: Any, _user_id: str) -> str:
        return "tier0"

    monkeypatch.setattr(tasks_mod, "_session_level", _session_level)
    monkeypatch.setattr(
        acl_mod, "is_allowed_for",
        lambda *a, **k: MagicMock(allow=True, rule_name="ok"),
    )
    monkeypatch.setattr(
        tasks_mod, "is_allowed_for",
        lambda *a, **k: MagicMock(allow=True, rule_name="ok"),
    )

    async def _append_audit(*a, **k) -> None:  # type: ignore[no-untyped-def]
        pass

    monkeypatch.setattr(q_mod, "append_audit_event", _append_audit)

    delivered: list[Any] = []

    async def _deliver_frame(
        _state: Any, agent_id: str, frame: Any, *, await_ack: bool, timeout_s: Any = None
    ) -> Any:
        delivered.append((agent_id, frame))
        return AckFrame(
            agent_id=agent_id,
            trace_id="0" * 32,
            span_id="0" * 16,
            ref_correlation_id="x",
            accepted=True,
        )

    monkeypatch.setattr(tasks_mod, "deliver_frame", _deliver_frame)
    return delivered


# ---------------------------------------------------------------------------
# (1) Cycle detection
# ---------------------------------------------------------------------------


def test_admit_delegation_refuses_cycle_back_to_original_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task created on agt_l0 → delegated to agt_l1. agt_l1 tries
    to delegate back to agt_l0. That's a cycle; refuse before
    dispatching."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    # agt_l1 is now the active executor; the prior delegation moved
    # the task there.
    task_row = _make_task_row(
        agent_id="agt_l0",          # original active
        active_agent_id="agt_l1",   # current active (after one hop)
    )
    state, _ = _delegation_state(
        task_row=task_row,
        prior_destinations=["agt_l1"],
    )

    caller = MagicMock(agent_id="agt_l1", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
    delivered = _patch_admit_helpers(
        monkeypatch, state, caller_row=caller, callee_row=callee,
    )

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state,
                _make_delegation_frame(destination="agt_l0"),
                caller_agent_id="agt_l1",
            )
        )
    assert exc_info.value.code == "delegation_cycle"
    # Dispatcher must NOT have been called — refusal happens before
    # deliver_frame.
    assert delivered == []
    state._scope.reassign_active_agent.assert_not_awaited()


def test_admit_delegation_refuses_cycle_back_to_prior_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A→B→C, then C tries to delegate to B. B has been visited
    already (it's in `prior_destinations`); refuse the cycle."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row(
        agent_id="agt_a",
        active_agent_id="agt_c",
    )
    state, _ = _delegation_state(
        task_row=task_row,
        prior_destinations=["agt_b", "agt_c"],
    )

    caller = MagicMock(agent_id="agt_c", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_b", groups=[], capabilities=[], status="active")
    delivered = _patch_admit_helpers(
        monkeypatch, state, caller_row=caller, callee_row=callee,
    )

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state,
                _make_delegation_frame(destination="agt_b"),
                caller_agent_id="agt_c",
            )
        )
    assert exc_info.value.code == "delegation_cycle"
    assert delivered == []


def test_admit_delegation_allows_forward_chain_to_new_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A→B already happened; B→C is a new agent. Not a cycle;
    permit the delegation through. This pins that the cycle check
    doesn't falsely refuse legitimate forward chains."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row(
        agent_id="agt_a",
        active_agent_id="agt_b",
    )
    state, _ = _delegation_state(
        task_row=task_row,
        prior_destinations=["agt_b"],
    )

    caller = MagicMock(agent_id="agt_b", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_c", groups=[], capabilities=[], status="active")
    delivered = _patch_admit_helpers(
        monkeypatch, state, caller_row=caller, callee_row=callee,
    )

    out = asyncio.run(
        tasks_mod._admit_delegation(
            state,
            _make_delegation_frame(destination="agt_c"),
            caller_agent_id="agt_b",
        )
    )
    assert out == "tsk_x"
    # Dispatch happened.
    assert len(delivered) == 1
    state._scope.reassign_active_agent.assert_awaited_once()


# ---------------------------------------------------------------------------
# (2) Depth cap
# ---------------------------------------------------------------------------


def test_admit_delegation_refuses_when_at_max_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chain has already hit the configured cap. Refuse the next hop
    even if the destination is new (no cycle yet) — pure depth
    runaway protection."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    # Configure a low cap (3) so the test is cheap.
    prior = [f"agt_{i}" for i in range(3)]
    task_row = _make_task_row(
        agent_id="agt_root",
        active_agent_id=prior[-1],
    )
    state, _ = _delegation_state(
        task_row=task_row,
        prior_destinations=prior,
        max_depth=3,
    )

    caller = MagicMock(agent_id=prior[-1], groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_fresh", groups=[], capabilities=[], status="active")
    delivered = _patch_admit_helpers(
        monkeypatch, state, caller_row=caller, callee_row=callee,
    )

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state,
                _make_delegation_frame(destination="agt_fresh"),
                caller_agent_id=prior[-1],
            )
        )
    assert exc_info.value.code == "delegation_depth_exceeded"
    assert "3 times" in exc_info.value.message
    assert delivered == []


def test_admit_delegation_allows_when_under_depth_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At depth N < max, the next hop is permitted. Pins that the
    `>=` boundary check is correct (off-by-one would refuse legit
    chains)."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    # max_depth=5, chain at length 4 → one more hop allowed.
    prior = [f"agt_{i}" for i in range(4)]
    task_row = _make_task_row(
        agent_id="agt_root",
        active_agent_id=prior[-1],
    )
    state, _ = _delegation_state(
        task_row=task_row,
        prior_destinations=prior,
        max_depth=5,
    )

    caller = MagicMock(agent_id=prior[-1], groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_fresh", groups=[], capabilities=[], status="active")
    delivered = _patch_admit_helpers(
        monkeypatch, state, caller_row=caller, callee_row=callee,
    )

    out = asyncio.run(
        tasks_mod._admit_delegation(
            state,
            _make_delegation_frame(destination="agt_fresh"),
            caller_agent_id=prior[-1],
        )
    )
    assert out == "tsk_x"
    assert len(delivered) == 1


# ---------------------------------------------------------------------------
# (3) Source pins
# ---------------------------------------------------------------------------


def test_admit_delegation_source_pin_uses_settings_cap() -> None:
    """Source-pin so a future refactor that drops the settings field
    or hardcodes the cap gets caught."""
    from bp_router import tasks as tasks_mod

    src = inspect.getsource(tasks_mod._admit_delegation)
    assert "task_delegation_max_depth" in src
    assert "list_delegation_destinations" in src
    assert "delegation_cycle" in src
    assert "delegation_depth_exceeded" in src


def test_settings_field_exposes_delegation_cap() -> None:
    """The settings field exists and has the documented bounds."""
    pytest.importorskip("pydantic_settings")
    from pydantic import ValidationError

    from bp_router.settings import Settings

    base = dict(
        db_url="postgresql://x:x@localhost/x",
        public_url="https://example.com",
        jwt_secret="x" * 32,
        redis_url="redis://localhost:6379/0",
        admin_session_secret="y" * 32,
    )

    s = Settings(**base)  # type: ignore[arg-type]
    # Default value.
    assert s.task_delegation_max_depth == 32
    # Lower bound: rejecting 0 protects "at least one delegation allowed".
    with pytest.raises(ValidationError):
        Settings(**base, task_delegation_max_depth=0)  # type: ignore[arg-type]
    # Upper bound: 128 protects the LIMIT in the cycle-history query.
    with pytest.raises(ValidationError):
        Settings(**base, task_delegation_max_depth=129)  # type: ignore[arg-type]
