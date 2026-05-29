"""Tests for the delegation mechanism.

Delegation reuses `NewTaskFrame` (with an existing `task_id`) rather
than introducing a separate `DelegationFrame`. The router validates
the caller against `tasks.active_agent_id`, dispatches to the new
destination, and on the destination's ack atomically reassigns
`active_agent_id`. The destination sees `frame.delegating_agent_id`
set and branches on `ctx.delegating_agent_id` if it cares —
delegation is not a separate handler/registry.

Tests below cover:
  * Wire contract: NewTaskFrame.delegating_agent_id round-trips.
  * SDK routing: ctx.delegating_agent_id wired from frame.
  * SDK dispatch: delegation resolves to the same mode handler
    (no separate registry).
  * Router admit_task: happy path, terminal-task rejection,
    not-active-executor rejection, L1 disconnect rejection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from bp_protocol.frames import AckFrame, NewTaskFrame, parse_frame, serialize_frame
from bp_protocol.types import TaskPriority, TaskState


# Module-level payload model so `from __future__ import annotations`
# deferred-annotation resolution can find it when the handler
# decorators run `typing.get_type_hints`.
class _DelegationPayload(BaseModel):
    msg: str


# ===========================================================================
# Wire contract: NewTaskFrame.delegating_agent_id round-trip
# ===========================================================================


def test_new_task_frame_delegating_agent_id_defaults_none() -> None:
    """Plain spawns leave the field unset on the wire."""
    frame = NewTaskFrame(
        agent_id="agt_caller",
        trace_id="0" * 32,
        span_id="0" * 16,
        destination_agent_id="agt_callee",
        user_id="usr_alice",
        session_id="ses_1",
    )
    assert frame.delegating_agent_id is None


def test_new_task_frame_delegating_agent_id_serialises() -> None:
    """Set field survives a serialize → parse round-trip."""
    frame = NewTaskFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id="tsk_existing",
        destination_agent_id="agt_l1",
        user_id="usr_alice",
        session_id="ses_1",
        delegating_agent_id="agt_l0",
    )
    raw = serialize_frame(frame)
    parsed = parse_frame(raw)
    assert isinstance(parsed, NewTaskFrame)
    assert parsed.delegating_agent_id == "agt_l0"


# ===========================================================================
# SDK: ctx.delegating_agent_id wired from frame
# ===========================================================================


def test_task_context_delegating_agent_id_defaults_none() -> None:
    from bp_sdk.context import CancelToken, TaskContext

    ctx = TaskContext(
        task_id="tsk_x",
        parent_task_id=None,
        user_id="usr_alice",
        user_level="tier0",
        session_id="ses_1",
        trace_id="0" * 32,
        span_id="0" * 16,
        deadline=None,
        cancel_token=CancelToken(),
        log=MagicMock(),
    )
    assert ctx.delegating_agent_id is None


def test_task_context_delegating_agent_id_accepts_value() -> None:
    from bp_sdk.context import CancelToken, TaskContext

    ctx = TaskContext(
        task_id="tsk_x",
        parent_task_id=None,
        user_id="usr_alice",
        user_level="tier0",
        session_id="ses_1",
        trace_id="0" * 32,
        span_id="0" * 16,
        deadline=None,
        cancel_token=CancelToken(),
        log=MagicMock(),
        delegating_agent_id="agt_l0",
    )
    assert ctx.delegating_agent_id == "agt_l0"


# ===========================================================================
# SDK: delegation is context-only (no separate handler/registry)
# ===========================================================================


def test_delegation_uses_same_mode_handler_no_separate_registry() -> None:
    """Delegation is NOT a routing axis any more. A frame carrying
    `delegating_agent_id` resolves to the SAME mode handler as a
    plain spawn — there is no `@on_delegation` decorator/registry.
    Delegation-aware behaviour is read off `ctx.delegating_agent_id`
    inside the handler."""
    pytest.importorskip("fastapi")

    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.dispatch import Dispatcher
    from bp_sdk.transport.inproc import InProcessTransport

    agent = Agent(info=AgentInfo(agent_id="agt_l1", description="t"))
    assert not hasattr(agent, "on_delegation")

    @agent.handler
    async def regular(ctx: Any, payload: _DelegationPayload) -> None:
        pass

    transport = InProcessTransport()
    transport.attach(inbound=asyncio.Queue(), outbound=asyncio.Queue())
    dispatcher = Dispatcher(agent, transport)

    plain = NewTaskFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id="tsk_x",
        destination_agent_id="agt_l1",
        user_id="usr_alice",
        session_id="ses_1",
        payload={"msg": "hi"},
    )
    assert dispatcher._resolve_handler_for(plain).fn is regular

    delegation = plain.model_copy(update={"delegating_agent_id": "agt_l0"})
    # Same handler — delegation only changes ctx.delegating_agent_id.
    assert dispatcher._resolve_handler_for(delegation).fn is regular


def test_on_delegation_falls_back_to_handler_when_unregistered() -> None:
    """If no `@on_delegation` is registered for the payload type, the
    dispatcher uses the regular `@handler`; user code reads
    `ctx.delegating_agent_id` to branch."""
    pytest.importorskip("fastapi")

    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.dispatch import Dispatcher
    from bp_sdk.transport.inproc import InProcessTransport

    agent = Agent(info=AgentInfo(agent_id="agt_l1", description="t"))

    @agent.handler
    async def only_handler(ctx: Any, payload: _DelegationPayload) -> None:
        pass

    transport = InProcessTransport()
    transport.attach(inbound=asyncio.Queue(), outbound=asyncio.Queue())
    dispatcher = Dispatcher(agent, transport)

    delegation = NewTaskFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id="tsk_x",
        destination_agent_id="agt_l1",
        user_id="usr_alice",
        session_id="ses_1",
        payload={"msg": "hi"},
        delegating_agent_id="agt_l0",
    )
    picked = dispatcher._resolve_handler_for(delegation)
    assert picked is not None
    assert picked.fn is only_handler


# ===========================================================================
# Router: _admit_delegation
# ===========================================================================


def _delegation_state(
    *,
    task_row: Any,
    ack: AckFrame | None,
    ack_raises: Exception | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Build a state mock that satisfies _admit_delegation.

    `task_row` is the row returned by `lock_task_for_delegation`.
    `ack` (or `ack_raises`) controls what L1's ack looks like.
    Returns (state, audit_log) — caller asserts on audit_log entries.
    """
    from bp_router.security.rate_limit import Decision  # noqa: PLC0415

    state = MagicMock()
    state.settings = MagicMock()
    state.settings.pending_ack_timeout_s = 5.0
    state.settings.quota_admit_rate_per_s = {"tier0": None}
    state.settings.quota_admit_burst = {"tier0": None}
    state.settings.task_delegation_max_depth = 32
    state.admit_quota = MagicMock()
    state.admit_quota.try_consume = AsyncMock(
        return_value=Decision(allowed=True, retry_after_s=0.0, tokens_remaining=1.0)
    )

    audit_log: list[dict[str, Any]] = []

    scope = MagicMock()
    scope.lock_task_for_delegation = AsyncMock(return_value=task_row)
    scope.reassign_active_agent = AsyncMock(return_value=True)
    scope.insert_task_event = AsyncMock()
    scope.list_delegation_destinations = AsyncMock(return_value=[])

    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn
    state.db_pool = pool

    # Patchable helpers — the real implementations get monkey-patched
    # in each test via monkeypatch.setattr.
    state._scope = scope
    state._audit_log = audit_log
    state._conn = conn
    return state, audit_log


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


