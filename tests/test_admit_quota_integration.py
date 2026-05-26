"""Integration tests for the admit-time per-user rate quota
(WS-H3 / `docs/design/quota-enforcement.md`).

Verifies that `admit_task` walks through:
  - the per-(user_id, level) bucket lookup,
  - the rate-vs-burst short-circuit when the level has no cap,
  - the metric increment,
  - the typed `AdmitError("quota_exceeded")` with `retry_after_s`
    populated for the admin API to translate to `Retry-After`.

The bucket itself is exercised against a real Redis-protocol
server via `fakeredis` (see `tests/test_redis_integration.py`);
this file pins the wire-up between `admit_task` and the bucket
helper.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Reuse the stub-builder helpers from the depth-cap suite — they
# already produce a fake `state` + `frame` of the right shape.
from tests.test_review_transport_robustness import (
    _agent,
    _new_task_frame,
)


def _make_state_with_quota(
    *,
    rate_per_s: float | None,
    burst: int | None,
    bucket: Any | None = None,
) -> Any:
    """Build a fake `state` with per-tier quota knobs wired up.

    Mirrors `_make_state` from test_review_transport_robustness but
    additionally populates `state.admit_quota`. When `bucket` is
    None, the quota is gated to 'no cap' so the short-circuit fires
    and admit_task never touches the bucket; useful for the
    happy-path test."""
    state = MagicMock()
    state.settings.spawn_max_depth = 16
    state.settings.default_task_deadline_s = 300
    state.settings.quota_admit_rate_per_s = {"tier0": rate_per_s}
    state.settings.quota_admit_burst = {"tier0": burst}
    state.rules.rules = []
    state.admit_quota = bucket
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"closed_at": None})
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return state


def _wire_admit_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every `admit_task` dependency before the quota check
    so the test exercises ONLY the quota gate."""
    from bp_router import tasks

    monkeypatch.setattr(
        tasks.queries,
        "get_agent",
        AsyncMock(side_effect=[_agent("agt_caller"), _agent("agt_callee")]),
    )
    monkeypatch.setattr(tasks, "_session_level", AsyncMock(return_value="tier0"))
    monkeypatch.setattr(
        tasks, "is_allowed_for",
        lambda *a, **kw: type("D", (), {"allow": True, "rule_name": "stub"})(),
    )


def test_quota_short_circuits_when_level_uncapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller's level has `rate_per_s=None`, admit_task
    MUST NOT call into the bucket helper. Pin so a future refactor
    doesn't accidentally start paying a Redis round-trip per
    admin/service admit (where the cap is intentionally None).

    Drives the next gate (spawn-depth) into an explicit failure so
    we can assert "we got past the quota gate" without standing up
    the full DB-transaction path."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    _wire_admit_stubs(monkeypatch)
    monkeypatch.setattr(
        tasks.queries, "count_task_chain_depth",
        AsyncMock(return_value=999),
    )

    bucket = MagicMock()
    # Make `try_consume` blow up if it ever runs — proves the
    # short-circuit fires before the call.
    bucket.try_consume = AsyncMock(side_effect=AssertionError(
        "try_consume must NOT be called when level is uncapped"
    ))

    state = _make_state_with_quota(rate_per_s=None, burst=None, bucket=bucket)
    frame = _new_task_frame(parent_task_id="tsk_deep")

    with pytest.raises(tasks.AdmitError) as excinfo:
        asyncio.run(tasks.admit_task(state, frame, caller_agent_id="agt_caller"))
    # We got past the quota gate (no AssertionError from the bucket)
    # and into the depth gate.
    assert excinfo.value.code == "spawn_depth_exceeded"
    bucket.try_consume.assert_not_called()


def test_quota_denied_raises_quota_exceeded_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket says 'no' → `AdmitError("quota_exceeded")` with
    `retry_after_s` populated. Surfaces the metric increment too."""
    pytest.importorskip("fastapi")
    from bp_router import tasks
    from bp_router.observability import metrics
    from bp_router.security.rate_limit import Decision

    _wire_admit_stubs(monkeypatch)

    bucket = MagicMock()
    bucket.try_consume = AsyncMock(return_value=Decision(
        allowed=False, retry_after_s=0.42, tokens_remaining=0.0,
    ))

    state = _make_state_with_quota(
        rate_per_s=20.0, burst=40, bucket=bucket,
    )
    frame = _new_task_frame()

    # Snapshot the metric counter so we can prove we incremented it.
    before = metrics.quota_exceeded_total.labels(
        counter="admit_rate", level="tier0"
    )._value.get()  # type: ignore[attr-defined]

    with pytest.raises(tasks.AdmitError) as excinfo:
        asyncio.run(tasks.admit_task(state, frame, caller_agent_id="agt_caller"))

    err = excinfo.value
    assert err.code == "quota_exceeded"
    assert err.retry_after_s == pytest.approx(0.42)
    # The error message includes the level so admins can spot
    # which tier is hot in the response body without going to
    # metrics.
    assert "tier0" in err.message

    # Bucket key shape pin: `<user_id>:<level>` so per-user buckets
    # at the same tier don't share state. Catches a regression that
    # accidentally keys on level alone.
    call_args = bucket.try_consume.call_args
    assert call_args[0][0] == "usr_alice:tier0"
    assert call_args.kwargs["rate_per_s"] == 20.0
    assert call_args.kwargs["burst"] == 40

    # Metric incremented exactly once.
    after = metrics.quota_exceeded_total.labels(
        counter="admit_rate", level="tier0"
    )._value.get()  # type: ignore[attr-defined]
    assert after == before + 1


def test_quota_allowed_continues_to_depth_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket says 'yes' → admit_task continues past the
    quota gate. We can't easily assert the full-success path
    without standing up the whole DB transaction; instead, pin
    that the next downstream gate (depth check) is reached by
    making it the explicit failure."""
    pytest.importorskip("fastapi")
    from bp_router import tasks
    from bp_router.security.rate_limit import Decision

    _wire_admit_stubs(monkeypatch)
    # Force depth check to trip — proves we got past the quota.
    monkeypatch.setattr(
        tasks.queries, "count_task_chain_depth",
        AsyncMock(return_value=999),
    )

    bucket = MagicMock()
    bucket.try_consume = AsyncMock(return_value=Decision(
        allowed=True, retry_after_s=0.0, tokens_remaining=39.0,
    ))

    state = _make_state_with_quota(
        rate_per_s=20.0, burst=40, bucket=bucket,
    )
    frame = _new_task_frame(parent_task_id="tsk_deep")

    with pytest.raises(tasks.AdmitError) as excinfo:
        asyncio.run(tasks.admit_task(state, frame, caller_agent_id="agt_caller"))
    # Depth gate fired (proving we got past the quota gate).
    assert excinfo.value.code == "spawn_depth_exceeded"
    bucket.try_consume.assert_awaited_once()


def test_admin_endpoint_maps_quota_exceeded_to_429() -> None:
    """Source pin: the admin `/v1/admin/tasks/test` handler must
    map `quota_exceeded` to HTTP 429 and emit a `Retry-After`
    header from `AdmitError.retry_after_s`. Catches a regression
    that drops the new code from the status_for_code dict."""
    import inspect

    from bp_router.api import admin as admin_module

    src = inspect.getsource(admin_module.test_task)
    assert '"quota_exceeded": 429' in src or "'quota_exceeded': 429" in src, (
        "admin test_task no longer maps quota_exceeded to 429"
    )
    # Retry-After header wired from AdmitError.retry_after_s.
    assert "Retry-After" in src
    assert "retry_after_s" in src
