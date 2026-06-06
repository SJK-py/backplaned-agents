"""Closed-session GC — the router's background hard-delete of sessions
closed past `closed_session_retention_days`, plus the `filter-existing`
endpoint the suite uses to reap its own rows for purged sessions.

Integration tests are gated on `test_db_url` (router schema). Pure-logic
tests assert the SQL safety guards via source inspection.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg


async def _init_json_codec(conn: asyncpg.Connection) -> None:
    """Register the jsonb codec the router's own pool installs — needed so
    `append_audit_event`'s dict → jsonb write works on a raw test pool."""
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(
            typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


def test_gc_selects_closed_old_no_live_tasks() -> None:
    """The candidate query must require closed_at set + past cutoff + NO live
    task. A regression here risks deleting an open or in-flight session."""
    from bp_router import tasks as tasks_mod

    src = inspect.getsource(tasks_mod._gc_closed_sessions)
    assert "closed_at IS NOT NULL" in src
    assert "closed_at < $1" in src
    # Live-task exclusion covers all non-terminal states.
    assert "'QUEUED', 'RUNNING', 'WAITING_CHILDREN'" in src
    assert "purge_session" in src  # reuses the audited, file-detaching purge


async def _insert_session(conn, sid: str, *, closed_at, with_live_task=False):
    await conn.execute(
        "INSERT INTO sessions (session_id, user_id, closed_at) VALUES ($1,'usr_a',$2)",
        sid, closed_at,
    )
    if with_live_task:
        await conn.execute(
            "INSERT INTO tasks (task_id, root_task_id, user_id, session_id, "
            "agent_id, caller_agent_id, active_agent_id, state) VALUES "
            "($1, $1, 'usr_a', $2, 'orchestrator', 'orchestrator', "
            "'orchestrator', 'RUNNING')",
            f"tsk_{sid}", sid,
        )


def test_gc_closed_sessions_purges_only_old_closed_idle(test_db_url: str) -> None:
    async def _drive() -> None:
        pool = await asyncpg.create_pool(test_db_url, init=_init_json_codec)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE users, agents, sessions, tasks, task_events, "
                    "audit_log RESTART IDENTITY CASCADE"
                )
                await conn.execute(
                    "INSERT INTO users (user_id, level, auth_kind) "
                    "VALUES ('usr_a','tier0','password')"
                )
                await conn.execute(
                    "INSERT INTO agents (agent_id, kind, status) "
                    "VALUES ('orchestrator','embedded','active')"
                )
                now = datetime.now(UTC)
                old = now - timedelta(days=100)
                recent = now - timedelta(days=10)
                await _insert_session(conn, "ses_old", closed_at=old)
                await _insert_session(conn, "ses_recent", closed_at=recent)
                await _insert_session(conn, "ses_open", closed_at=None)
                await _insert_session(
                    conn, "ses_live", closed_at=old, with_live_task=True
                )

            from bp_router import tasks as tasks_mod

            state = SimpleNamespace(db_pool=pool)
            purged = await tasks_mod._gc_closed_sessions(state, retention_days=90)
            assert purged == 1

            async with pool.acquire() as conn:
                survivors = {
                    r["session_id"]
                    for r in await conn.fetch("SELECT session_id FROM sessions")
                }
                assert survivors == {"ses_recent", "ses_open", "ses_live"}
                # The purge was audited.
                audited = await conn.fetchval(
                    "SELECT count(*) FROM audit_log "
                    "WHERE event='session.purged' AND target_id='ses_old'"
                )
                assert audited == 1
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_filter_existing_returns_present_subset(test_db_url: str) -> None:
    """The endpoint handler returns exactly the supplied ids that still have a
    `sessions` row — driven directly so we don't stand up the auth stack."""
    async def _drive() -> None:
        pool = await asyncpg.create_pool(test_db_url, init=_init_json_codec)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE users, sessions RESTART IDENTITY CASCADE"
                )
                await conn.execute(
                    "INSERT INTO users (user_id, level, auth_kind) "
                    "VALUES ('usr_a','tier0','password')"
                )
                await conn.execute(
                    "INSERT INTO sessions (session_id, user_id) "
                    "VALUES ('ses_live','usr_a')"
                )

            from bp_router.api.admin import (
                FilterExistingSessionsRequest,
                filter_existing_sessions,
            )

            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(bp=SimpleNamespace(db_pool=pool)))
            )
            resp = await filter_existing_sessions(
                request,  # type: ignore[arg-type]
                FilterExistingSessionsRequest(session_ids=["ses_live", "ses_gone"]),
                principal=SimpleNamespace(user_id="svc"),  # type: ignore[arg-type]
            )
            assert resp.existing == ["ses_live"]

            empty = await filter_existing_sessions(
                request,  # type: ignore[arg-type]
                FilterExistingSessionsRequest(session_ids=[]),
                principal=SimpleNamespace(user_id="svc"),  # type: ignore[arg-type]
            )
            assert empty.existing == []
        finally:
            await pool.close()

    asyncio.run(_drive())