def _patch_admit_helpers(
    monkeypatch: pytest.MonkeyPatch,
    state: Any,
    *,
    caller_row: Any,
    callee_row: Any,
    deliver_result: Any = "use_default",
    deliver_raises: Exception | None = None,
) -> list[Any]:
    """Patch `queries.get_agent`, `queries.Scope.user`, `_session_level`,
    `is_allowed_for`, `append_audit_event`, and `deliver_frame` to
    plug into the state's audit_log / scope mock.

    Returns the list of delivered (agent_id, frame) tuples so tests
    can assert on what the router sent.
    """
    from bp_router import tasks as tasks_mod
    from bp_router.db import queries as q_mod

    async def _get_agent(conn: Any, agent_id: str) -> Any:
        return caller_row if agent_id == caller_row.agent_id else callee_row

    monkeypatch.setattr(q_mod, "get_agent", _get_agent)
    monkeypatch.setattr(
        q_mod.Scope, "user", MagicMock(return_value=state._scope)
    )

    async def _session_level(_state: Any, _user_id: str) -> str:
        return "tier0"

    monkeypatch.setattr(tasks_mod, "_session_level", _session_level)

    from bp_router import acl as acl_mod

    monkeypatch.setattr(
        acl_mod,
        "is_allowed_for",
        lambda *a, **k: MagicMock(allow=True, rule_name="ok"),
    )
    monkeypatch.setattr(
        tasks_mod,
        "is_allowed_for",
        lambda *a, **k: MagicMock(allow=True, rule_name="ok"),
    )

    async def _append_audit(
        _conn: Any,
        *,
        actor_kind: str,
        actor_id: Any,
        event: str,
        target_kind: Any = None,
        target_id: Any = None,
        payload: Any = None,
    ) -> None:
        state._audit_log.append({
            "actor_kind": actor_kind,
            "actor_id": actor_id,
            "event": event,
            "target_id": target_id,
            "payload": payload,
        })

    monkeypatch.setattr(q_mod, "append_audit_event", _append_audit)

    delivered: list[Any] = []

    if deliver_result == "use_default":
        deliver_result = AckFrame(
            agent_id="agt_l1",
            trace_id="0" * 32,
            span_id="0" * 16,
            ref_correlation_id="x",
            accepted=True,
        )

    async def _deliver_frame(
        _state: Any, agent_id: str, frame: Any, *, await_ack: bool, timeout_s: Any = None
    ) -> Any:
        delivered.append((agent_id, frame))
        if deliver_raises is not None:
            raise deliver_raises
        return deliver_result

    monkeypatch.setattr(tasks_mod, "deliver_frame", _deliver_frame)
    return delivered


