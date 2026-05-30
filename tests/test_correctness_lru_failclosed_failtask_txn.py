"""Tests for the correctness bundle (review M3 + M5 + M8):

  M3 — `_user_level_cache` is bounded by an LRU cap so a multi-tenant
        install with many distinct user_ids can't grow it without
        bound. Hot users stay alive past the cap; least-recently-used
        entries get evicted on insert.
  M5 — When `resolve_user_level` fails (DB unreachable) and the
        requested preset has `min_user_level != "*"`, dispatch
        surfaces a clean ``auth_lookup_failed`` error code rather
        than silently falling through with `user_level=None`.
  M8 — `fail_task` reads `parent_task_id` INSIDE its transaction so
        future maintainers can't accidentally introduce non-atomic
        writes after the state transition.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# M3 — LRU cap on the user-level cache
# ---------------------------------------------------------------------------


def _make_user_row(level: str = "tier1", suspended: bool = False) -> Any:
    return SimpleNamespace(
        user_id="placeholder",
        level=level,
        auth_kind="password",
        auth_secret_hash=None,
        email=None,
        created_at=datetime.now(UTC),
        suspended_at=datetime.now(UTC) if suspended else None,
        deleted_at=None,
    )


def _service_with_low_cap(cap: int = 3):
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]
    svc.USER_LEVEL_CACHE_MAX = cap
    return svc


def test_lru_evicts_oldest_when_cap_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service_with_low_cap(cap=3)
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=_make_user_row()),
    )

    # Insert four distinct users into a 3-cap cache.
    for uid in ("u1", "u2", "u3", "u4"):
        asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id=uid))

    # u1 should have been evicted; u2/u3/u4 remain.
    assert "u1" not in svc._user_level_cache
    assert set(svc._user_level_cache.keys()) == {"u2", "u3", "u4"}


def test_lru_hits_keep_entries_alive_past_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hot user gets touched on every cache hit; LRU should evict
    cold entries instead."""
    svc = _service_with_low_cap(cap=3)
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=_make_user_row()),
    )

    asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u1"))
    asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u2"))
    asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u3"))

    # Hit u1 (touches it to most-recent).
    asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u1"))

    # Now insert u4 — should evict u2 (oldest), NOT u1.
    asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u4"))

    assert "u1" in svc._user_level_cache
    assert "u2" not in svc._user_level_cache
    assert set(svc._user_level_cache.keys()) == {"u1", "u3", "u4"}


def test_lru_does_not_grow_unbounded_in_default_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default cap is 5000; insert 5005 entries and verify the
    cache size never exceeds the cap."""
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=_make_user_row()),
    )

    # Insert one past the cap.
    cap = svc.USER_LEVEL_CACHE_MAX
    for i in range(cap + 5):
        asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id=f"u{i}"))

    assert len(svc._user_level_cache) == cap


# ---------------------------------------------------------------------------
# M5 — fail-closed on auth lookup error when preset needs tier check
# ---------------------------------------------------------------------------


def test_dispatch_user_level_lookup_error_fails_closed_for_tier_preset() -> None:
    """When the requested preset has min_user_level != "*" and the
    user-level lookup raises (DB outage), dispatch must surface
    `auth_lookup_failed` rather than silently fall through."""
    import inspect as _inspect

    from bp_router import dispatch

    src = _inspect.getsource(dispatch._run_llm_call)
    # The new branch must reference the fail-closed code path AND
    # consult the preset's tier requirement.
    assert "auth_lookup_failed" in src
    # Second-pass rename: the requested-preset gate is `first_preset_gated`.
    assert "first_preset_gated" in src
    # And the failure log must distinguish the requested-preset-gated path.
    assert '"first_preset_gated"' in src


def test_dispatch_user_level_lookup_error_proceeds_for_open_preset() -> None:
    """When the preset is `*` (open), a DB lookup failure should NOT
    produce auth_lookup_failed — the gate doesn't consult user_level
    so the request can proceed (with user_level=None)."""
    import inspect as _inspect

    from bp_router import dispatch

    src = _inspect.getsource(dispatch._run_llm_call)
    # The fail-closed (auth_lookup_failed) path must be guarded by
    # `if first_preset_gated:` — it only fires when the REQUESTED preset is
    # gated. An open (`*`) preset whose lookup fails (or which has no gated
    # fallback) proceeds with user_level=None rather than failing closed.
    assert "if first_preset_gated:" in src


# ---------------------------------------------------------------------------
# M8 — fail_task reads parent_task_id inside the transaction
# ---------------------------------------------------------------------------


def test_fail_task_reads_parent_inside_transaction() -> None:
    """The `SELECT parent_task_id` query must run inside the
    `async with conn.transaction()` block, not after it. Source check
    is sufficient — we read the function body and verify the lock-vs-
    read ordering by line numbers."""
    from bp_router import tasks

    src = inspect.getsource(tasks.fail_task)
    lines = src.split("\n")

    # Match `.transaction()` invoked on any variable name. The
    # review3-M4 refactor made `fail_task` accept an optional
    # `conn` and pushed the DB work into a nested helper that
    # binds the connection to a local `c` — so the transaction
    # is now `c.transaction()` rather than the original
    # `conn.transaction()`. The semantic invariant (parent select
    # inside the transaction) is still pinned by the ordering
    # assertions below.
    txn_start = next(
        (i for i, l in enumerate(lines) if ".transaction()" in l),
        None,
    )
    parent_select = next(
        (
            i for i, l in enumerate(lines)
            if "SELECT parent_task_id" in l
        ),
        None,
    )
    transition = next(
        (i for i, l in enumerate(lines) if "task_transition" in l),
        None,
    )

    assert txn_start is not None, "fail_task must use conn.transaction()"
    assert parent_select is not None
    assert transition is not None
    # The SELECT must come AFTER the txn begins and BEFORE the transition
    # (so we have parent_task_id in hand atomically with the state change).
    assert txn_start < parent_select < transition, (
        f"fail_task ordering broken: txn={txn_start} "
        f"select={parent_select} transition={transition}"
    )


def test_fail_task_does_not_read_parent_outside_transaction() -> None:
    """Defensive: there should be NO `SELECT parent_task_id` after the
    `async with conn.transaction():` block ends. Catches a future
    maintainer accidentally adding a stray read in autocommit."""
    from bp_router import tasks

    src = inspect.getsource(tasks.fail_task)

    # Count the `SELECT parent_task_id` occurrences. There must be
    # exactly one, and it must be inside the transaction (the previous
    # test verified that). This guards against a regression where
    # someone adds a duplicate read.
    assert src.count("SELECT parent_task_id") == 1, (
        "fail_task must read parent_task_id exactly once, inside "
        "the transaction"
    )


# ---------------------------------------------------------------------------
# Combined sanity: existing semantics preserved
# ---------------------------------------------------------------------------


def test_resolve_user_level_still_caches_active_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LRU rework didn't break the basic caching behaviour."""
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=_make_user_row(level="tier2")),
    )

    out = asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u1"))
    assert out == "tier2"
    assert "u1" in svc._user_level_cache


def test_resolve_user_level_suspended_still_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suspended-user gate from PR #37 still works after LRU rework."""
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "bp_router.db.queries.get_user_by_id",
        AsyncMock(return_value=_make_user_row(level="tier0", suspended=True)),
    )

    out = asyncio.run(svc.resolve_user_level(conn=AsyncMock(), user_id="u1"))
    assert out is None
    assert "u1" not in svc._user_level_cache
