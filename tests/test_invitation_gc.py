"""Tests for the invitation sweep loop.

Mirrors the registration_attempts_gc / session_gc test patterns — source
pins on the loop shape and the helper's SQL, plus a wiring check that the
new loop joins the existing `start_background_loops` set.

The invitations table is high-churn now (the suite mints a fresh single-use
token per agent on every launch, 10-min TTL), so dead rows must be GC-ed or
the table grows without bound.
"""

from __future__ import annotations

import inspect

# ===========================================================================
# Query helper
# ===========================================================================


def test_gc_expired_invitations_query_exists() -> None:
    from bp_router.db import queries

    assert hasattr(queries, "gc_expired_invitations")


def test_gc_expired_invitations_takes_cutoff_kwarg() -> None:
    from bp_router.db import queries

    sig = inspect.signature(queries.gc_expired_invitations)
    assert "cutoff" in sig.parameters
    # Keyword-only — callers can't pass a positional cutoff and confuse it
    # with the connection.
    assert sig.parameters["cutoff"].kind == inspect.Parameter.KEYWORD_ONLY


def test_gc_expired_invitations_spares_live_tokens() -> None:
    """Source pin: the WHERE clause is `COALESCE(used_at, expires_at) < $1`.

    This is the load-bearing safety property — a LIVE invitation (unused AND
    unexpired) has a FUTURE `expires_at`, so COALESCE yields that future
    instant, which sorts after any past cutoff and is never deleted. A
    refactor to `expires_at < $1` alone would delete unused tokens the moment
    they expire (losing the retention window), and one to `used_at < $1` would
    never reap expired-unused rows. Pin the exact expression."""
    from bp_router.db import queries

    src = inspect.getsource(queries.gc_expired_invitations)
    assert "DELETE FROM invitations" in src
    assert "COALESCE(used_at, expires_at) < $1" in src


def test_gc_expired_invitations_returns_deletion_count() -> None:
    """The asyncpg DELETE result string is `DELETE n`; we parse the second
    token for the GC log line + tests."""
    from bp_router.db import queries

    src = inspect.getsource(queries.gc_expired_invitations)
    assert "result.split()" in src
    assert "int(parts[1])" in src


# ===========================================================================
# Loop / GC wrapper
# ===========================================================================


def test_invitation_gc_loop_exists() -> None:
    from bp_router import tasks

    assert hasattr(tasks, "invitation_gc_loop")


def test_invitation_gc_loop_defaults_hourly_seven_day_retention() -> None:
    """Hourly tick like the other GC loops, but a SHORTER 7-day retention —
    invitations are high-churn (fresh per launch) and the auth audit log
    already records onboard outcomes, so we don't keep 30 days of dead
    tokens."""
    from bp_router import tasks

    sig = inspect.signature(tasks.invitation_gc_loop)
    assert sig.parameters["interval_s"].default == 3_600.0
    assert sig.parameters["retention_days"].default == 7


def test_invitation_gc_loop_exits_cleanly_on_cancellation() -> None:
    """Source pin: the loop catches CancelledError and returns — NOT
    logs+continues — so a graceful shutdown can drain."""
    from bp_router import tasks

    src = inspect.getsource(tasks.invitation_gc_loop)
    assert "except asyncio.CancelledError:" in src
    assert "return" in src


def test_invitation_gc_loop_swallows_unexpected_exceptions() -> None:
    """A transient DB error must not kill the loop — the next tick retries."""
    from bp_router import tasks

    src = inspect.getsource(tasks.invitation_gc_loop)
    assert "except Exception:" in src
    assert "invitation_gc_failed" in src


def test_gc_wrapper_uses_db_pool_with_acquired_conn() -> None:
    """One acquired connection per sweep — matches the other gc patterns."""
    from bp_router import tasks

    src = inspect.getsource(tasks._gc_expired_invitations)
    assert "pool.acquire()" in src
    assert "queries.gc_expired_invitations" in src


def test_gc_wrapper_logs_deletion_count_on_nonzero() -> None:
    """Quiet on no-ops (`if deleted:`), informative when it does work."""
    from bp_router import tasks

    src = inspect.getsource(tasks._gc_expired_invitations)
    assert "if deleted:" in src
    assert "invitation_gc" in src
    assert '"deleted": deleted' in src


def test_gc_wrapper_computes_cutoff_from_retention_days() -> None:
    """Pin the cutoff arithmetic — `_now() - timedelta(days=...)`."""
    from bp_router import tasks

    src = inspect.getsource(tasks._gc_expired_invitations)
    assert "_now() - timedelta(days=retention_days)" in src


# ===========================================================================
# Wiring
# ===========================================================================


def test_start_background_loops_includes_invitation_gc() -> None:
    """The loop must be added to the startup set; otherwise it never runs."""
    from bp_router import tasks

    src = inspect.getsource(tasks.start_background_loops)
    assert "invitation_gc_loop" in src
    # Co-located with the other gc loops.
    assert "registration_attempts_gc_loop" in src
    assert "session_gc_loop" in src
