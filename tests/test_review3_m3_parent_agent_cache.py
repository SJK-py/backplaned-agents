"""Tests for the third-pass review M-3 fix — `caller_agent_cache`
short-circuits the per-frame DB JOIN in `_handle_progress`.

Without the cache, every inbound `ProgressFrame` issued a fresh
`pool.acquire()` + JOIN against `tasks` to find the parent agent.
At line rate (a chatty agent emitting 100 Progress/s) this
saturates the default 10-conn DB pool. The cache is populated at
admit time (caller_agent_id IS the caller_agent_id; immutable for
the task's lifetime), evicted on terminal-state transition, and
back-filled on miss for tasks admitted before this worker started.

This is the §11.4 work flagged in `docs/design/quota-enforcement.md`
as the "one-PR fix that buys 90% of the dispatcher headroom without
the policy work."
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Helpers and source pins
# ===========================================================================


def test_m3_app_state_initialises_caller_agent_cache() -> None:
    """The lifespan must populate `state.caller_agent_cache` before
    any router code runs — a missing attribute would force
    `_handle_progress` and the cache helpers down their None
    fallback path forever, which is correct but defeats the
    purpose of the fix.

    R8 made the cache a `BoundedLRUDict` (bounded LRU) rather
    than a plain dict to fix the multi-worker leak surface. Pin
    on attribute existence + the cache class, not the literal
    `= {}`."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module.lifespan)
    assert "caller_agent_cache" in src
    # R8: bounded LRU rather than unbounded dict.
    assert "BoundedLRUDict" in src


def test_m3_cache_helper_exists_and_handles_none_state_gracefully() -> None:
    """`_cache_caller_agent(state, task_id, caller_agent_id)` must
    exist and be a no-op when state has no `caller_agent_cache`
    attribute. Defence so test fixtures that mock state lightly
    don't crash through the helper."""
    from bp_router import tasks as tasks_module

    fn = tasks_module._cache_caller_agent
    sig = inspect.signature(fn)
    assert list(sig.parameters) == [
        "state",
        "task_id",
        "caller_agent_id",
        "active_agent_id",
    ]

    # No-op when state has no cache attribute.
    state_bare = MagicMock(spec=[])
    fn(state_bare, "task-1", "agent-A", "exec-A")  # must not raise


def test_m3_cache_stores_string_for_child_task() -> None:
    """`_cache_caller_agent(state, "task-X", "agent-A")` populates
    `cache["task-X"] = "agent-A"`."""
    from bp_router import tasks as tasks_module

    state = MagicMock()
    state.caller_agent_cache = {}
    tasks_module._cache_caller_agent(state, "task-X", "agent-A", "exec-A")
    assert state.caller_agent_cache == {"task-X": ("agent-A", "exec-A")}


def test_m3_cache_stores_none_for_root_task() -> None:
    """Root tasks (no parent) must still be cached as `None` so
    the next Progress frame from the root short-circuits the SQL.
    Caching `None` as a sentinel is correct: it means "we know
    there's no fan-out target," distinguishable from cache
    absence."""
    from bp_router import tasks as tasks_module

    state = MagicMock()
    state.caller_agent_cache = {}
    tasks_module._cache_caller_agent(state, "root-task", None, "exec-R")
    assert state.caller_agent_cache == {"root-task": (None, "exec-R")}


# ===========================================================================
# Eviction on terminal state
# ===========================================================================


def test_m3_notify_task_terminal_evicts_cache_entry() -> None:
    """`_notify_task_terminal` was extended to also evict the
    parent-agent cache entry (review item M-3). A terminal task
    will never emit another Progress frame, so the cached fan-out
    target is dead weight."""
    from bp_router import tasks as tasks_module

    state = MagicMock()
    state.task_terminal_events = {}
    state.caller_agent_cache = {"task-X": "agent-A", "task-Y": "agent-B"}

    tasks_module._notify_task_terminal(state, "task-X")

    assert "task-X" not in state.caller_agent_cache
    # Other entries untouched.
    assert state.caller_agent_cache == {"task-Y": "agent-B"}


def test_m3_notify_task_terminal_safe_when_cache_missing_attr() -> None:
    """Defence: `_notify_task_terminal` must not crash if the state
    lacks `caller_agent_cache` (e.g. during partial test setup)."""
    from bp_router import tasks as tasks_module

    state = MagicMock(spec=[])
    tasks_module._notify_task_terminal(state, "task-X")  # must not raise


