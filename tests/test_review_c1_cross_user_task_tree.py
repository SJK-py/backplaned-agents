"""Tests for the C1-cluster review fixes — cross-user task-tree integrity.

Bundles the fixes from the post-merge full-codebase review:

  - **C1**: `Scope.create_task` previously accepted a `parent_task_id`
    that exists but belongs to another user, silently misrooting the
    new task and leaving an orphan FK pointer. Now raises
    `CrossUserTaskAccess`, which `admit_task` translates to
    `AdmitError("invalid_parent_task", ...)`.
  - **DB-H3**: `task_has_ancestor_with_agent` now scopes by `user_id`
    and bounds the recursive walk by depth (`_MAX_TASK_TREE_DEPTH`).
    Covered in `test_security_cancel_authz.py` — see
    `test_helper_user_id_scope_prevents_cross_user_walk`.
  - **DB-M1**: `list_descendants` recursive CTE now has a depth bound
    so a malformed cycle in `parent_task_id` (FK only checks
    existence, not acyclicity) can't pin Postgres work-mem looping.
  - **DB-M2**: `insert_task_event` now enforces ownership via
    `INSERT ... SELECT ... WHERE EXISTS`. Misuse (or future caller
    passing a user-supplied task_id) writes nothing and raises
    `CrossUserTaskAccess` instead of silently audit-logging under
    another user's task.

Tests use stub connections (no live DB) — the same pattern as
`test_security_cancel_authz.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import Any

import pytest

from bp_protocol.types import TaskPriority

# ---------------------------------------------------------------------------
# Stub asyncpg-shaped connection
# ---------------------------------------------------------------------------


class _StubConn:
    """Records the SQL it executes and returns canned responses.

    `responses` is consumed FIFO — one per `fetchrow` / `fetch` call.
    Each response can be a value (returned as-is) or an Exception
    (raised). A None response simulates "no row matched".
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return self._next("fetchrow", query, args)

    async def fetch(self, query: str, *args: Any) -> Any:
        return self._next("fetch", query, args)

    def _next(self, op: str, query: str, args: tuple[Any, ...]) -> Any:
        self.calls.append((op, query, args))
        if not self._responses:
            raise AssertionError(
                f"unexpected {op} #{len(self.calls)}: no canned response"
            )
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


# ===========================================================================
# C1 — Scope.create_task rejects cross-user / missing parent_task_id
# ===========================================================================


def test_create_task_with_no_parent_succeeds_and_self_roots() -> None:
    """Sanity baseline: when `parent_task_id is None`, create_task
    inserts a row with `root_task_id = task_id` (self-root). C1 fix
    must not regress the no-parent path."""
    from bp_router.db import queries
    from bp_router.db.models import TaskRow

    inserted_args: list[tuple[Any, ...]] = []

    class _C(_StubConn):
        async def fetchrow(self, query: str, *args: Any) -> Any:
            inserted_args.append(args)
            # Return a synthetic row matching what INSERT...RETURNING
            # would produce. Argument indices follow the order in
            # `Scope.create_task`:
            #   $1..$6 = task_id, parent, root, user, session, agent
            #   $7..$8 = caller_agent_id, active_agent_id
            #   $9..$14 = state, idempotency, priority, deadline,
            #             created_at, input
            return {
                "task_id": args[0],
                "parent_task_id": args[1],
                "root_task_id": args[2],
                "user_id": args[3],
                "session_id": args[4],
                "agent_id": args[5],
                "caller_agent_id": args[6],
                "active_agent_id": args[7],
                "state": args[8],
                "idempotency_key": args[9],
                "priority": args[10],
                "deadline": args[11],
                "created_at": args[12],
                "updated_at": args[12],
                "input": args[13],
                "output": None,
                "error": None,
                "status_code": None,
            }

    conn = _C([])
    scope = queries.Scope.user(conn, "usr_alice")  # type: ignore[arg-type]

    out = asyncio.run(scope.create_task(
        session_id="ses_1", agent_id="agt_1",
        caller_agent_id="agt_caller",
        parent_task_id=None,
        priority=TaskPriority.NORMAL,
        deadline=None, idempotency_key=None,
        input={},
    ))
    assert isinstance(out, TaskRow)
    assert out.parent_task_id is None
    assert out.root_task_id == out.task_id  # self-rooted


