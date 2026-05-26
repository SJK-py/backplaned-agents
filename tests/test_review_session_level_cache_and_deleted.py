"""`_session_level` caches via `LlmService._user_level_cache` and
refuses soft-deleted users.

R8 fourth-pass review surfaced two related findings:

  HIGH (tasks.py): `_session_level` SELECT filtered only
  `suspended_at IS NULL`, missing `deleted_at IS NULL`. Soft-
  deleted users could still have tasks admitted on their behalf.

  HIGH (perf): `_session_level` did a fresh DB round-trip per
  admit, bypassing the `LlmService._user_level_cache` (60s TTL,
  5000 entries) that `_principal_from_request` already consults.

R8 fix (single PR — same call site, both bugs):
  - Route through `state.llm_service.peek_user_level_cached`
    first (cache hit → no DB).
  - On miss, call `resolve_user_level(conn, user_id)` which
    consults `users.suspended_at AND users.deleted_at` via the
    shared `user_is_active` helper.
  - Defensive: if `state.llm_service` is unset (test fixtures
    only), fall through to a raw SELECT that ALSO checks
    `deleted_at`.

Net effect: a soft-deleted user can no longer admit tasks; the
admit-hot-path skips the DB on cache hit.
"""

from __future__ import annotations

import inspect

import pytest


def test_session_level_routes_through_cache_peek_first() -> None:
    """Source pin: `_session_level` calls
    `peek_user_level_cached` before touching the DB. The
    docstring also mentions `pool.acquire()` so we pin against
    the actual call expression `async with pool.acquire() as conn:`
    rather than the substring."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks._session_level)
    assert "peek_user_level_cached" in src
    peek_idx = src.index("peek_user_level_cached(user_id)")
    acquire_idx = src.index("async with pool.acquire()")
    assert peek_idx < acquire_idx


def test_session_level_cache_hit_skips_db() -> None:
    """Functional: a fresh cache entry → no DB call.

    Builds a stub state whose `llm_service.peek_user_level_cached`
    returns a non-None level. The DB pool's `acquire` must NOT be
    awaited because we returned cached."""
    pytest.importorskip("fastapi")
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bp_router import tasks

    state = MagicMock()
    state.llm_service.peek_user_level_cached = MagicMock(return_value="tier0")
    state.db_pool.acquire = AsyncMock()

    out = asyncio.run(tasks._session_level(state, "usr_x"))
    assert out == "tier0"
    state.llm_service.peek_user_level_cached.assert_called_once_with("usr_x")
    # Confirms we didn't touch the pool.
    state.db_pool.acquire.assert_not_called()


def test_session_level_falls_through_to_resolve_on_miss() -> None:
    """Functional: cache miss (peek returns None) → call
    `resolve_user_level` which does the DB round-trip with the
    deleted_at guard built in."""
    pytest.importorskip("fastapi")
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bp_router import tasks

    state = MagicMock()
    state.llm_service.peek_user_level_cached = MagicMock(return_value=None)
    state.llm_service.resolve_user_level = AsyncMock(return_value="tier1")

    # `pool.acquire()` is an async context manager → __aenter__
    # returns a mock conn.
    pool_cm = MagicMock()
    pool_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    pool_cm.__aexit__ = AsyncMock(return_value=None)
    state.db_pool.acquire.return_value = pool_cm

    out = asyncio.run(tasks._session_level(state, "usr_y"))
    assert out == "tier1"
    state.llm_service.resolve_user_level.assert_awaited_once()


def test_session_level_fallback_sql_includes_deleted_at_guard() -> None:
    """Source pin on the fallback SELECT (when `state.llm_service`
    is unset — only test fixtures hit this). MUST include the
    `deleted_at IS NULL` clause that was missing pre-R8."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks._session_level)
    assert "deleted_at IS NULL" in src
    assert "suspended_at IS NULL" in src


def test_session_level_returns_none_for_empty_user_id() -> None:
    """Defensive: empty / None user_id short-circuits to None
    before touching cache or DB."""
    pytest.importorskip("fastapi")
    import asyncio
    from unittest.mock import MagicMock

    from bp_router import tasks

    state = MagicMock()
    out = asyncio.run(tasks._session_level(state, ""))
    assert out is None


def test_session_level_docstring_documents_both_lifecycle_flags() -> None:
    """Doc pin: docstring mentions both `suspended_at` and
    `deleted_at` so a future reader doesn't reopen the
    soft-delete hole."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    doc = tasks._session_level.__doc__ or ""
    assert "suspended" in doc.lower()
    assert "deleted" in doc.lower() or "soft-delet" in doc.lower()
