"""Blob GC is refcount-aware — a named blob is NOT reaped by its upload TTL.

Regression for the pre-release blocker: `_gc_files_once` reaped blobs purely
by `files.expires_at` (set to now+TTL at upload) and never consulted the
`file_names` directory, so a `persist/{name}` (or any still-named session
blob) was destroyed `file_default_ttl_s` after upload while a name still
pointed at it — silent data loss. The `count_names_for_file` helper and every
"left for the refcount sweep" docstring assumed a refcount sweep that was
never wired.

Fix: `find_expired_files` (the sweep's selection) now requires the blob to
have ZERO referencing `file_names` rows. `expires_at` is purely the
eligibility timer for a NAMELESS blob (a never-bound orphan upload, or a blob
whose last name was deleted).

Source-pin guards cover the wiring where `TEST_DB_URL` is unset; the
round-trip proves the predicate against the real schema.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta

import asyncpg

from bp_router.db import queries


def test_find_expired_files_excludes_named_blobs_source() -> None:
    """Source pin: the sweep selection guards on NOT EXISTS over file_names."""
    src = inspect.getsource(queries.find_expired_files)
    assert "NOT EXISTS" in src
    assert "file_names" in src and "fn.file_id = files.file_id" in src


def test_inline_write_blob_gets_upload_ttl_source() -> None:
    """Source pin: the inline `_file_write` path no longer inserts a blob
    with `expires_at=None` (which made it un-reclaimable forever once
    unbound); it sets the same upload TTL as the upload-with-grant path."""
    from bp_router import dispatch  # noqa: PLC0415

    src = inspect.getsource(dispatch._file_write)
    # The inline-write insert now carries a TTL derived from
    # file_default_ttl_s instead of expires_at=None.
    assert "file_default_ttl_s" in src
    assert "expires_at=None" not in src


def test_named_blob_survives_expiry_sweep(test_db_url: str) -> None:
    """End-to-end against the real schema: an expired blob is reaped only
    while it is NAMELESS; binding a name spares it; deleting the name makes
    it eligible again."""

    async def _drive() -> None:
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.execute(
                "TRUNCATE users, files, file_names RESTART IDENTITY CASCADE"
            )
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('usr_a', 'tier0', 'password')"
            )
            past = datetime.now(UTC) - timedelta(hours=1)
            # A blob whose upload TTL has already elapsed.
            await conn.execute(
                "INSERT INTO files (file_id, sha256, user_id, byte_size, "
                "storage_url, expires_at) "
                "VALUES ('fil_1', 'sha', 'usr_a', 3, 's3://x', $1)",
                past,
            )
            now = datetime.now(UTC)

            # (1) Nameless + expired → reapable.
            rows = await queries.find_expired_files(conn, now=now)
            assert [r.file_id for r in rows] == ["fil_1"]

            # (2) Bind a persist name to it → spared despite the elapsed TTL.
            await conn.execute(
                "INSERT INTO file_names (user_id, scope, filename, file_id, "
                "byte_size) VALUES ('usr_a', 'persist', 'chart.png', 'fil_1', 3)"
            )
            rows = await queries.find_expired_files(conn, now=now)
            assert rows == []  # named → not reaped (the data-loss fix)

            # (3) A second name (session scope) on the same blob — still spared.
            await conn.execute(
                "INSERT INTO file_names (user_id, scope, filename, file_id, "
                "byte_size) VALUES ('usr_a', 'session:ses_1', 'chart.png', "
                "'fil_1', 3)"
            )
            assert await queries.find_expired_files(conn, now=now) == []

            # (4) Drop both names → blob is nameless again → reapable.
            await conn.execute("DELETE FROM file_names WHERE file_id = 'fil_1'")
            rows = await queries.find_expired_files(conn, now=now)
            assert [r.file_id for r in rows] == ["fil_1"]
        finally:
            await conn.close()

    asyncio.run(_drive())
