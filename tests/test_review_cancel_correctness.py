"""Tests for the cancellation-correctness review fixes (WS-H5, SDK-H2).

WS-H5 — On user cancel that races with the agent's natural Result:
  - cancel-wins → `cancel_task` transitions + emits synthetic
    Result(CANCELLED) to the parent. Agent's late Result is
    dropped by `complete_task` on `IllegalTransition`. Parent
    sees exactly ONE terminal frame.
  - agent-wins → `complete_task` transitions + emits the
    legitimate Result. `cancel_task`'s transition raises
    `IllegalTransition` and skips the synthetic emit. Parent
    sees exactly ONE terminal frame.

SDK-H2 — Cascading cancel correctness:
  - The router's `cancel_task` already cascades to descendants
    via `list_descendants` (PR #61).
  - PR #64's `_drain_task_correlations` already rejects the
    cancelled handler's pending peer-call / LLM futures so
    callers fail fast rather than waiting out
    `correlation_timeout`.
  Both halves of SDK-H2 are addressed by prior PRs; this test
  pins the contract end-to-end so a future regression in either
  surface is caught.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# WS-H5: cancel_task emits synthetic Result(CANCELLED) to parent
# ===========================================================================


def test_cancel_task_emits_result_to_parent_on_transition_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `task_transition` succeeds (cancel won the race),
    `cancel_task` must fan a Result(CANCELLED) to the parent agent
    so its `peers.spawn(...)` future resolves immediately. Without
    this fan-out the parent's await would hang until
    `correlation_timeout`."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    delivered: list[Any] = []

    async def _deliver(state: Any, agent_id: str, frame: Any, *, await_ack: bool) -> None:
        delivered.append((agent_id, frame))

    monkeypatch.setattr(tasks, "deliver_frame", _deliver)

    async def _transition_ok(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    monkeypatch.setattr(tasks, "task_transition", _transition_ok)

    state = _make_cancel_state(
        monkeypatch,
        descendants=[],
        owner_for={
            "tsk_x": {
                "agent_id": "agt_child",
                "caller_agent_id": "agt_parent",
                "parent_task_id": "tsk_parent",
            },
            "tsk_parent": {
                "agent_id": "agt_parent",
                "caller_agent_id": "agt_channel",
                "parent_task_id": None,
            },
        },
    )

    out = asyncio.run(tasks.cancel_task(
        state, "tsk_x", user_id="usr_alice", reason="user_aborted",
    ))
    assert out == 1

    delivered_kinds = [
        (agent, type(frame).__name__) for agent, frame in delivered
    ]
    # Synthetic Result(CANCELLED) goes to the caller (parent's agent
    # for this child); CancelFrame goes to the executor.
    assert ("agt_parent", "ResultFrame") in delivered_kinds
    assert ("agt_child", "CancelFrame") in delivered_kinds

    parent_results = [
        f for a, f in delivered
        if a == "agt_parent" and type(f).__name__ == "ResultFrame"
    ]
    assert len(parent_results) == 1
    rf = parent_results[0]
    from bp_protocol.types import TaskStatus
    assert rf.status == TaskStatus.CANCELLED
    assert rf.status_code == 499
    assert rf.task_id == "tsk_x"
    assert rf.parent_task_id == "tsk_parent"


def test_cancel_task_skips_result_when_transition_loses_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race scenario: agent's Result(SUCCEEDED) won — `complete_task`
    already transitioned to SUCCEEDED and fanned its Result. When
    `cancel_task` then runs, `task_transition` raises
    `IllegalTransition`, the loop `continue`s, and the synthetic
    Result(CANCELLED) MUST NOT be emitted (else the parent gets
    two terminal frames)."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import TaskState
    from bp_router import tasks
    from bp_router.state import IllegalTransition

    delivered: list[Any] = []

    async def _deliver(state: Any, agent_id: str, frame: Any, *, await_ack: bool) -> None:
        delivered.append((agent_id, frame))

    monkeypatch.setattr(tasks, "deliver_frame", _deliver)

    async def _transition_illegal(*args: Any, **kwargs: Any) -> Any:
        raise IllegalTransition(
            args[1] if len(args) > 1 else "tsk_x",
            TaskState.SUCCEEDED,
            TaskState.CANCELLED,
        )

    monkeypatch.setattr(tasks, "task_transition", _transition_illegal)

    state = _make_cancel_state(
        monkeypatch,
        descendants=[],
        owner_for={
            "tsk_x": {"agent_id": "agt_child", "parent_task_id": "tsk_parent"},
            "tsk_parent": {"agent_id": "agt_parent", "parent_task_id": None},
        },
    )

    out = asyncio.run(tasks.cancel_task(
        state, "tsk_x", user_id="usr_alice",
    ))
    assert out == 0
    # No Result fanned — agent's legitimate one is the only one
    # the parent should see (via complete_task, not exercised here).
    # No CancelFrame either: the task is already terminal.
    assert delivered == []


def test_cancel_task_top_level_fans_result_to_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a top-level task fans a synthetic Result(CANCELLED)
    to its caller (a channel agent for root tasks) plus a CancelFrame
    to the executor. The caller is always a real agent under the
    caller_agent_id model, so root tasks no longer drop their
    terminal Result on the floor."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    delivered: list[Any] = []

    async def _deliver(state: Any, agent_id: str, frame: Any, *, await_ack: bool) -> None:
        delivered.append((agent_id, frame))

    monkeypatch.setattr(tasks, "deliver_frame", _deliver)

    async def _transition_ok(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    monkeypatch.setattr(tasks, "task_transition", _transition_ok)

    state = _make_cancel_state(
        monkeypatch,
        descendants=[],
        owner_for={
            "tsk_root": {
                "agent_id": "agt_owner",
                "caller_agent_id": "agt_channel",
                "parent_task_id": None,
            },
        },
    )

    out = asyncio.run(tasks.cancel_task(
        state, "tsk_root", user_id="usr_alice",
    ))
    assert out == 1
    delivered_kinds = [(a, type(f).__name__) for a, f in delivered]
    assert ("agt_channel", "ResultFrame") in delivered_kinds
    assert ("agt_owner", "CancelFrame") in delivered_kinds


def test_cancel_task_cascades_to_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the recursive cancel: `list_descendants` returns
    children, and each gets transitioned + Result emitted to ITS
    parent + CancelFrame to ITS agent. SDK-H2's cascade contract."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    delivered: list[Any] = []

    async def _deliver(state: Any, agent_id: str, frame: Any, *, await_ack: bool) -> None:
        delivered.append((agent_id, frame))

    monkeypatch.setattr(tasks, "deliver_frame", _deliver)

    async def _transition_ok(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    monkeypatch.setattr(tasks, "task_transition", _transition_ok)

    descendant_rows = [
        MagicMock(task_id="tsk_child1"),
        MagicMock(task_id="tsk_grandchild"),
    ]
    state = _make_cancel_state(
        monkeypatch,
        descendants=descendant_rows,
        owner_for={
            "tsk_root": {
                "agent_id": "agt_root",
                "caller_agent_id": "agt_channel",
                "parent_task_id": None,
            },
            "tsk_child1": {
                "agent_id": "agt_c1",
                "caller_agent_id": "agt_root",
                "parent_task_id": "tsk_root",
            },
            "tsk_grandchild": {
                "agent_id": "agt_gc",
                "caller_agent_id": "agt_c1",
                "parent_task_id": "tsk_child1",
            },
        },
    )

    out = asyncio.run(tasks.cancel_task(
        state, "tsk_root", user_id="usr_alice",
    ))
    assert out == 3

    # Synthetic Result(CANCELLED) goes to the caller for every level
    # of the tree — including the root (caller is the channel agent).
    cancel_results = [
        (a, f.task_id, f.parent_task_id)
        for a, f in delivered
        if type(f).__name__ == "ResultFrame"
    ]
    assert ("agt_channel", "tsk_root", None) in cancel_results
    assert ("agt_root", "tsk_child1", "tsk_root") in cancel_results
    assert ("agt_c1", "tsk_grandchild", "tsk_child1") in cancel_results

    cancel_targets = [
        (a, f.task_id) for a, f in delivered
        if type(f).__name__ == "CancelFrame"
    ]
    assert ("agt_root", "tsk_root") in cancel_targets
    assert ("agt_c1", "tsk_child1") in cancel_targets
    assert ("agt_gc", "tsk_grandchild") in cancel_targets


def test_cancel_task_offline_parent_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the parent agent is disconnected when the synthetic
    Result is fanned out, log + drop. Don't raise — the cancel
    itself succeeded; the parent will pick up state on
    reconnect via the recv-loop replay."""
    pytest.importorskip("fastapi")
    from bp_router import tasks
    from bp_router.delivery import AgentNotConnected

    async def _deliver_offline(state: Any, agent_id: str, frame: Any, *, await_ack: bool) -> None:
        if agent_id == "agt_parent":
            raise AgentNotConnected(agent_id)

    monkeypatch.setattr(tasks, "deliver_frame", _deliver_offline)

    async def _transition_ok(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    monkeypatch.setattr(tasks, "task_transition", _transition_ok)

    state = _make_cancel_state(
        monkeypatch,
        descendants=[],
        owner_for={
            "tsk_x": {"agent_id": "agt_child", "parent_task_id": "tsk_parent"},
            "tsk_parent": {"agent_id": "agt_parent", "parent_task_id": None},
        },
    )

    # Should not raise.
    out = asyncio.run(tasks.cancel_task(state, "tsk_x", user_id="usr_alice"))
    assert out == 1


def test_cancel_task_source_documents_atomicity_contract() -> None:
    """Source-level pin: the docstring must explicitly call out
    the "exactly one terminal frame" guarantee, and the
    IllegalTransition branch must `continue` BEFORE reaching the
    parent-fanout section."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.cancel_task)
    assert "exactly one terminal frame" in src
    illegal_idx = src.index("except IllegalTransition")
    fanout_idx = src.index('status=TaskStatus.CANCELLED')
    # The IllegalTransition continue MUST appear BEFORE the
    # CANCELLED Result construction in source order.
    assert illegal_idx < fanout_idx
    after_illegal = src[illegal_idx:fanout_idx]
    assert "continue" in after_illegal


# ===========================================================================
# SDK-H2: drain interaction with cancel
# ===========================================================================


def test_sdk_handle_cancel_triggers_handler_drain_via_cancel_token() -> None:
    """SDK-H2 contract end-to-end: when the SDK's `_handle_cancel`
    trips the cancel_token, the handler's `_run_handler` finally
    block calls `_drain_task_correlations`, which rejects every
    pending peer-call / LLM future for that task with
    `HandlerExited`. Caller's awaits unblock immediately rather
    than waiting out `correlation_timeout`.

    PR #64 wired the drain. PR #61 added router-side cascade. This
    test pins the source contract that both halves of SDK-H2 are
    addressed by the existing infrastructure."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    # `_run_handler.finally` calls drain.
    handler_src = inspect.getsource(dispatch.Dispatcher._run_handler)
    assert "_drain_task_correlations" in handler_src
    assert "HandlerExited" in handler_src

    # `_handle_cancel` trips the cancel_token (which causes the
    # handler to raise CancellationError, hitting the finally).
    cancel_src = inspect.getsource(dispatch.Dispatcher._handle_cancel)
    assert "cancel_token.trip" in cancel_src


def test_router_cancel_cascades_via_list_descendants() -> None:
    """Pin the router-side cascade: `cancel_task` walks descendants
    via `list_descendants` (introduced PR #61) and processes each
    one. SDK-H2's "child agents continue running" concern is
    addressed by this cascade."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.cancel_task)
    assert "list_descendants" in src
    assert "for tid in targets" in src


# ===========================================================================
# Helpers
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_scope_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests below that override `queries.Scope.user` don't
    leak the patched classmethod into adjacent test files. Each
    test in this module monkeypatches Scope.user via the helper
    below; the autouse fixture guarantees `monkeypatch.undo()` on
    teardown restores the original.
    """
    # No-op body — `monkeypatch` itself does the cleanup at
    # function exit. Just owning the fixture is enough.
    return None


def _make_cancel_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    descendants: list[Any],
    owner_for: dict[str, dict[str, Any]],
) -> Any:
    """Build a fake `state` for cancel_task. `owner_for` maps
    task_id -> {agent_id, parent_task_id} so the SELECTs after
    transition return the right owner row.

    Uses `monkeypatch.setattr` for `queries.Scope.user` so the
    classmethod override is automatically reverted at test exit
    (avoids pollution into other test files)."""
    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool

    scope = MagicMock()
    scope.list_descendants = AsyncMock(return_value=descendants)

    from bp_router.db import queries as queries_module
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    async def _fetchrow(query: str, *args: Any) -> Any:
        if "FROM tasks WHERE task_id = $1" in query:
            tid = args[0]
            entry = owner_for.get(tid)
            if entry is None:
                return None
            return {
                "agent_id": entry["agent_id"],
                "active_agent_id": entry.get("active_agent_id", entry["agent_id"]),
                "caller_agent_id": entry.get("caller_agent_id", entry.get("parent_agent_id", entry["agent_id"])),
                "parent_task_id": entry["parent_task_id"],
                "user_id": "usr_alice",
                "state": "running",
            }
        return None

    conn.fetchrow = _fetchrow
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    monkeypatch.setattr(
        queries_module.Scope, "user", MagicMock(return_value=scope)
    )
    return state
