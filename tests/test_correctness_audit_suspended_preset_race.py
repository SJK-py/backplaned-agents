"""Tests for the three correctness invariants from the post-merge review:

  C3 — `append_audit_event` takes an advisory lock so concurrent
        writes on an empty `audit_log` can't both insert genesis rows
        with `prev_hash=""`, forking the chain.
  C5 — `resolve_user_level` returns None for suspended users so the
        LLM tier gate denies them on every non-`*` preset, even
        before the next cache invalidation.
  C6 — Admin preset create / patch / delete take an advisory lock and
        run cycle check inside the same transaction, so two concurrent
        writers can't each individually pass while their combined
        effect is a fallback cycle.

The first two are exercised against fakes that record SQL / mutate
in-memory state. C6 is verified by reading the handler structure.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# C3 — advisory lock on the audit chain
# ---------------------------------------------------------------------------


class _RecordingConn:
    """asyncpg-shaped stub that captures every executed query in
    order, plus the args. Returns pre-staged responses for fetchrow."""

    def __init__(self, *, audit_rows: list[dict[str, Any]]) -> None:
        self.queries: list[str] = []
        self.exec_args: list[tuple[Any, ...]] = []
        self._audit_rows = audit_rows

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append(query)
        self.exec_args.append(args)
        return ""

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.queries.append(query)
        self.exec_args.append(args)
        if "audit_log" in query and "self_hash" in query:
            return self._audit_rows[-1] if self._audit_rows else None
        return None

    def transaction(self) -> _RecordingTxn:
        return _RecordingTxn(self)


class _RecordingTxn:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        self._conn.queries.append("BEGIN")
        self._conn.exec_args.append(())  # keep parallel arrays aligned
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._conn.queries.append("ROLLBACK" if exc else "COMMIT")
        self._conn.exec_args.append(())


def test_audit_event_takes_advisory_lock_first_in_transaction() -> None:
    """Verify the `pg_advisory_xact_lock` runs INSIDE the transaction
    and BEFORE the read of the latest row — otherwise two concurrent
    writers would still race the read."""
    from bp_router.db import queries

    conn = _RecordingConn(audit_rows=[])
    asyncio.run(queries.append_audit_event(
        conn,  # type: ignore[arg-type]
        actor_kind="admin",
        actor_id="u",
        event="test",
    ))

    # Sequence: BEGIN → advisory lock → SELECT prev → INSERT → COMMIT.
    flat = " | ".join(q.strip().split("\n")[0] for q in conn.queries)
    begin_idx = conn.queries.index("BEGIN")
    lock_idx = next(
        i for i, q in enumerate(conn.queries)
        if "pg_advisory_xact_lock" in q
    )
    select_idx = next(
        i for i, q in enumerate(conn.queries)
        if "self_hash" in q and "audit_log" in q
    )
    insert_idx = next(
        i for i, q in enumerate(conn.queries)
        if "INSERT INTO audit_log" in q
    )
    commit_idx = conn.queries.index("COMMIT")

    assert begin_idx < lock_idx < select_idx < insert_idx < commit_idx, (
        f"audit-event ordering broken: {flat}"
    )


def test_audit_event_lock_uses_constant_key() -> None:
    """The advisory lock key MUST be a stable constant — using a
    per-event-derived key would defeat serialisation entirely (each
    writer would lock a different row, no contention)."""
    from bp_router.db import queries

    conn = _RecordingConn(audit_rows=[])
    asyncio.run(queries.append_audit_event(
        conn,  # type: ignore[arg-type]
        actor_kind="admin",
        actor_id="u",
        event="ev1",
    ))
    first_args = next(
        args for q, args in zip(conn.queries, conn.exec_args, strict=False)
        if "pg_advisory_xact_lock" in q
    )

    conn2 = _RecordingConn(audit_rows=[])
    asyncio.run(queries.append_audit_event(
        conn2,  # type: ignore[arg-type]
        actor_kind="user",
        actor_id="u2",
        event="ev2",
    ))
    second_args = next(
        args for q, args in zip(conn2.queries, conn2.exec_args, strict=False)
        if "pg_advisory_xact_lock" in q
    )

    assert first_args == second_args, (
        "advisory lock key must be a stable constant across writers"
    )


def test_audit_event_chain_links_prev_hash_correctly() -> None:
    """Sanity check on the chain: when there's a previous row, the
    new row's body includes that prev_hash (so SHA over body actually
    binds the chain)."""
    from bp_router.db import queries

    conn = _RecordingConn(audit_rows=[{"self_hash": "abcdef"}])
    asyncio.run(queries.append_audit_event(
        conn,  # type: ignore[arg-type]
        actor_kind="admin",
        actor_id="u",
        event="follower",
    ))

    insert_args = next(
        args for q, args in zip(conn.queries, conn.exec_args, strict=False)
        if "INSERT INTO audit_log" in q
    )
    # Args are positional matches the INSERT shape; prev_hash is one
    # of them. Just check that "abcdef" appears.
    assert any(a == "abcdef" for a in insert_args), (
        f"prev_hash not threaded into INSERT args: {insert_args}"
    )


# ---------------------------------------------------------------------------
# C5 — suspended users no longer pass the LLM tier gate
# ---------------------------------------------------------------------------


def _make_user_row(*, level: str, suspended: bool) -> Any:
    """Build a UserRow stand-in (just an object with `.level`,
    `.suspended_at`, and `.deleted_at` — `resolve_user_level` reads
    those three fields)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        user_id="u1",
        level=level,
        auth_kind="password",
        auth_secret_hash=None,
        email=None,
        created_at=datetime.now(UTC),
        suspended_at=datetime.now(UTC) if suspended else None,
        deleted_at=None,
    )


