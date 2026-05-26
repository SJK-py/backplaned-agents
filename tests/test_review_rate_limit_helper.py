"""`_enforce_per_target_mint_rate_limit` folds the duplicate rate-
limit shape shared by F8 service_mint_refresh_token and F9
mint_password_reset_token. The helper:
  - Computes the bucket key as `<prefix>:user:<target_id>`
  - Consumes one token; allowed → return
  - Denied → writes a per-endpoint audit row + raises 429 with
    Retry-After
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_state(*, allowed: bool, retry_after_s: float = 0.0) -> MagicMock:
    state = MagicMock()
    decision = MagicMock()
    decision.allowed = allowed
    decision.retry_after_s = retry_after_s
    state.login_quota.try_consume = AsyncMock(return_value=decision)
    # Connection-acquire returns a stub connection.
    pool = MagicMock()
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.db_pool = pool
    return state


def test_helper_returns_none_on_allowed() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _enforce_per_target_mint_rate_limit

    state = _make_state(allowed=True)

    out = asyncio.run(
        _enforce_per_target_mint_rate_limit(
            state=state,
            actor_user_id="usr_a",
            target_user_id="usr_t",
            bucket_prefix="prefix",
            rate_per_s=1.0,
            burst=5,
            audit_event="auth.some_event",
            error_detail="not used on allow path",
        )
    )
    assert out is None
    # Bucket key shape.
    state.login_quota.try_consume.assert_awaited_once()
    call = state.login_quota.try_consume.await_args
    assert call.args[0] == "prefix:user:usr_t"
    assert call.kwargs["rate_per_s"] == 1.0
    assert call.kwargs["burst"] == 5


def test_helper_raises_429_on_denied() -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.api.admin import _enforce_per_target_mint_rate_limit

    # Stub queries.append_audit_event so we don't need a real DB.
    from bp_router.db import queries

    state = _make_state(allowed=False, retry_after_s=3.4)
    # The helper now consults try_consume twice: once for the
    # rate-limit bucket (deny → 429), then again for the
    # `_denial_audit_allowed` dampener. Sequence-return so the
    # dampener allows the audit write to fire.
    rl_denied = MagicMock(allowed=False, retry_after_s=3.4)
    dampener_allowed = MagicMock(allowed=True, retry_after_s=0.0)
    state.login_quota.try_consume = AsyncMock(
        side_effect=[rl_denied, dampener_allowed]
    )
    queries.append_audit_event = AsyncMock(return_value=None)  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            _enforce_per_target_mint_rate_limit(
                state=state,
                actor_user_id="usr_a",
                target_user_id="usr_t",
                bucket_prefix="prefix",
                rate_per_s=1.0,
                burst=5,
                audit_event="auth.some_event",
                error_detail="too many widgets",
            )
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "too many widgets"
    # Retry-After is ceil(retry_after_s) = 4 here.
    assert exc_info.value.headers["Retry-After"] == "4"

    # Audit row written with the per-endpoint event name.
    queries.append_audit_event.assert_awaited_once()
    audit_kwargs = queries.append_audit_event.await_args.kwargs
    assert audit_kwargs["event"] == "auth.some_event"
    assert audit_kwargs["target_id"] == "usr_t"
    assert audit_kwargs["actor_id"] == "usr_a"
    assert audit_kwargs["payload"] == {"retry_after_s": 4}


def test_helper_used_at_both_endpoints() -> None:
    """Source pin: both `service_mint_refresh_token` and
    `mint_password_reset_token` route through the helper, passing
    their per-endpoint `BUCKET_*` constant + audit event."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    # service_mint_refresh_token
    src = inspect.getsource(admin.service_mint_refresh_token)
    assert "_enforce_per_target_mint_rate_limit(" in src
    assert "BUCKET_SERVICE_MINT_REFRESH_TOKEN" in src
    assert (
        'audit_event="auth.refresh_token_service_mint_rate_limited"'
        in src
    )

    # mint_password_reset_token
    src = inspect.getsource(admin.mint_password_reset_token)
    assert "_enforce_per_target_mint_rate_limit(" in src
    assert "BUCKET_PASSWORD_RESET_MINT" in src
    assert 'audit_event="auth.password_reset_mint_rate_limited"' in src