def test_admit_delegation_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller is the active executor, L1 accepts → flip
    active_agent_id + emit task.delegated audit + task_event."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row(active_agent_id="agt_l0")
    state, audit_log = _delegation_state(task_row=task_row, ack=None)

    caller = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_l1", groups=[], capabilities=[], status="active")
    delivered = _patch_admit_helpers(monkeypatch, state, caller_row=caller, callee_row=callee)

    out = asyncio.run(
        tasks_mod._admit_delegation(
            state, _make_delegation_frame(), caller_agent_id="agt_l0"
        )
    )
    assert out == "tsk_x"

    # Outbound frame stamped with delegating_agent_id.
    assert len(delivered) == 1
    agent_id, frame = delivered[0]
    assert agent_id == "agt_l1"
    assert isinstance(frame, NewTaskFrame)
    assert frame.delegating_agent_id == "agt_l0"
    assert frame.task_id == "tsk_x"

    # Atomic flip ran.
    state._scope.reassign_active_agent.assert_awaited_once()
    flip_kwargs = state._scope.reassign_active_agent.await_args.kwargs
    assert flip_kwargs["new_active_agent_id"] == "agt_l1"
    assert flip_kwargs["expected_current_agent_id"] == "agt_l0"

    # Audit row.
    delegated_events = [e for e in audit_log if e["event"] == "task.delegated"]
    assert len(delegated_events) == 1
    assert delegated_events[0]["payload"] == {"from": "agt_l0", "to": "agt_l1"}


def test_admit_delegation_rejects_when_caller_not_active_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the current active_agent_id can delegate. A caller that
    isn't the active executor gets `not_active_executor`."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row(active_agent_id="agt_l1")  # someone else is active
    state, _audit_log = _delegation_state(task_row=task_row, ack=None)

    caller = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_l2", groups=[], capabilities=[], status="active")
    _patch_admit_helpers(monkeypatch, state, caller_row=caller, callee_row=callee)

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state,
                _make_delegation_frame(destination="agt_l2"),
                caller_agent_id="agt_l0",
            )
        )
    assert exc_info.value.code == "not_active_executor"
    state._scope.reassign_active_agent.assert_not_awaited()


def test_admit_delegation_rejects_when_task_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row(state=TaskState.SUCCEEDED, active_agent_id="agt_l0")
    state, _ = _delegation_state(task_row=task_row, ack=None)

    caller = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_l1", groups=[], capabilities=[], status="active")
    _patch_admit_helpers(monkeypatch, state, caller_row=caller, callee_row=callee)

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state, _make_delegation_frame(), caller_agent_id="agt_l0"
            )
        )
    assert exc_info.value.code == "task_terminal"


