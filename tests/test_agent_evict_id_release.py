"""Eviction frees the agent_id for reuse (tombstone-rename, history kept).

Evict renames the `removed` row's PK to `deleted_<id>_<epoch>` (and its
co-located service principal the same way), so a brand-new agent can onboard
under the original id. The FK `ON UPDATE CASCADE` (migration 0002) carries
every dependent `tasks` row to the tombstone id — history is preserved, not
purged. Query transitions run against the live schema; the endpoint contract
is source-pinned.
"""

from __future__ import annotations

import asyncio
import inspect

import asyncpg

from bp_router.api import admin as admin_mod
from bp_router.db import queries


def test_tombstone_name_fits_agent_id_check() -> None:
    """`deleted_<id>_<epoch>` must satisfy the agents.agent_id CHECK
    (≤64 chars, [A-Za-z_][A-Za-z0-9_-]{0,63}, no '.'/':')."""
    import re  # noqa: PLC0415

    pat = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    # Ordinary id.
    short = queries.tombstone_agent_id("chatbot", epoch=1748000000)
    assert short == "deleted_chatbot_1748000000"
    assert pat.match(short)
    # Pathologically long id is truncated to fit (suffix always preserved).
    long_id = "x" * 80
    t = queries.tombstone_agent_id(long_id, epoch=1748000000)
    assert len(t) <= 64
    assert t.endswith("_1748000000")
    assert pat.match(t)


def test_evict_endpoint_releases_id_contract() -> None:
    src = inspect.getsource(admin_mod.evict_agent)
    # Fails in-flight tasks BEFORE renaming (they key off the live id).
    fail_idx = src.index("fail_inflight_for_agent")
    rename_idx = src.index("rename_evicted_agent")
    assert fail_idx < rename_idx
    # Auto-detects the service twin and audits the release.
    assert "service_user_id_for_agent(agent_id)" in src
    assert 'event="agent.id_released"' in src
    assert '"tombstone_agent_id"' in src
    assert '"id_released"' in src


def test_evict_rename_releases_id_and_preserves_history(test_db_url: str) -> None:
    """A removed agent renamed to a tombstone: its tasks follow (cascade),
    the original id is free to INSERT again, and a service twin is renamed."""

    async def _drive() -> dict:
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.execute(
                "TRUNCATE users, agents, sessions, tasks, task_events "
                "RESTART IDENTITY CASCADE"
            )
            # An agent + its co-located service principal + a user + session.
            await conn.execute(
                "INSERT INTO agents (agent_id, kind, status) "
                "VALUES ('chatbot', 'external', 'removed')"
            )
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_service_chatbot', 'service', 'api_key')"
            )
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_a', 'tier1', 'password')"
            )
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id) VALUES ('ses_1', 'usr_a')"
            )
            # A finished task referencing the agent as handler/caller/active.
            await conn.execute(
                "INSERT INTO tasks (task_id, root_task_id, user_id, session_id, "
                "agent_id, caller_agent_id, active_agent_id, state) "
                "VALUES ('t1', 't1', 'usr_a', 'ses_1', 'chatbot', 'chatbot', "
                "'chatbot', 'SUCCEEDED')"
            )

            async with conn.transaction():
                new_aid, new_svc = await queries.rename_evicted_agent(
                    conn, "chatbot", epoch=1748000000,
                    service_user_id="usr_service_chatbot",
                )

            # The task row followed the rename (history preserved, not purged).
            task_agent = await conn.fetchval(
                "SELECT agent_id FROM tasks WHERE task_id = 't1'"
            )
            # Original ids are now free — a fresh agent + service user onboard.
            await conn.execute(
                "INSERT INTO agents (agent_id, kind, status) "
                "VALUES ('chatbot', 'external', 'pending')"
            )
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_service_chatbot', 'service', 'api_key')"
            )
            statuses = {
                r["agent_id"]: r["status"]
                for r in await conn.fetch("SELECT agent_id, status FROM agents")
            }
            return {
                "new_aid": new_aid, "new_svc": new_svc,
                "task_agent": task_agent, "statuses": statuses,
            }
        finally:
            await conn.close()

    res = asyncio.run(_drive())
    assert res["new_aid"] == "deleted_chatbot_1748000000"
    assert res["new_svc"] == "deleted_usr_service_chatbot_1748000000"
    # The task cascaded onto the tombstone id (preserved under the new name).
    assert res["task_agent"] == "deleted_chatbot_1748000000"
    # Both the tombstone and a fresh re-onboarded agent now coexist.
    assert res["statuses"]["deleted_chatbot_1748000000"] == "removed"
    assert res["statuses"]["chatbot"] == "pending"


def test_rename_only_acts_on_removed(test_db_url: str) -> None:
    """A non-removed agent is left untouched (no-op, returns the same id)."""

    async def _drive() -> tuple:
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.execute(
                "TRUNCATE users, agents, sessions, tasks, task_events "
                "RESTART IDENTITY CASCADE"
            )
            await conn.execute(
                "INSERT INTO agents (agent_id, kind, status) "
                "VALUES ('live', 'external', 'active')"
            )
            async with conn.transaction():
                new_aid, new_svc = await queries.rename_evicted_agent(
                    conn, "live", epoch=1748000000
                )
            still = await conn.fetchval(
                "SELECT agent_id FROM agents WHERE agent_id = 'live'"
            )
            return (new_aid, new_svc, still)
        finally:
            await conn.close()

    new_aid, new_svc, still = asyncio.run(_drive())
    assert new_aid == "live" and new_svc is None and still == "live"
