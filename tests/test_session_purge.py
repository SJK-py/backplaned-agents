"""Session hard-delete (`?purge=true`) — the cascade query + endpoint wiring.

The real-DB test seeds the full FK chain (user → agent → session → task →
task_event → file → file_name) and verifies `Scope.purge_session` removes
the session and its dependents in FK order while DETACHING (not deleting)
the dedup'd `files` row. Source-inspection guards cover the wiring so the
contract holds even where `TEST_DB_URL` is unset.
"""

from __future__ import annotations

import asyncio
import inspect

import asyncpg

from bp_router.api import sessions as sessions_mod
from bp_router.db import queries


def test_close_endpoint_has_purge_param_and_audits() -> None:
    src = inspect.getsource(sessions_mod.close_session)
    assert "purge: bool = False" in src
    assert "purge_session(session_id)" in src
    assert 'event="session.purged"' in src
    # Close always runs first (so a purge never races an in-flight task).
    assert src.index("_close_session(") < src.index("purge_session(")


def test_purge_query_detaches_files_and_orders_deletes() -> None:
    src = inspect.getsource(queries.Scope.purge_session)
    # files are detached, never hard-deleted (dedup/refcount safety).
    assert "UPDATE files SET session_id = NULL" in src
    assert "DELETE FROM files" not in src
    # task_events before tasks; sessions last.
    assert src.index("DELETE FROM task_events") < src.index("DELETE FROM tasks")
    assert src.index("DELETE FROM tasks") < src.index("DELETE FROM sessions")


def test_purge_session_cascade(test_db_url: str) -> None:
    """End-to-end against the real router schema + FKs."""
    async def _drive() -> None:
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.execute(
                "TRUNCATE users, agents, sessions, tasks, task_events, files, "
                "file_names RESTART IDENTITY CASCADE"
            )
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_a', 'tier0', 'password')"
            )
            await conn.execute(
                "INSERT INTO agents (agent_id, kind, status) "
                "VALUES ('orchestrator', 'embedded', 'active')"
            )
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id) VALUES ('ses_1', 'usr_a')"
            )
            await conn.execute(
                "INSERT INTO tasks (task_id, root_task_id, user_id, session_id, "
                "agent_id, caller_agent_id, active_agent_id, state) VALUES "
                "('tsk_1', 'tsk_1', 'usr_a', 'ses_1', 'orchestrator', "
                "'orchestrator', 'orchestrator', 'SUCCEEDED')"
            )
            await conn.execute(
                "INSERT INTO task_events (task_id, kind) VALUES ('tsk_1', 'admitted')"
            )
            # One dedup'd blob named in BOTH the session scope and persist/.
            await conn.execute(
                "INSERT INTO files (file_id, sha256, user_id, session_id, task_id, "
                "byte_size, storage_url) VALUES "
                "('fil_1', 'sha', 'usr_a', 'ses_1', 'tsk_1', 3, 's3://x')"
            )
            for scope in ("session:ses_1", "persist"):
                await conn.execute(
                    "INSERT INTO file_names (user_id, scope, filename, file_id, "
                    "byte_size) VALUES ('usr_a', $1, 'f.txt', 'fil_1', 3)",
                    scope,
                )

            async with conn.transaction():
                removed = await queries.Scope.user(conn, "usr_a").purge_session("ses_1")
            assert removed is True

            assert await conn.fetchval("SELECT count(*) FROM sessions") == 0
            assert await conn.fetchval("SELECT count(*) FROM tasks") == 0
            assert await conn.fetchval("SELECT count(*) FROM task_events") == 0
            # session file-name gone; persist/ name survives.
            scopes = [r["scope"] for r in await conn.fetch("SELECT scope FROM file_names")]
            assert scopes == ["persist"]
            # the dedup'd blob row is DETACHED, not deleted (persist still needs it).
            frow = await conn.fetchrow("SELECT session_id, task_id FROM files WHERE file_id='fil_1'")
            assert frow is not None
            assert frow["session_id"] is None and frow["task_id"] is None
        finally:
            await conn.close()

    asyncio.run(_drive())