def test_create_task_with_same_user_parent_inherits_root_task_id() -> None:
    """When the parent exists AND belongs to the same user, the new
    task inherits `root_task_id` from the parent. Existing happy path."""
    from bp_router.db import queries

    class _C:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetchrow(self, query: str, *args: Any) -> Any:
            self.calls.append(query)
            if "SELECT root_task_id" in query:
                # Parent exists for this user.
                return {"root_task_id": "root_xyz"}
            # INSERT path — return a synthetic row. Argument layout
            # matches `Scope.create_task` (see the test in the same
            # file for the canonical indexing).
            return {
                "task_id": args[0],
                "parent_task_id": args[1],
                "root_task_id": args[2],
                "user_id": args[3],
                "session_id": args[4],
                "agent_id": args[5],
                "caller_agent_id": args[6],
                "active_agent_id": args[7],
                "state": args[8],
                "idempotency_key": args[9],
                "priority": args[10],
                "deadline": args[11],
                "created_at": args[12],
                "updated_at": args[12],
                "input": args[13],
                "output": None,
                "error": None,
                "status_code": None,
            }

    conn = _C()
    scope = queries.Scope.user(conn, "usr_alice")  # type: ignore[arg-type]
    out = asyncio.run(scope.create_task(
        session_id="ses_1", agent_id="agt_1",
        caller_agent_id="agt_caller",
        parent_task_id="tsk_parent",
        priority=TaskPriority.NORMAL,
        deadline=None, idempotency_key=None,
        input={},
    ))
    assert out.root_task_id == "root_xyz"


def test_create_task_with_cross_user_parent_raises_cross_user_task_access() -> None:
    """C1 regression test: when `parent_task_id` exists but belongs
    to another user, the SELECT scoped by `user_id` returns None.
    Previous code silently fell through and INSERTed with the foreign
    parent pointer (FK accepts; root_task_id self-defaulted).

    Now raises `CrossUserTaskAccess` BEFORE the INSERT runs.
    """
    from bp_router.db import queries

    inserts: list[tuple[Any, ...]] = []

    class _C:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetchrow(self, query: str, *args: Any) -> Any:
            self.calls.append(query)
            if "SELECT root_task_id" in query:
                # Parent doesn't belong to this user (or doesn't exist).
                return None
            # If we reach the INSERT path, the bug is back.
            inserts.append(args)
            return {}

    conn = _C()
    scope = queries.Scope.user(conn, "usr_attacker")  # type: ignore[arg-type]

    with pytest.raises(queries.CrossUserTaskAccess) as excinfo:
        asyncio.run(scope.create_task(
            session_id="ses_1", agent_id="agt_1",
            caller_agent_id="agt_caller",
            parent_task_id="tsk_victim_owned",
            priority=TaskPriority.NORMAL,
            deadline=None, idempotency_key=None,
            input={},
        ))
    assert excinfo.value.task_id == "tsk_victim_owned"
    # Critically: the INSERT was NOT issued. If C1 ever regresses to
    # silent fall-through, this assertion fails because the SELECT
    # would return None and the code would proceed to INSERT.
    assert inserts == []


# ===========================================================================
# admit_task wraps CrossUserTaskAccess as AdmitError("invalid_parent_task")
# ===========================================================================


def test_admit_task_translates_cross_user_parent_to_admit_error() -> None:
    """Source-level check: `admit_task` catches `CrossUserTaskAccess`
    from the create_task call and re-raises as
    `AdmitError("invalid_parent_task", ...)`. Without this mapping
    the agent would see an opaque server error instead of a typed
    rejection."""
    import inspect

    from bp_router import tasks

    src = inspect.getsource(tasks.admit_task)
    assert "queries.CrossUserTaskAccess" in src
    assert "invalid_parent_task" in src
    # And it raises `AdmitError`, not the original exception.
    assert "raise AdmitError" in src


# ===========================================================================
# DB-M1 — list_descendants has depth bound + cycle protection
# ===========================================================================


