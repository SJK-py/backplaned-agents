"""Tests for the cross-user cancel authorisation bug.

The original `_handle_cancel` looked up the task's user_id from DB
and used it to call `cancel_task` — without checking that the calling
agent had any relationship to that task. An agent A1 (any user) could
cancel any other user's task tree by sending `Cancel{task_id=X}`.

Fix: walk the task's ancestor chain and require the calling agent to
appear as the assignee of the task itself or one of its ancestors.

These tests exercise the recursive-CTE helper directly. The dispatch
handler integration is covered by mocking the helper.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _cancel_frame(**kwargs: Any) -> Any:
    """CancelFrame with the required `_FrameBase` fields filled in."""
    from bp_protocol.frames import CancelFrame

    base = {
        "type": "Cancel",
        "trace_id": "trc_test",
        "span_id": "spn_test",
        "agent_id": "agt_test",
    }
    base.update(kwargs)
    return CancelFrame(**base)


# ---------------------------------------------------------------------------
# task_has_ancestor_with_agent — recursive CTE returns True only for
# the task itself or a true ancestor's agent.
# ---------------------------------------------------------------------------


class _StubConn:
    """Minimal asyncpg-shaped stub that records the SQL it executes
    and returns a pre-staged row."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.last_query: str = ""
        self.last_args: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.last_query = query
        self.last_args = args
        return self._response


def test_helper_returns_true_when_match_found() -> None:
    from bp_router.db.queries import task_has_ancestor_with_agent

    conn = _StubConn(response={"?column?": 1})
    out = asyncio.run(
        task_has_ancestor_with_agent(
            conn,  # type: ignore[arg-type]
            task_id="tsk_abc",
            agent_id="agt_alice",
            user_id="usr_owner",
        )
    )
    assert out is True
    # Verify the recursive CTE shape (anchor + recursive) plus the
    # new `user_id` scoping + depth bound from review item DB-H3.
    assert "WITH RECURSIVE" in conn.last_query
    assert "ancestors" in conn.last_query
    assert "user_id = $3" in conn.last_query
    assert "_depth" in conn.last_query
    # Bound parameters (no interpolation — SQL injection guard).
    # The fourth arg is the depth bound; just verify the leading
    # three and that the fourth is a positive int cap.
    assert conn.last_args[:3] == ("tsk_abc", "agt_alice", "usr_owner")
    assert isinstance(conn.last_args[3], int) and conn.last_args[3] > 0


def test_helper_returns_false_when_no_match() -> None:
    from bp_router.db.queries import task_has_ancestor_with_agent

    conn = _StubConn(response=None)
    out = asyncio.run(
        task_has_ancestor_with_agent(
            conn,  # type: ignore[arg-type]
            task_id="tsk_abc",
            agent_id="agt_bob",
            user_id="usr_owner",
        )
    )
    assert out is False


def test_helper_user_id_scope_prevents_cross_user_walk() -> None:
    """DB-H3 finding: even if a malformed `parent_task_id` chain
    crosses user boundaries (which review item C1 also blocks at
    insert time), the recursive walk must filter by `user_id` at
    every step. The CTE's anchor and recursive arms both include
    `WHERE user_id = $3` — verify that's hard-wired so a future
    refactor can't drop the scope and silently authorise a
    cross-user cancel."""
    import inspect

    from bp_router.db import queries

    src = inspect.getsource(queries.task_has_ancestor_with_agent)
    # Anchor and recursive UNION arms both filter by user_id.
    assert src.count("user_id = $3") >= 2
    # And the depth cap is wired into the recursive arm.
    assert "_depth + 1" in src
    assert "_depth <" in src


# ---------------------------------------------------------------------------
# _handle_cancel: dropped silently when the agent is not authorised
# ---------------------------------------------------------------------------


def _make_state(*, task_user_id: str, authorised: bool) -> Any:
    """Build a stub `state` whose db_pool returns `task_user_id` for
    the SELECT, and patches `task_has_ancestor_with_agent` to return
    `authorised`."""
    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"user_id": task_user_id})
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return state, conn


