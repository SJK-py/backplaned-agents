"""Idempotency dedup is per (caller_agent, user, key) — not cross-agent.

Pre-release review: the dedup keyed on `(user_id, idempotency_key)`, so two
DIFFERENT caller agents acting for the same user that picked the same key
string collided — the second agent's spawn replayed the FIRST agent's task
result instead of creating its own (a cross-agent result leak), contradicting
`protocol.md` §4.3 ("unique per agent", "not visible to the destination").

Fix: the `tasks_idempotency_unique` constraint and `find_idempotent` are now
keyed on `(caller_agent_id, user_id, idempotency_key)`. (The dedup remains
permanent — no expiry window — which `protocol.md` §4.3 now documents.)
"""

from __future__ import annotations

import asyncio
import inspect
import json

import asyncpg
import pytest

from bp_router.db import queries


def test_find_idempotent_filters_by_caller_agent_sourcepin() -> None:
    src = inspect.getsource(queries.Scope.find_idempotent)
    assert "caller_agent_id = $1" in src


def test_constraint_includes_caller_agent_sourcepin() -> None:
    import pathlib

    p = pathlib.Path("bp_router/db/migrations/versions/0001_initial_schema.py")
    src = p.read_text()
    assert (
        "tasks_idempotency_unique UNIQUE "
        "(caller_agent_id, user_id, idempotency_key)" in src
    )


def test_per_agent_scope_round_trip(test_db_url: str) -> None:
    """Two caller agents, same (user, key): each gets its OWN task; the same
    caller reusing the key still dedups (UNIQUE violation)."""

    async def _drive() -> None:
        conn = await asyncpg.connect(test_db_url)
        try:
            # Mirror the router pool's jsonb codec so TaskRow's jsonb columns
            # (input/output/error) decode to dicts, not strings.
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
            await conn.execute(
                "TRUNCATE users, agents, sessions, tasks RESTART IDENTITY CASCADE"
            )
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_x', 'tier0', 'password')"
            )
            for a in ("agt_a", "agt_b", "dest"):
                await conn.execute(
                    "INSERT INTO agents (agent_id, kind, status) "
                    "VALUES ($1, 'external', 'active')", a,
                )
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id) VALUES ('ses_1', 'usr_x')"
            )

            async def _insert(task_id: str, caller: str) -> None:
                await conn.execute(
                    "INSERT INTO tasks (task_id, root_task_id, user_id, session_id, "
                    "agent_id, caller_agent_id, active_agent_id, state, "
                    "idempotency_key) VALUES "
                    "($1, $1, 'usr_x', 'ses_1', 'dest', $2, 'dest', 'SUCCEEDED', 'k1')",
                    task_id, caller,
                )

            # agt_a creates a task under key 'k1'.
            await _insert("t_a", "agt_a")

            scope = queries.Scope.user(conn, "usr_x")
            # agt_a sees its own task; agt_b sees NOTHING (no cross-agent leak).
            got_a = await scope.find_idempotent("k1", caller_agent_id="agt_a")
            got_b = await scope.find_idempotent("k1", caller_agent_id="agt_b")
            assert got_a is not None and got_a.task_id == "t_a"
            assert got_b is None, "different caller agent must NOT see agt_a's task"

            # agt_b can create its OWN task under the same (user, key) — the
            # constraint now includes caller_agent_id, so no unique violation.
            await _insert("t_b", "agt_b")
            got_b2 = await scope.find_idempotent("k1", caller_agent_id="agt_b")
            assert got_b2 is not None and got_b2.task_id == "t_b"

            # The SAME caller reusing the key still collides (dedup intact).
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert("t_a2", "agt_a")
        finally:
            await conn.close()

    asyncio.run(_drive())