def test_list_descendants_query_has_depth_bound() -> None:
    """The recursive CTE enforces termination via a depth counter
    bounded by `_MAX_TASK_TREE_DEPTH`. Source-level check so a
    refactor that drops the bound is caught immediately."""
    import inspect

    from bp_router.db import queries

    src = inspect.getsource(queries.Scope.list_descendants)
    # Anchor + recursive arm + termination bound.
    assert "WITH RECURSIVE" in src
    assert "_depth" in src
    assert "_depth + 1" in src
    assert "_depth <" in src
    # And the bound is the module-level constant, not an inline magic.
    assert "_MAX_TASK_TREE_DEPTH" in src


def test_list_descendants_passes_depth_cap_as_bound_param() -> None:
    """Beyond the source check: actually run the function against a
    stub conn and verify the depth cap is bound as a parameter (not
    interpolated into the SQL)."""
    from bp_router.db import queries

    conn = _StubConn([[]])  # empty result set
    scope = queries.Scope.user(conn, "usr_alice")  # type: ignore[arg-type]
    asyncio.run(scope.list_descendants("tsk_root"))

    op, query, args = conn.calls[0]
    assert op == "fetch"
    assert "WITH RECURSIVE" in query
    # Bound parameters: ($1=task_id, $2=user_id, $3=depth_cap).
    assert args[0] == "tsk_root"
    assert args[1] == "usr_alice"
    assert isinstance(args[2], int) and args[2] >= 16  # generous lower bound


# ===========================================================================
# DB-M2 — insert_task_event enforces ownership via WHERE EXISTS
# ===========================================================================


def test_insert_task_event_succeeds_for_owned_task() -> None:
    """Happy path: when the task belongs to the scoped user, the
    INSERT...SELECT EXISTS produces a row and `RETURNING` populates
    the result."""
    # The new query is INSERT ... SELECT ... WHERE EXISTS RETURNING.
    # Stub: return a synthetic row simulating successful insert.
    from datetime import datetime
    from uuid import uuid4

    from bp_router.db import queries

    fake_row = {
        # `event_id` is `uuid` in the DB; asyncpg returns a UUID
        # object. Use `uuid4()` here so the `TaskEventRow.model_validate`
        # call in the production code path accepts the fixture
        # (upstream-bug #12 audit).
        "event_id": uuid4(),
        "task_id": "tsk_owned",
        "ts": datetime.now(UTC),
        "kind": "admitted",
        "actor_agent_id": "agt_1",
        "from_state": None,
        "to_state": None,
        "payload": {},
    }
    conn = _StubConn([fake_row])
    scope = queries.Scope.user(conn, "usr_alice")  # type: ignore[arg-type]
    out = asyncio.run(scope.insert_task_event(
        task_id="tsk_owned", kind="admitted", actor_agent_id="agt_1",
    ))
    assert out.task_id == "tsk_owned"

    op, query, args = conn.calls[0]
    # Verify the query shape — INSERT ... SELECT ... WHERE EXISTS.
    assert "INSERT INTO task_events" in query
    assert "WHERE EXISTS" in query
    assert "FROM tasks" in query
    # And the user_id is bound (NOT interpolated).
    assert "usr_alice" in args


def test_insert_task_event_raises_when_task_not_owned() -> None:
    """DB-M2 regression test: when the EXISTS clause finds no
    matching task (wrong task_id, wrong user_id), the RETURNING
    clause yields no rows. The previous implementation silently
    skipped the audit row; now raises `CrossUserTaskAccess` so
    callers see the issue immediately and the surrounding
    transaction rolls back."""
    from bp_router.db import queries

    # Stub: simulate no row inserted (EXISTS clause failed).
    conn = _StubConn([None])
    scope = queries.Scope.user(conn, "usr_attacker")  # type: ignore[arg-type]

    with pytest.raises(queries.CrossUserTaskAccess) as excinfo:
        asyncio.run(scope.insert_task_event(
            task_id="tsk_victim_owned",
            kind="malicious_audit",
            actor_agent_id="agt_attacker",
        ))
    assert excinfo.value.task_id == "tsk_victim_owned"