def test_m3_notify_task_terminal_safe_when_task_not_in_cache() -> None:
    """Eviction of a task that was never cached must be a no-op,
    not a KeyError. (`pop(..., None)` semantics.)"""
    from bp_router import tasks as tasks_module

    state = MagicMock()
    state.task_terminal_events = {}
    state.caller_agent_cache = {}
    tasks_module._notify_task_terminal(state, "missing-task")
    assert state.caller_agent_cache == {}


# ===========================================================================
# admit_task populates the cache
# ===========================================================================


def test_m3_admit_task_calls_cache_caller_agent_after_commit() -> None:
    """Source pin: `admit_task` must invoke `_cache_caller_agent`
    AFTER the inner `conn.transaction()` block (so the cache only
    populates for committed tasks). Pin the call site so a future
    refactor that drops the populate step is caught immediately."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.admit_task)
    assert "_cache_caller_agent" in src, (
        "review3-M3 regression: admit_task no longer populates the cache"
    )
    # Ordering: cache call must be AFTER the inner transaction block.
    cache_idx = src.find("_cache_caller_agent")
    txn_idx = src.find("async with conn.transaction()")
    assert txn_idx < cache_idx, (
        "review3-M3: cache populate must follow the transaction commit "
        "(otherwise an aborted admit could leave a stale cache entry)"
    )


def test_m3_admit_task_caches_caller_unconditionally() -> None:
    """Source pin: admit_task must populate the cache with the
    caller's id for EVERY task — including root tasks. The old
    "cache None for root" path silently dropped Progress and
    Result frames bound for channel agents (webapp, telegram)
    that issued root tasks.
    """
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.admit_task)
    assert "_cache_caller_agent" in src
    # No conditional that nulls the caller for root tasks.
    assert "if frame.parent_task_id is not None else None" not in src


# ===========================================================================
# _handle_progress reads from the cache and back-fills on miss
# ===========================================================================


def test_m3_handle_progress_uses_cache_when_present() -> None:
    """Behavioural: when `state.caller_agent_cache` already has an
    entry for the frame's task, `_handle_progress` must not touch
    the DB pool — the cache lookup is the entire fast path."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache = {"task-X": ("agent-A", "exec-A")}
        state = MagicMock()
        state.caller_agent_cache = cache
        # If anything touches the pool, fail loudly.
        state.db_pool = MagicMock()
        state.db_pool.acquire.side_effect = AssertionError(
            "review3-M3 regression: cache hit should NOT acquire DB"
        )
        state.socket_registry = MagicMock()
        # No agent socket registered → fanout_frame is a no-op,
        # which is fine for this test (we just want to confirm no DB).
        state.socket_registry.get.return_value = None

        entry = MagicMock()
        entry.agent_id = "exec-A"  # the task's active executor (authz)
        frame = ProgressFrame(
            agent_id="agent-A-child",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="task-X",
            event="status",
            content="halfway",
        )
        await dispatch._handle_progress(state, entry, frame)
        # Cache untouched.
        assert cache == {"task-X": ("agent-A", "exec-A")}

    asyncio.run(_scenario())