def test_resolve_user_level_returns_none_for_suspended_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]

    suspended_user = _make_user_row(level="tier0", suspended=True)
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=suspended_user),
    )

    out = asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u1"))
    assert out is None
    # And the cache must NOT be poisoned with `None`/level — otherwise
    # an un-suspend wouldn't take effect on the next request.
    assert "u1" not in svc._user_level_cache


def test_resolve_user_level_caches_active_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: active users still get cached so we don't hammer the DB."""
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]

    user = _make_user_row(level="tier1", suspended=False)
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=user),
    )

    out = asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u1"))
    assert out == "tier1"
    assert "u1" in svc._user_level_cache


def test_suspend_invalidates_cached_user_level() -> None:
    """Plug the invalidation hook: after `update_user` flips
    `suspended_at`, the LLM service drops the cache entry so the next
    call re-fetches and sees the suspension. (We exercise the helper
    directly here — the admin endpoint integration is covered by
    code reading; FastAPI isn't installed in CI.)"""
    from bp_router.llm.service import LlmService, _UserLevelCacheEntry

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]
    import time

    svc._user_level_cache["u1"] = _UserLevelCacheEntry(
        level="tier1",
        expires_at=time.monotonic() + 60.0,
    )
    svc.invalidate_user_level("u1")
    assert "u1" not in svc._user_level_cache


# ---------------------------------------------------------------------------
# Source-reading tests (need fastapi to import admin module)
# ---------------------------------------------------------------------------

# These tests read `bp_router.api.admin` source, which transitively
# imports fastapi. Skip when fastapi isn't available (CI sandbox).
try:
    import fastapi  # noqa: F401
    _has_fastapi = True
except ImportError:
    _has_fastapi = False

_skip_no_fastapi = pytest.mark.skipif(
    not _has_fastapi, reason="fastapi not installed"
)


@_skip_no_fastapi
def test_admin_user_patch_invalidates_cache_on_suspend() -> None:
    """C5: the suspend / unsuspend branches must appear in the
    cache-invalidation set in admin.update_user, alongside
    level_changed."""
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin.update_user)
    assert "user.suspended" in src
    assert "user.unsuspended" in src
    assert "invalidate_user_level" in src


# ---------------------------------------------------------------------------
# C6 — preset writes serialised by advisory lock + transaction
# ---------------------------------------------------------------------------


@_skip_no_fastapi
def test_preset_write_handlers_use_transaction_and_advisory_lock() -> None:
    """Read-the-source check: each of the three preset write handlers
    (create / update / delete) must wrap their work in a transaction
    and call `_lock_preset_writes` so concurrent admins serialise on
    the cycle-check critical section."""
    import inspect

    from bp_router.api import admin

    for fn in (
        admin.create_llm_preset,
        admin.update_llm_preset,
        admin.delete_llm_preset,
    ):
        src = inspect.getsource(fn)
        assert "conn.transaction()" in src, (
            f"{fn.__name__} must run inside conn.transaction()"
        )
        assert "_lock_preset_writes" in src, (
            f"{fn.__name__} must take the preset-write advisory lock"
        )


@_skip_no_fastapi
def test_lock_preset_writes_uses_constant_key() -> None:
    """The lock helper must take a stable advisory key so all preset
    writers contend on the same lock. Source check is sufficient."""
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin._lock_preset_writes)
    assert "pg_advisory_xact_lock" in src
    # The key constant lives next to the helper.
    assert "_PRESET_WRITE_LOCK_KEY" in src


@_skip_no_fastapi
def test_create_llm_preset_runs_cycle_check_inside_lock() -> None:
    """Sanity on call ordering: the cycle check must run AFTER the
    INSERT (so the candidate map reflects the write) but INSIDE the
    advisory lock (so concurrent writes don't both pass)."""
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin.create_llm_preset)
    lines = src.split("\n")
    lock_line = next(i for i, l in enumerate(lines) if "_lock_preset_writes" in l)
    insert_line = next(i for i, l in enumerate(lines) if "insert_llm_preset" in l)
    cycle_line = next(
        i for i, l in enumerate(lines) if "_check_fallback_post_write" in l
    )
    audit_line = next(
        i for i, l in enumerate(lines) if 'event="llm_preset.created"' in l
    )

    assert lock_line < insert_line < cycle_line < audit_line, (
        "create_llm_preset ordering broken; expected lock → insert → cycle → audit"
    )
