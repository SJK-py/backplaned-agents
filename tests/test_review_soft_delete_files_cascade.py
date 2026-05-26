"""`soft_delete_user` caps the user's files' `expires_at` at 24h.

R6 third-pass review (HIGH): `soft_delete_user` ran a four-step
cascade (refresh tokens, password-reset tokens, serviced_by
sweep) but did NOT touch `files`. The user's files survived
their natural TTL (up to 7 days by default) after a soft-delete,
and a file with `expires_at IS NULL` (none today but the schema
allows it) survived indefinitely. The background file-GC
(`_gc_files_once`) reaps by `expires_at`, so capping there is
the right shim.

R6 fix: extend the cascade with a single UPDATE that pulls
`expires_at` down to `LEAST(current_value, now() + 24h)` for
every file the user owns. The background GC then collects the
bytes within its next sweep cycle.

Source-pin style — the actual UPDATE is exercised by integration
tests against Postgres; these tests pin the SQL shape + return
shape.
"""

from __future__ import annotations

import inspect

import pytest


def test_soft_delete_user_caps_file_expiry() -> None:
    """Source pin: the cascade now includes an `UPDATE files`
    that pulls `expires_at` down to `LEAST(..., now() + 24h)`."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    assert "UPDATE files" in src
    assert "expires_at = LEAST(" in src
    # The 24h ceiling — adjustable via the SQL literal.
    assert "INTERVAL '24 hours'" in src


def test_soft_delete_user_preserves_earlier_expiry() -> None:
    """The LEAST() guards against silently EXTENDING an earlier
    expiry. A short-lived ephemeral upload (e.g. expires_at = now
    + 5 min for a captcha challenge file) must keep its 5-min
    schedule, not be lifted to 24h. Source pin on the LEAST."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    # LEAST(COALESCE(expires_at, now() + 24h), now() + 24h)
    # — preserves the earlier of (existing-or-24h, 24h).
    assert "LEAST(" in src
    assert "COALESCE(expires_at, now()" in src


def test_soft_delete_user_handles_nullable_expires_at() -> None:
    """A file with `expires_at IS NULL` must NOT be left
    untouched — that's the case that would survive indefinitely.
    COALESCE(NULL, now() + 24h) → 24h. LEAST(24h, 24h) → 24h.
    Source pin on the COALESCE."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    assert "COALESCE(expires_at, now() + INTERVAL '24 hours')" in src


def test_soft_delete_user_returns_files_expired_count() -> None:
    """The return dict now includes `files_expired_count` for
    audit trails. Pre-R6 the dict had four counts (refresh,
    reset, sweep, was_already_deleted); R6 adds the fifth."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    # Two return paths — the already-deleted no-op and the live
    # cascade. Both must include the new key for symmetric
    # dict shape across calls.
    assert src.count('"files_expired_count"') >= 2


def test_soft_delete_user_files_count_extracted_from_asyncpg_status() -> None:
    """asyncpg's `conn.execute(...)` for an UPDATE returns a
    command tag like `"UPDATE 17"`. Pin the parsing step so a
    future refactor that swaps `execute` for `fetchval` (which
    would return None for an UPDATE without RETURNING) doesn't
    silently report `files_expired_count: 0`."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    # The split-on-space + int() pattern.
    assert 'rsplit(" ", 1)' in src
    assert "int(" in src


def test_soft_delete_user_count_extraction_is_defensive() -> None:
    """The status-tag parse is wrapped in try/except so a
    malformed tag (asyncpg version drift, mock object that
    doesn't have rsplit) doesn't break the cascade."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    # The except branch falls back to 0 — count is audit-only.
    assert "except" in src
    assert "files_expired = 0" in src


def test_idempotent_path_includes_files_count_zero() -> None:
    """The already-deleted return shape includes
    `files_expired_count: 0` so callers don't have to special-case
    the missing key."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    # The was_already_deleted=True dict has the new key with 0.
    idx = src.index('"was_already_deleted": True')
    snippet = src[idx : idx + 500]
    assert '"files_expired_count": 0' in snippet
