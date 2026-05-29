"""WS Hello path consults `is_jti_revoked` after `verify_agent_token`.

Backstory: HTTP authn paths (`_principal_from_request` in
`security/jwt.py`, `refresh_agent_token` in `api/onboard.py`) call
`is_jti_revoked` after `verify_*` so that a rotated / explicitly-
revoked token is rejected before natural exp. The handshake path in
`bp_router/ws_hub.py:_handshake` previously skipped this step — an
agent whose jti had been revoked (e.g. via `/agent/refresh-token`
rotation or admin action) could still reconnect on the old token
until its natural expiry. This test pins the missing guard.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_handshake_rejects_revoked_jti(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_handshake` raises `_HandshakeFailed` with AUTH_FAILED when
    `is_jti_revoked` returns True for the verified jti."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ErrorCode, HelloFrame, serialize_frame
    from bp_protocol.types import AgentInfo
    from bp_router import ws_hub
    from bp_router.security.jwt import AgentPrincipal

    fake_principal = AgentPrincipal(
        agent_id="agt_alice",
        expires_at=datetime.now(UTC) + timedelta(seconds=3600),
        jti="revoked_jti_xyz",
        sdk_protocol_version="1",
    )
    monkeypatch.setattr(
        ws_hub, "verify_agent_token", lambda *a, **kw: fake_principal
    )
    # CRITICAL: this is the guard the fix adds.
    is_revoked_calls: list[tuple[Any, str]] = []

    async def _fake_is_jti_revoked(redis: Any, jti: str) -> bool:
        is_revoked_calls.append((redis, jti))
        return True

    monkeypatch.setattr(ws_hub, "is_jti_revoked", _fake_is_jti_revoked)

    hello = HelloFrame(
        agent_id="agt_alice",
        trace_id="0" * 32,
        span_id="0" * 16,
        auth_token="dummy",
        sdk_version="test",
        agent_info=AgentInfo(agent_id="agt_alice", description="t"),
    )

    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value=serialize_frame(hello))

    state = MagicMock()
    state.redis = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.settings.max_payload_bytes = 1_048_576

    with pytest.raises(ws_hub._HandshakeFailed) as exc_info:
        asyncio.run(ws_hub._handshake(ws, state))

    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    assert "revoked" in exc_info.value.reason
    # The guard actually called is_jti_revoked exactly once, with
    # the principal's jti.
    assert len(is_revoked_calls) == 1
    assert is_revoked_calls[0][1] == "revoked_jti_xyz"


def test_handshake_accepts_unrevoked_jti(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity-check companion: when `is_jti_revoked` returns False,
    the handshake proceeds past the revocation guard. Asserts via
    the next checkpoint (agent_id mismatch raises a different
    handshake-failed error) — a regression that flips revocation
    fail-closed would surface here too."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ErrorCode, HelloFrame, serialize_frame
    from bp_protocol.types import AgentInfo
    from bp_router import ws_hub
    from bp_router.security.jwt import AgentPrincipal

    fake_principal = AgentPrincipal(
        agent_id="agt_alice",
        expires_at=datetime.now(UTC) + timedelta(seconds=3600),
        jti="ok_jti",
        sdk_protocol_version="1",
    )
    monkeypatch.setattr(
        ws_hub, "verify_agent_token", lambda *a, **kw: fake_principal
    )
    monkeypatch.setattr(
        ws_hub, "is_jti_revoked", AsyncMock(return_value=False)
    )

    # Mismatch: Hello.agent_id != principal.agent_id → raises with
    # a different reason than "revoked". Confirms the JTI guard
    # passed through and the next check fired.
    hello = HelloFrame(
        agent_id="agt_bob",  # mismatch
        trace_id="0" * 32,
        span_id="0" * 16,
        auth_token="dummy",
        sdk_version="test",
        agent_info=AgentInfo(agent_id="agt_bob", description="t"),
    )

    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value=serialize_frame(hello))

    state = MagicMock()
    state.redis = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.settings.max_payload_bytes = 1_048_576

    with pytest.raises(ws_hub._HandshakeFailed) as exc_info:
        asyncio.run(ws_hub._handshake(ws, state))

    # Got past the revocation guard. Reason is the agent_id mismatch.
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    assert "token sub does not match Hello.agent_id" in exc_info.value.reason


def test_handshake_calls_revocation_check_before_db_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revocation check is cheap (single Redis EXISTS); the agent-row
    DB fetch is more expensive. The handshake order must be
    `verify → is_jti_revoked → agent_id match → DB lookup`. This
    test fails closed: a regression that re-ordered the check after
    DB lookup, or removed it entirely, would not satisfy this pin."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    revoke_idx = src.find("is_jti_revoked")
    db_idx = src.find("queries.get_agent")
    assert revoke_idx != -1, "is_jti_revoked must be called in _handshake"
    assert db_idx != -1, "queries.get_agent should still be called after the guard"
    assert revoke_idx < db_idx, (
        "is_jti_revoked must be called BEFORE queries.get_agent — "
        "otherwise revoked tokens trigger an unnecessary DB hit."
    )
