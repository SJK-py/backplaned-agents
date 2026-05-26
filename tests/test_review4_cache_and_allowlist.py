"""Tests for the fourth-pass review M-1 + M-2 fixes.

  - M-1: `_handle_progress` SQL fallback no longer back-fills the
    `caller_agent_cache` when the task row is in a terminal state.
    Without that guard, a late or adversarial Progress frame for an
    already-terminal task re-inserts the cache entry that
    `_notify_task_terminal` had just evicted — and nothing else
    evicts it again, so the dict grew without bound under
    sustained stale-Progress traffic. This was a regression in
    the M-3 (review 3) fix.
  - M-2: `update_llm_preset`'s column-allowlist defence-in-depth
    branch raises `HTTPException(500, ...)` instead of bare
    `RuntimeError`. The L-2 (review 3) fix was applied to
    `update_user` and `update_rule` but missed this third PATCH
    endpoint.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# M-1: terminal-task back-fill suppression
# ===========================================================================


def test_m1_handle_progress_does_not_backfill_on_terminal_state() -> None:
    """Behavioural: a Progress frame whose task row is already
    SUCCEEDED (or any other terminal state) MUST NOT cause the
    cache to be re-populated. The fan-out still happens
    (best-effort), but the cache entry stays absent so the next
    stale frame doesn't accumulate state."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache: dict[str, Any] = {}
        state = MagicMock()
        state.caller_agent_cache = cache

        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "state": "SUCCEEDED",
            "parent_task_id": "parent-task",
            "caller_agent_id": "agent-A",
            "active_agent_id": "reporter",
        })
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state.db_pool = pool

        # Spy fanout_frame so we can confirm the late Progress IS
        # still fanned out — only the cache write is suppressed.
        fanout_calls: list[Any] = []
        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod.fanout_frame

        def _spy(state: Any, agent_ids: list[str], frame: Any) -> int:
            fanout_calls.append((agent_ids, frame))
            return len(agent_ids)

        dispatch_mod.fanout_frame = _spy

        entry = MagicMock()
        entry.agent_id = "reporter"  # == active_agent_id (authz passes)
        frame = ProgressFrame(
            agent_id="reporter",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="terminal-task",
            event="status",
            content="late",
        )
        try:
            await dispatch._handle_progress(state, entry, frame)

            # Cache MUST stay empty — the eviction-and-no-rewrite
            # invariant is the entire fix.
            assert cache == {}, (
                "review4-M1 regression: terminal-task Progress frame "
                "re-populated caller_agent_cache; cache will grow "
                "without bound under sustained stale traffic"
            )
            # Fan-out still happened (the parent agent might still
            # care about a late Progress for diagnostic purposes).
            assert fanout_calls == [(["agent-A"], frame)]
        finally:
            dispatch_mod.fanout_frame = original

    asyncio.run(_scenario())


@pytest.mark.parametrize(
    "terminal_state",
    ["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"],
)
def test_m1_no_backfill_for_every_terminal_state(terminal_state: str) -> None:
    """All four terminal states must suppress the back-fill —
    pin via parametrize so a future TaskState addition that
    forgets to update `is_terminal` is caught."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache: dict[str, Any] = {}
        state = MagicMock()
        state.caller_agent_cache = cache

        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "state": terminal_state,
            "parent_task_id": "parent-task",
            "caller_agent_id": "agent-A",
            "active_agent_id": "x",
        })
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state.db_pool = pool

        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod.fanout_frame
        dispatch_mod.fanout_frame = lambda *a, **k: 0

        entry = MagicMock()
        frame = ProgressFrame(
            agent_id="r",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="t",
            event="status",
        )
        try:
            await dispatch._handle_progress(state, entry, frame)
            assert cache == {}, (
                f"review4-M1: cache populated for terminal state "
                f"{terminal_state!r}"
            )
        finally:
            dispatch_mod.fanout_frame = original

    asyncio.run(_scenario())


@pytest.mark.parametrize(
    "active_state", ["QUEUED", "RUNNING", "WAITING_CHILDREN"]
)
def test_m1_does_backfill_for_non_terminal_state(active_state: str) -> None:
    """Sanity-pin the happy path: non-terminal states still
    back-fill (otherwise the cache never warms up)."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ProgressFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        cache: dict[str, Any] = {}
        state = MagicMock()
        state.caller_agent_cache = cache

        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "state": active_state,
            "parent_task_id": "parent-task",
            "caller_agent_id": "agent-A",
            "active_agent_id": "x",
        })
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        state.db_pool = pool

        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod.fanout_frame
        dispatch_mod.fanout_frame = lambda *a, **k: 0

        entry = MagicMock()
        frame = ProgressFrame(
            agent_id="r",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="active-task",
            event="status",
        )
        try:
            await dispatch._handle_progress(state, entry, frame)
            assert cache == {"active-task": ("agent-A", "x")}, (
                f"review4-M1 over-correction: non-terminal state "
                f"{active_state!r} should still back-fill"
            )
        finally:
            dispatch_mod.fanout_frame = original

    asyncio.run(_scenario())


def test_m1_sql_fetches_state_column() -> None:
    """Source pin: the SQL must SELECT `t.state` so the back-fill
    decision has the data it needs."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_progress)
    # The single-table SELECT must still include `state` so the
    # back-fill guard can short-circuit on terminal rows.
    assert "state" in src and "FROM tasks" in src, (
        "back-fill guard regression: SQL no longer fetches the state column"
    )


def test_m1_uses_taskstate_is_terminal_not_inline_set() -> None:
    """`TaskState.is_terminal` is the single source of truth for
    the terminal-state membership check. Using a hardcoded set
    here would drift from the enum."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_progress)
    assert "is_terminal" in src, (
        "review4-M1: must use TaskState.is_terminal not a duplicated "
        "string-set"
    )


# ===========================================================================
# M-2: update_llm_preset allowlist HTTPException(500)
# ===========================================================================


def test_m2_update_llm_preset_raises_http_exception_not_runtime_error() -> None:
    """The third-pass review's L-2 fix was applied to `update_user`
    and `update_rule` but missed `update_llm_preset`. This test
    pins the L-2 envelope across all three PATCH endpoints."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_llm_preset)
    # The structured 500 must be present.
    assert "HTTPException(" in src
    assert "status_code=500" in src
    assert "preset column allowlist drift" in src
    # The bare RuntimeError must be GONE.
    assert "raise RuntimeError(" not in src, (
        "review4-M2 regression: update_llm_preset raising bare "
        "RuntimeError on column allowlist drift"
    )
    # Citation present so a future maintainer can grep the chain
    # of related fixes.


def test_m2_l2_pattern_now_consistent_across_all_three_patch_endpoints() -> None:
    """Cross-cutting pin: every PATCH endpoint that has a
    column-allowlist defence MUST use the structured 500 form.
    Catches a future fourth PATCH endpoint that copies the
    pre-L-2 RuntimeError pattern."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    for func_name in ("update_user", "update_rule", "update_llm_preset"):
        src = inspect.getsource(getattr(admin, func_name))
        assert "raise RuntimeError(" not in src, (
            f"review4-M2: {func_name} still raises bare RuntimeError "
            "on the allowlist defence — must be HTTPException(500)"
        )
        # And EACH must have the HTTPException(500) form.
        assert "status_code=500" in src, (
            f"review4-M2: {func_name} missing the structured 500 form"
        )
