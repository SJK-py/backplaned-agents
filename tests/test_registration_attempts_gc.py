"""Tests for the registration_attempts sweep loop.

Mirrors the session_gc / file_gc test patterns — source pins on
the loop shape and the helper's SQL, plus a wiring check that the
new loop joins the existing `start_background_loops` set.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# Query helper
# ===========================================================================


def test_gc_registration_attempts_query_exists() -> None:
    from bp_router.db import queries

    assert hasattr(queries, "gc_registration_attempts")


def test_gc_registration_attempts_takes_cutoff_kwarg() -> None:
    from bp_router.db import queries

    sig = inspect.signature(queries.gc_registration_attempts)
    assert "cutoff" in sig.parameters
    # Keyword-only — callers can't pass a positional cutoff and
    # confuse it with the connection.
    assert sig.parameters["cutoff"].kind == inspect.Parameter.KEYWORD_ONLY


def test_gc_registration_attempts_deletes_by_attempted_at() -> None:
    """Source pin: the WHERE clause uses `attempted_at < $1`. The
    sliding-window indexed-DESC column on the table — anything
    else would either scan the whole table or be a typo."""
    from bp_router.db import queries

    src = inspect.getsource(queries.gc_registration_attempts)
    assert "DELETE FROM registration_attempts" in src
    assert "attempted_at < $1" in src


def test_gc_registration_attempts_returns_deletion_count() -> None:
    """The asyncpg DELETE result string is `DELETE n`; we parse
    the second token. Useful for the GC log line + tests."""
    from bp_router.db import queries

    src = inspect.getsource(queries.gc_registration_attempts)
    assert 'result.split()' in src
    assert "int(parts[1])" in src


# ===========================================================================
# Loop / GC wrapper
# ===========================================================================


def test_registration_attempts_gc_loop_exists() -> None:
    from bp_router import tasks

    assert hasattr(tasks, "registration_attempts_gc_loop")


def test_gc_loop_defaults_one_hour_interval_thirty_day_retention() -> None:
    """Same defaults as session_gc_loop — hourly tick, 30-day
    retention. Operators can change in code if they need either
    end of the trade-off."""
    from bp_router import tasks

    sig = inspect.signature(tasks.registration_attempts_gc_loop)
    assert sig.parameters["interval_s"].default == 3_600.0
    assert sig.parameters["retention_days"].default == 30


def test_gc_loop_exits_cleanly_on_cancellation() -> None:
    """Source pin: the loop catches CancelledError and returns —
    NOT logs+continues. Without this the loop survives a graceful
    shutdown and the app hangs on drain."""
    from bp_router import tasks

    src = inspect.getsource(tasks.registration_attempts_gc_loop)
    assert "except asyncio.CancelledError:" in src
    assert "return" in src


def test_gc_loop_swallows_unexpected_exceptions() -> None:
    """A transient DB error must not kill the loop — the next tick
    retries. Pin the broad except + the exception log so a future
    refactor that surfaces unhandled errors is caught."""
    from bp_router import tasks

    src = inspect.getsource(tasks.registration_attempts_gc_loop)
    assert "except Exception:" in src
    assert "registration_attempts_gc_failed" in src


def test_gc_wrapper_uses_db_pool_with_acquired_conn() -> None:
    """One acquired connection per sweep, not per query — matches
    the session/file gc patterns."""
    from bp_router import tasks

    src = inspect.getsource(tasks._gc_registration_attempts)
    assert "pool.acquire()" in src
    assert "queries.gc_registration_attempts" in src


def test_gc_wrapper_logs_deletion_count_on_nonzero() -> None:
    """Quiet on no-ops (`if deleted:`), informative when the
    sweep does work."""
    from bp_router import tasks

    src = inspect.getsource(tasks._gc_registration_attempts)
    assert "if deleted:" in src
    assert "registration_attempts_gc" in src
    assert '"deleted": deleted' in src


def test_gc_wrapper_computes_cutoff_from_retention_days() -> None:
    """Pin the cutoff arithmetic — `_now() - timedelta(days=...)`.
    Without this, a future refactor that picks a fixed date would
    silently retain forever."""
    from bp_router import tasks

    src = inspect.getsource(tasks._gc_registration_attempts)
    assert "_now() - timedelta(days=retention_days)" in src


# ===========================================================================
# Wiring
# ===========================================================================


def test_start_background_loops_includes_registration_attempts_gc() -> None:
    """The loop must be added to the startup set; otherwise it
    never runs."""
    from bp_router import tasks

    src = inspect.getsource(tasks.start_background_loops)
    assert "registration_attempts_gc_loop" in src
    # Co-located with the other gc loops (defensive against an
    # accidental move out of the start set).
    assert "session_gc_loop" in src
    assert "file_gc_loop" in src