def test_admit_delegation_no_flip_when_l1_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the destination has no live socket, the active executor
    must NOT change — L0 retains the task."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod
    from bp_router.delivery import AgentNotConnected

    task_row = _make_task_row(active_agent_id="agt_l0")
    state, _ = _delegation_state(task_row=task_row, ack=None)

    caller = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_l1", groups=[], capabilities=[], status="active")
    _patch_admit_helpers(
        monkeypatch,
        state,
        caller_row=caller,
        callee_row=callee,
        deliver_raises=AgentNotConnected("agt_l1"),
    )

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state, _make_delegation_frame(), caller_agent_id="agt_l0"
            )
        )
    assert exc_info.value.code == "agent_disconnected"
    state._scope.reassign_active_agent.assert_not_awaited()


def test_admit_delegation_no_flip_when_l1_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If L1 sends Ack(accepted=False), the active executor must
    NOT change. L0 retains the task; the caller's `peers.delegate()`
    raises SpawnRejected."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    task_row = _make_task_row(active_agent_id="agt_l0")
    state, _ = _delegation_state(task_row=task_row, ack=None)

    caller = MagicMock(agent_id="agt_l0", groups=[], capabilities=[], status="active")
    callee = MagicMock(agent_id="agt_l1", groups=[], capabilities=[], status="active")
    reject_ack = AckFrame(
        agent_id="agt_l1",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="x",
        accepted=False,
        reason="too busy",
    )
    _patch_admit_helpers(
        monkeypatch,
        state,
        caller_row=caller,
        callee_row=callee,
        deliver_result=reject_ack,
    )

    with pytest.raises(tasks_mod.AdmitError) as exc_info:
        asyncio.run(
            tasks_mod._admit_delegation(
                state, _make_delegation_frame(), caller_agent_id="agt_l0"
            )
        )
    assert exc_info.value.code == "rejected"
    assert "too busy" in str(exc_info.value)
    state._scope.reassign_active_agent.assert_not_awaited()


# ===========================================================================
# Wire-level: admit_task routes to delegation branch when task_id is set
# ===========================================================================


def test_admit_task_routes_to_delegation_branch_when_task_id_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """admit_task with frame.task_id set delegates to
    `_admit_delegation` without touching the spawn / idempotency
    paths."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    sentinel = {"task_id": "tsk_x"}

    async def _stub_delegation(state: Any, frame: Any, *, caller_agent_id: str) -> str:
        sentinel["called_with_task_id"] = frame.task_id
        sentinel["called_with_caller"] = caller_agent_id
        return frame.task_id

    monkeypatch.setattr(tasks_mod, "_admit_delegation", _stub_delegation)

    state = MagicMock()
    frame = _make_delegation_frame()
    out = asyncio.run(tasks_mod.admit_task(state, frame, caller_agent_id="agt_l0"))
    # R9: admit_task returns AdmitResult; the delegation branch wraps
    # the str task_id with replay_result=None.
    assert out.task_id == "tsk_x"
    assert out.replay_result is None
    assert sentinel["called_with_task_id"] == "tsk_x"
    assert sentinel["called_with_caller"] == "agt_l0"


def test_admit_task_skips_delegation_branch_for_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain spawn (task_id is None) does NOT call _admit_delegation."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_mod

    called = []

    async def _stub_delegation(*args: Any, **kwargs: Any) -> str:
        called.append(True)
        return "should_not_run"

    monkeypatch.setattr(tasks_mod, "_admit_delegation", _stub_delegation)

    # Spawn path will trip later in admit_task (no real state); we
    # only care that _admit_delegation isn't called.
    spawn_frame = NewTaskFrame(
        agent_id="agt_l0",
        trace_id="0" * 32,
        span_id="0" * 16,
        destination_agent_id="agt_l1",
        user_id="usr_alice",
        session_id="ses_1",
        # task_id stays None — this is a spawn.
    )
    state = MagicMock()
    state.db_pool = MagicMock()
    pool = state.db_pool
    pool.acquire.return_value.__aenter__ = AsyncMock(
        side_effect=Exception("spawn path acquired pool")
    )
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # Just confirm _admit_delegation wasn't called even when the spawn
    # path errors out.
    with pytest.raises(Exception):
        asyncio.run(tasks_mod.admit_task(state, spawn_frame, caller_agent_id="agt_l0"))
    assert called == []