def test_unauthorised_cancel_does_not_call_cancel_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent A1 (user U1) sending Cancel{task_id=X} where X belongs
    to U2 must NOT cause cancel_task to run."""
    from bp_router import dispatch
    from bp_router.db import queries

    # Stub: agent_id "agt_attacker" is NOT in any ancestor of the target.
    monkeypatch.setattr(
        queries,
        "task_has_ancestor_with_agent",
        AsyncMock(return_value=False),
    )

    cancel_task_mock = AsyncMock()
    monkeypatch.setattr("bp_router.tasks.cancel_task", cancel_task_mock)

    state, _ = _make_state(task_user_id="usr_victim", authorised=False)
    entry = MagicMock()
    entry.agent_id = "agt_attacker"

    frame = _cancel_frame(task_id="tsk_target", reason="malicious")

    asyncio.run(dispatch._handle_cancel(state, entry, frame))

    cancel_task_mock.assert_not_called()


def test_authorised_cancel_propagates_to_cancel_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router import dispatch
    from bp_router.db import queries

    ancestor_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(queries, "task_has_ancestor_with_agent", ancestor_mock)
    cancel_task_mock = AsyncMock(return_value=1)
    monkeypatch.setattr("bp_router.tasks.cancel_task", cancel_task_mock)

    state, _ = _make_state(task_user_id="usr_owner", authorised=True)
    entry = MagicMock()
    entry.agent_id = "agt_owner"

    frame = _cancel_frame(task_id="tsk_owned", reason="user_aborted")

    asyncio.run(dispatch._handle_cancel(state, entry, frame))

    cancel_task_mock.assert_awaited_once()
    # The user_id passed in MUST come from the looked-up task row,
    # not from the agent (the agent is just the initiator).
    kwargs = cancel_task_mock.call_args.kwargs
    assert kwargs["user_id"] == "usr_owner"
    assert kwargs["initiator"] == "agt_owner"
    # DB-H3: the ancestor walk is now scoped by user_id. Verify the
    # call site passes the task's user_id (NOT the calling agent's).
    # Without this, a cross-user `parent_task_id` chain could let a
    # malicious agent's tree appear as an ancestor.
    ancestor_kwargs = ancestor_mock.call_args.kwargs
    assert ancestor_kwargs["user_id"] == "usr_owner"
    assert ancestor_kwargs["agent_id"] == "agt_owner"


def test_unknown_task_id_drops_silently_without_querying_authz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the task doesn't exist, we return early before doing the
    ancestor walk. (Avoids a probe that an agent could time to detect
    presence vs. absence of arbitrary task IDs.)"""
    from bp_router import dispatch
    from bp_router.db import queries

    ancestor_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(queries, "task_has_ancestor_with_agent", ancestor_mock)
    cancel_task_mock = AsyncMock()
    monkeypatch.setattr("bp_router.tasks.cancel_task", cancel_task_mock)

    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)  # task not found
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    entry = MagicMock()
    entry.agent_id = "agt_anyone"

    frame = _cancel_frame(task_id="tsk_does_not_exist", reason="probe")
    asyncio.run(dispatch._handle_cancel(state, entry, frame))

    ancestor_mock.assert_not_awaited()
    cancel_task_mock.assert_not_called()


def test_llm_call_abort_path_does_not_check_task_authz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM-abort mode (frame.ref_correlation_id) operates on the
    socket's own llm_tasks dict — only the agent's own in-flight
    requests are reachable. No task-tree authorisation needed."""
    from bp_router import dispatch
    from bp_router.db import queries

    ancestor_mock = AsyncMock()
    monkeypatch.setattr(queries, "task_has_ancestor_with_agent", ancestor_mock)

    inflight = MagicMock()
    inflight.done.return_value = False
    inflight.cancel = MagicMock()

    entry = MagicMock()
    entry.agent_id = "agt_owner"
    entry.llm_tasks = {"corr_42": inflight}

    state = MagicMock()
    frame = _cancel_frame(ref_correlation_id="corr_42")

    asyncio.run(dispatch._handle_cancel(state, entry, frame))

    inflight.cancel.assert_called_once()
    ancestor_mock.assert_not_awaited()
