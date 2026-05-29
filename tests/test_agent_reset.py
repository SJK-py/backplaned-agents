"""Admin agent reset → pending (recovery for lost agent credentials).

`POST /v1/admin/agents/{id}/reset` moves a registered agent back to
`pending` so it can re-onboard with a fresh invitation — without freeing the
id for reuse (re-onboard still needs an admin invitation) and without
resurrecting an evicted (`removed`, terminal) agent.

Query transitions are exercised against the live schema; the endpoint
contract (status guards, audit, in-flight handling) is source-pinned.
"""

from __future__ import annotations

import asyncio
import inspect

import asyncpg

from bp_router.api import admin as admin_mod
from bp_router.db import queries


def test_reset_query_guards_status_transitions() -> None:
    src = inspect.getsource(queries.reset_agent_to_pending)
    # Only active/suspended → pending; never resurrects removed or no-ops pending.
    assert "status IN ('active', 'suspended')" in src
    assert "SET status = 'pending'" in src
    assert "RETURNING agent_id" in src


def test_reset_endpoint_contract() -> None:
    src = inspect.getsource(admin_mod.reset_agent)
    assert 'event="agent.reset"' in src
    assert "reset_agent_to_pending(conn, agent_id)" in src
    # Idempotent on pending; refuses removed (terminal) with 409.
    assert '"pending"' in src
    assert '"removed"' in src
    assert "409" in src
    # Forces the agent offline so it must re-onboard.
    assert "fail_inflight_for_agent" in src
    assert "agent_reset" in src


def test_reset_agent_to_pending_roundtrip(test_db_url: str) -> None:
    """active/suspended → pending; pending + removed are left untouched."""

    async def _drive() -> dict:
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.execute(
                "TRUNCATE users, agents, sessions, tasks, task_events "
                "RESTART IDENTITY CASCADE"
            )
            rows = [
                ("a_active", "active"),
                ("a_susp", "suspended"),
                ("a_pending", "pending"),
                ("a_removed", "removed"),
            ]
            for aid, status in rows:
                await conn.execute(
                    "INSERT INTO agents (agent_id, kind, status) VALUES ($1, 'external', $2)",
                    aid, status,
                )
            ret = {
                aid: await queries.reset_agent_to_pending(conn, aid)
                for aid, _ in rows
            }
            final = {
                aid: await conn.fetchval(
                    "SELECT status FROM agents WHERE agent_id = $1", aid
                )
                for aid, _ in rows
            }
            return {"ret": ret, "final": final}
        finally:
            await conn.close()

    res = asyncio.run(_drive())
    # active/suspended transitioned to pending.
    assert res["ret"]["a_active"] is True and res["final"]["a_active"] == "pending"
    assert res["ret"]["a_susp"] is True and res["final"]["a_susp"] == "pending"
    # pending is a no-op (already pending); removed stays terminal.
    assert res["ret"]["a_pending"] is False and res["final"]["a_pending"] == "pending"
    assert res["ret"]["a_removed"] is False and res["final"]["a_removed"] == "removed"
