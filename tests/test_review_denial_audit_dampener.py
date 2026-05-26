"""Per-actor dampener bounds denial-audit writes.

R4 second-pass review found that the three denial paths on
admin mint endpoints write one hash-chained `audit_log` row
per attempt, with no per-actor cap:

  * `service_mint_refresh_token` → `auth.refresh_token_mint_denied`
    (caller's user_id not in `target.serviced_by`)
  * `mint_password_reset_token` → `auth.password_reset_mint_denied`
    (same gate)
  * `_enforce_per_target_mint_rate_limit` →
    `auth.<endpoint>_rate_limited` (per-target bucket saturated)

Each `append_audit_event` takes `pg_advisory_xact_lock` for the
hash chain. A hostile or misconfigured service principal pounding
the endpoint at 100/s pushed 100 audit writes/s through that
single advisory lock, serializing every legitimate audit write
behind it.

Fix: `_denial_audit_allowed(state, actor, event)` per-actor
dampener (0.1/s, burst 10) gates each denial-audit write.
Drops increment `router_audit_denials_dropped_total{event}` so
operators see the rate.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_denial_audit_dampener_metric_registered() -> None:
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import audit_denials_dropped_total

    assert (
        audit_denials_dropped_total._name  # type: ignore[attr-defined]
        == "router_audit_denials_dropped"
    )
    # `.labels(event=...)` must not raise.
    audit_denials_dropped_total.labels(event="auth.refresh_token_mint_denied")


def test_dampener_returns_true_when_bucket_allows() -> None:
    """Allowed path: the bucket grants the consume, helper returns
    True, caller proceeds to write the audit row."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _denial_audit_allowed

    state = MagicMock()
    decision = MagicMock()
    decision.allowed = True
    state.login_quota.try_consume = AsyncMock(return_value=decision)

    out = asyncio.run(
        _denial_audit_allowed(state, "usr_a", "auth.something_denied")
    )
    assert out is True
    # Bucket key includes both event and actor — distinct events
    # for the same actor do NOT share a bucket (an admin tripping
    # not_serviced_by on one endpoint shouldn't dampen denial
    # audits on a different endpoint).
    call = state.login_quota.try_consume.await_args
    assert call.args[0] == "audit_denial:auth.something_denied:usr_a"


def test_dampener_returns_false_when_bucket_denies() -> None:
    """Denied path: bucket saturated → helper returns False; caller
    SKIPs the audit append but still raises the underlying
    HTTPException."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _denial_audit_allowed

    state = MagicMock()
    decision = MagicMock()
    decision.allowed = False
    state.login_quota.try_consume = AsyncMock(return_value=decision)

    out = asyncio.run(
        _denial_audit_allowed(state, "usr_a", "auth.something_denied")
    )
    assert out is False


def test_dampener_uses_event_specific_bucket() -> None:
    """Two different events for the same actor must NOT share a
    bucket. Otherwise a `not_serviced_by` denial on the refresh
    endpoint would dampen the legitimate rate-limit denial on
    the password-reset endpoint."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _denial_audit_allowed

    state = MagicMock()
    decision = MagicMock()
    decision.allowed = True
    state.login_quota.try_consume = AsyncMock(return_value=decision)

    asyncio.run(_denial_audit_allowed(state, "usr_a", "event_x"))
    asyncio.run(_denial_audit_allowed(state, "usr_a", "event_y"))

    # Two separate consumes against two separate keys.
    keys = [
        c.args[0]
        for c in state.login_quota.try_consume.await_args_list
    ]
    assert keys == [
        "audit_denial:event_x:usr_a",
        "audit_denial:event_y:usr_a",
    ]


def test_dampener_handles_none_actor_id() -> None:
    """Unauthenticated path edge case — `actor_user_id=None`
    shouldn't crash the dampener. Routes to a shared `anon`
    bucket."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _denial_audit_allowed

    state = MagicMock()
    decision = MagicMock()
    decision.allowed = True
    state.login_quota.try_consume = AsyncMock(return_value=decision)

    asyncio.run(_denial_audit_allowed(state, None, "event_x"))
    call = state.login_quota.try_consume.await_args
    assert call.args[0] == "audit_denial:event_x:anon"


def test_dampener_constants_are_sane() -> None:
    """Source pin: rate is tight enough to bound abuse but not
    so tight it suppresses legit admin denials. 0.1/s with
    burst 10 absorbs ~10 attempts immediately, then 1 per 10s
    sustained — enough for a typo-prone admin, not enough to
    audit-spam a 100/s flood."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import (
        _AUDIT_DENIAL_DAMPENER_BURST,
        _AUDIT_DENIAL_DAMPENER_RATE_PER_S,
    )

    assert 0.05 <= _AUDIT_DENIAL_DAMPENER_RATE_PER_S <= 1.0
    assert 5 <= _AUDIT_DENIAL_DAMPENER_BURST <= 50


def test_service_mint_refresh_token_uses_dampener() -> None:
    """Source pin: the not_serviced_by denial in
    `service_mint_refresh_token` is gated on the dampener."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.service_mint_refresh_token)
    assert "_denial_audit_allowed(" in src
    assert '"auth.refresh_token_mint_denied"' in src


def test_mint_password_reset_token_uses_dampener() -> None:
    """Same for password-reset denial."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert "_denial_audit_allowed(" in src
    assert '"auth.password_reset_mint_denied"' in src


def test_rate_limit_helper_uses_dampener() -> None:
    """The per-target rate-limit DENY path also writes audit
    per-attempt (the bucket can saturate hard against repeated
    hits). Confirm it routes through the dampener too."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin._enforce_per_target_mint_rate_limit)
    assert "_denial_audit_allowed(" in src


def test_dampener_drop_increments_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the bucket denies, the helper bumps the dropped-counter
    so operators can graph the rate of suppressed audits."""
    pytest.importorskip("fastapi")
    pytest.importorskip("prometheus_client")
    from bp_router.api.admin import _denial_audit_allowed
    from bp_router.observability.metrics import audit_denials_dropped_total

    state = MagicMock()
    decision = MagicMock()
    decision.allowed = False
    state.login_quota.try_consume = AsyncMock(return_value=decision)

    # Snapshot the current count for this label.
    before = audit_denials_dropped_total.labels(
        event="auth.refresh_token_mint_denied"
    )._value.get()  # type: ignore[attr-defined]

    asyncio.run(
        _denial_audit_allowed(
            state, "usr_a", "auth.refresh_token_mint_denied"
        )
    )

    after = audit_denials_dropped_total.labels(
        event="auth.refresh_token_mint_denied"
    )._value.get()  # type: ignore[attr-defined]
    assert after == before + 1
