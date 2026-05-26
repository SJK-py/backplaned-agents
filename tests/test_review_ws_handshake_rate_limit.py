"""Per-IP rate limit on `/v1/agent` WebSocket handshake.

Mirrors `login_rate_limit_per_ip_*` (HTTP login path): bounds
unauthenticated handshake floods BEFORE the JWT verify + Redis
revocation lookup so a flooding IP can't burn auth machinery on
every connect attempt.

Failure path: WS close with code 4029 (de-facto rate-limit code
in the 4000-4999 private range), reason "rate_limited".

Settings: `ws_handshake_rate_limit_per_ip_per_s` (default 5.0)
+ `_burst` (default 20). Rate=0 disables.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_state(*, rate: float, burst: int) -> MagicMock:
    state = MagicMock()
    state.settings.ws_handshake_rate_limit_per_ip_per_s = rate
    state.settings.ws_handshake_rate_limit_per_ip_burst = burst
    state.login_quota.try_consume = AsyncMock()
    return state


def _make_ws(host: str = "203.0.113.5") -> MagicMock:
    ws = MagicMock()
    ws.client.host = host
    return ws


def test_settings_exposes_handshake_rate_limit_fields() -> None:
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "ws_handshake_rate_limit_per_ip_per_s" in fields
    assert "ws_handshake_rate_limit_per_ip_burst" in fields

    # Defaults are sensible.
    s = Settings(
        db_url="postgresql://x:x@localhost/x",
        public_url="https://example.com",
        jwt_secret="x" * 32,
        admin_session_secret="y" * 32,
    )
    assert s.ws_handshake_rate_limit_per_ip_per_s > 0
    assert s.ws_handshake_rate_limit_per_ip_burst >= 1


def test_helper_allowed_returns_false() -> None:
    """Bucket allows → helper returns False (caller proceeds)."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    state = _make_state(rate=5.0, burst=20)
    decision = MagicMock()
    decision.allowed = True
    state.login_quota.try_consume.return_value = decision

    out = asyncio.run(ws_hub._handshake_rate_limit_denied(_make_ws(), state))
    assert out is False
    # Bucket key was per-IP.
    call = state.login_quota.try_consume.await_args
    assert call.args[0] == "ws_handshake:ip:203.0.113.5"
    assert call.kwargs["rate_per_s"] == 5.0
    assert call.kwargs["burst"] == 20


def test_helper_denied_returns_true() -> None:
    """Bucket denied → helper returns True (caller closes WS)."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    state = _make_state(rate=5.0, burst=20)
    decision = MagicMock()
    decision.allowed = False
    decision.retry_after_s = 2.0
    state.login_quota.try_consume.return_value = decision

    out = asyncio.run(ws_hub._handshake_rate_limit_denied(_make_ws(), state))
    assert out is True


def test_helper_short_circuits_when_disabled() -> None:
    """Rate=0 disables — helper returns False without consulting
    the bucket. Lets dev / single-worker deployments opt out
    without touching the WS endpoint code."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    state = _make_state(rate=0.0, burst=20)

    out = asyncio.run(ws_hub._handshake_rate_limit_denied(_make_ws(), state))
    assert out is False
    # Bucket NOT consulted on the disabled path.
    state.login_quota.try_consume.assert_not_awaited()


def test_helper_handles_unknown_client_host_safely() -> None:
    """Some test transports / proxies leave `ws.client` None. The
    helper must not crash; use a sentinel bucket key so a flood
    via that path still hits a single bucket."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    state = _make_state(rate=5.0, burst=20)
    decision = MagicMock()
    decision.allowed = True
    state.login_quota.try_consume.return_value = decision

    ws = MagicMock()
    ws.client = None
    out = asyncio.run(ws_hub._handshake_rate_limit_denied(ws, state))
    assert out is False
    call = state.login_quota.try_consume.await_args
    assert call.args[0] == "ws_handshake:ip:unknown"


def test_endpoint_checks_rate_limit_before_handshake() -> None:
    """Source pin: the rate-limit check runs BEFORE `_handshake` is
    called, so a flooding IP doesn't even reach the WS receive_text /
    JWT verify path."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub.register_ws_endpoint)
    lines = src.splitlines()
    rate_idx = next(
        (i for i, line in enumerate(lines)
         if "_handshake_rate_limit_denied(" in line),
        -1,
    )
    handshake_idx = next(
        (i for i, line in enumerate(lines)
         if "_handshake(ws, state)" in line),
        -1,
    )
    assert rate_idx >= 0 and handshake_idx >= 0
    assert rate_idx < handshake_idx, (
        "Rate-limit check must run BEFORE _handshake — otherwise "
        "every flood attempt still does receive_text + parse_frame."
    )


def test_endpoint_closes_with_4029_on_rate_limit() -> None:
    """The denied path closes with 4029 + reason='rate_limited'.
    Source pin so a regression to 1008 doesn't silently change
    the operator-facing close code on client diagnostics."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub.register_ws_endpoint)
    assert "ws.close(code=4029, reason=" in src
    assert '"rate_limited"' in src


def test_endpoint_failopens_on_quota_error() -> None:
    """The rate-limit check is wrapped in try/except so a Redis
    outage / misconfigured quota doesn't lock the WS endpoint shut
    — the supervisor exists to make rate-limit infrastructure
    failures observable but not catastrophic.
    """
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub.register_ws_endpoint)
    # try/except wrapping the helper call.
    assert "_handshake_rate_limit_denied(" in src
    assert "ws_handshake_rate_limit_check_failed" in src
