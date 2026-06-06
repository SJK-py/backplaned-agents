"""Permanent user purge (GDPR erasure), router side: `purge_user` scrubs PII +
stamps purged_at + hard-deletes content + audits `user.purged`, and the
`filter-purged` endpoint reports purged users to the suite reaper.

Integration tests gated on `test_db_url`; a logic guard via source inspection.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import asyncpg


async def _init_json_codec(conn: asyncpg.Connection) -> None:
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(
            typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


def test_purge_user_keeps_row_and_scrubs_pii_source() -> None:
    """The tombstone contract: the row is kept (UPDATE, not DELETE) with PII
    nulled + purged_at stamped, and user_id is retained in the audit."""
    from bp_router.db import queries

    src = inspect.getsource(queries.purge_user)
    assert "UPDATE users SET email = NULL, auth_secret_hash = NULL" in src
    assert "purged_at = now()" in src
    assert "DELETE FROM users" not in src  # row is kept as a tombstone
    assert 'event="user.purged"' in src and "target_id=user_id" in src


def test_purge_user_erases_content_and_marks_purged(test_db_url: str) -> None:
    async def _drive() -> None:
        pool = await asyncpg.create_pool(test_db_url, init=_init_json_codec)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE users, agents, sessions, tasks, task_events, "
                    "files, file_names, audit_log RESTART IDENTITY CASCADE"
                )
                await conn.execute(
                    "INSERT INTO users (user_id, level, auth_kind, email, "
                    "auth_secret_hash) VALUES "
                    "('usr_a','tier0','password','a@x.io','HASH')"
                )
                await conn.execute(
                    "INSERT INTO sessions (session_id, user_id) "
                    "VALUES ('ses_1','usr_a')"
                )
                # A persist-scoped named file (survives session purge, must go).
                await conn.execute(
                    "INSERT INTO files (file_id, sha256, user_id, byte_size, "
                    "storage_url) VALUES ('fil_1','sha','usr_a',3,'s3://x')"
                )
                await conn.execute(
                    "INSERT INTO file_names (user_id, scope, filename, file_id, "
                    "byte_size) VALUES ('usr_a','persist','f.txt','fil_1',3)"
                )

            from bp_router.db import queries

            async with pool.acquire() as conn, conn.transaction():
                result = await queries.purge_user(conn, "usr_a", actor_id="adm")
            assert result is not None and result["sessions_purged"] == 1
            assert result["file_names_deleted"] == 1

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT email, auth_secret_hash, purged_at, deleted_at "
                    "FROM users WHERE user_id='usr_a'"
                )
                # Row kept; PII scrubbed; markers set.
                assert row is not None
                assert row["email"] is None and row["auth_secret_hash"] is None
                assert row["purged_at"] is not None and row["deleted_at"] is not None
                # Content gone.
                assert await conn.fetchval("SELECT count(*) FROM sessions") == 0
                assert await conn.fetchval("SELECT count(*) FROM file_names") == 0
                # Audited, retaining the user_id.
                assert await conn.fetchval(
                    "SELECT count(*) FROM audit_log "
                    "WHERE event='user.purged' AND target_id='usr_a'"
                ) == 1

            # Idempotent: a second purge is a no-op (no second audit row).
            async with pool.acquire() as conn, conn.transaction():
                again = await queries.purge_user(conn, "usr_a", actor_id="adm")
            assert again == {"was_already_purged": True}
            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT count(*) FROM audit_log WHERE event='user.purged'"
                ) == 1
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_filter_purged_users_returns_purged_subset(test_db_url: str) -> None:
    async def _drive() -> None:
        pool = await asyncpg.create_pool(test_db_url, init=_init_json_codec)
        try:
            async with pool.acquire() as conn:
                await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
                await conn.execute(
                    "INSERT INTO users (user_id, level, auth_kind, purged_at) "
                    "VALUES ('usr_purged','tier0','password', now())"
                )
                await conn.execute(
                    "INSERT INTO users (user_id, level, auth_kind) "
                    "VALUES ('usr_live','tier0','password')"
                )

            from bp_router.api.admin import (
                FilterPurgedUsersRequest,
                filter_purged_users,
            )

            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(bp=SimpleNamespace(db_pool=pool)))
            )
            resp = await filter_purged_users(
                request,  # type: ignore[arg-type]
                FilterPurgedUsersRequest(user_ids=["usr_purged", "usr_live", "usr_x"]),
                principal=SimpleNamespace(user_id="svc"),  # type: ignore[arg-type]
            )
            assert resp.purged == ["usr_purged"]
        finally:
            await pool.close()

    asyncio.run(_drive())