def test_m3_handle_progress_short_circuits_on_cached_none() -> None:
    """Cache hit with value `None` (root task) must NOT do a SQL
    round-trip AND must NOT call fanout_frame. Pin both so a
    regression that re-queries on `None` is caught."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache = {"root-task": (None, "root-exec")}
        state = MagicMock()
        state.caller_agent_cache = cache
        state.db_pool = MagicMock()
        state.db_pool.acquire.side_effect = AssertionError(
            "review3-M3: cached None should NOT trigger a DB query"
        )

        entry = MagicMock()
        entry.agent_id = "root-exec"  # passes authz; caller is None
        frame = ProgressFrame(
            agent_id="root-agent",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="root-task",
            event="status",
            content="halfway",
        )
        # Replace fanout_frame so we can assert it wasn't called.
        called: list[Any] = []
        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod.fanout_frame

        def _spy(*args: Any, **kwargs: Any) -> int:
            called.append((args, kwargs))
            return 0

        dispatch_mod.fanout_frame = _spy
        try:
            await dispatch._handle_progress(state, entry, frame)
        finally:
            dispatch_mod.fanout_frame = original

        assert called == [], (
            "fanout_frame was called for a cached-None (root) task — "
            "review3-M3: there's no parent agent to fan out to"
        )

    asyncio.run(_scenario())


def test_m3_handle_progress_falls_back_to_sql_on_miss_and_backfills() -> None:
    """Cache miss path: SQL fallback runs once, the result is
    back-filled into the cache, and a SECOND Progress frame for
    the same task is now a cache hit."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache: dict[str, Any] = {}
        state = MagicMock()
        state.caller_agent_cache = cache

        # Mock the pool / connection / fetchrow. The `state` field
        # was added by review4-M1 — non-terminal so the back-fill
        # path is exercised.
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "state": "RUNNING",
                "parent_task_id": "parent-task",
                "caller_agent_id": "agent-A",
                "active_agent_id": "reporting-agent",
            }
        )
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state.db_pool = pool

        # Spy on fanout_frame.
        fanout_calls: list[Any] = []
        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod.fanout_frame

        def _spy(state: Any, agent_ids: list[str], frame: Any) -> int:
            fanout_calls.append((agent_ids, frame))
            return len(agent_ids)

        dispatch_mod.fanout_frame = _spy

        entry = MagicMock()
        entry.agent_id = "reporting-agent"  # == active (authz passes)
        frame = ProgressFrame(
            agent_id="reporting-agent",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="task-Y",
            event="status",
            content="halfway",
        )
        try:
            # First call: cache miss, SQL runs, cache back-filled.
            await dispatch._handle_progress(state, entry, frame)
            assert cache == {"task-Y": ("agent-A", "reporting-agent")}
            assert conn.fetchrow.await_count == 1
            assert fanout_calls == [(["agent-A"], frame)]

            # Second call: cache hit, no SQL.
            conn.fetchrow.reset_mock()
            await dispatch._handle_progress(state, entry, frame)
            assert conn.fetchrow.await_count == 0
            assert len(fanout_calls) == 2
        finally:
            dispatch_mod.fanout_frame = original

    asyncio.run(_scenario())


def test_m3_handle_progress_missing_task_does_not_cache_miss() -> None:
    """If the SQL fallback returns `None` (task doesn't exist —
    race with admit, or bogus task_id), do NOT cache that result.
    A future admit could populate the row; caching the miss would
    permanently break Progress fan-out for that task_id."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache: dict[str, Any] = {}
        state = MagicMock()
        state.caller_agent_cache = cache

        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state.db_pool = pool

        entry = MagicMock()
        frame = ProgressFrame(
            agent_id="reporting-agent",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="ghost-task",
            event="status",
        )
        await dispatch._handle_progress(state, entry, frame)

        # Cache must remain empty — no negative caching.
        assert cache == {}, (
            "review3-M3 regression: missing task was negatively cached, "
            "permanently breaking Progress fan-out if the row appears later"
        )

    asyncio.run(_scenario())


def test_m3_handle_progress_works_when_state_has_no_cache_attr() -> None:
    """Defence: a state without `caller_agent_cache` (e.g. a stub
    test that doesn't initialise the lifespan) must still work —
    just falls through to SQL every time."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        state = MagicMock(spec=["db_pool", "socket_registry"])
        # No caller_agent_cache attr — getattr returns None.

        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "parent_task_id": None,
            "caller_agent_id": None,
            "active_agent_id": None,
            "state": "RUNNING",
        })
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state.db_pool = pool

        entry = MagicMock()
        frame = ProgressFrame(
            agent_id="x",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="task-Z",
            event="status",
        )
        # Must not raise.
        await dispatch._handle_progress(state, entry, frame)

    asyncio.run(_scenario())


def test_m3_handle_progress_drops_unauthorized_emitter() -> None:
    """H11 regression: a Progress frame whose SENDER is not the
    task's active executor is dropped (no fan-out). Without this,
    any connected agent could spoof `Progress{task_id=X}` and inject
    attacker-controlled content into another tenant's caller stream
    — the asymmetry vs the Result authz in `complete_task`."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        # Cached: caller=caller-A, active executor=exec-A.
        state = MagicMock()
        state.caller_agent_cache = {"task-X": ("caller-A", "exec-A")}
        state.db_pool = MagicMock()
        state.db_pool.acquire.side_effect = AssertionError(
            "cache hit must not touch the DB"
        )

        entry = MagicMock()
        entry.agent_id = "evil-agent"  # NOT the active executor
        frame = ProgressFrame(
            agent_id="evil-agent",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="task-X",
            event="status",
            content="<spoofed>",
        )
        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod.fanout_frame
        called: list[Any] = []
        dispatch_mod.fanout_frame = (
            lambda *a, **k: called.append((a, k)) or 0
        )
        try:
            await dispatch._handle_progress(state, entry, frame)
        finally:
            dispatch_mod.fanout_frame = original
        assert called == [], (
            "H11 regression: Progress from a non-active agent was "
            "fanned out — cross-tenant content injection"
        )

    asyncio.run(_scenario())
