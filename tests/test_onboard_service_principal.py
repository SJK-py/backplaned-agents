"""Co-located service-principal provisioning at agent onboarding.

Covers the change that lets a service-provisioning invitation, when
consumed at `POST /v1/onboard`, also create a `usr_service_{agent_id}`
`level=service` user and mint it a refresh token returned alongside the
agent JWT — removing the separate admin `create_user` + env-seeded
refresh-token bootstrap. See `bp_router/api/onboard.py`,
`bp_router/principals.py`, migration 0002, and `bp_sdk/onboarding.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# principals helper
# ---------------------------------------------------------------------------


def test_service_user_id_for_agent_shape() -> None:
    from bp_router.principals import (
        SERVICE_USER_ID_PREFIX,
        service_user_id_for_agent,
    )

    assert SERVICE_USER_ID_PREFIX == "usr_service_"
    assert service_user_id_for_agent("chatbot") == "usr_service_chatbot"
    # The derived id is a valid user_id (usr_ prefix, legal alphabet).
    import re

    assert re.match(r"^usr_[A-Za-z0-9_-]{8,128}$", service_user_id_for_agent("chatbot"))


# ---------------------------------------------------------------------------
# query layer
# ---------------------------------------------------------------------------


def test_insert_invitation_binds_provisions_service_user() -> None:
    """`provisions_service_user` is bound as the 6th SQL parameter and
    defaults to False for back-compat callers."""
    from bp_router.db import queries

    captured: list[tuple] = []

    class _StubConn:
        async def execute(self, query: str, *args: Any) -> Any:
            captured.append((query, args))
            return None

    # Explicit True.
    asyncio.run(
        queries.insert_invitation(
            _StubConn(),  # type: ignore[arg-type]
            token_hash="h",
            level="service",
            expires_at=datetime.now(UTC),
            created_by="admin_a",
            idempotency_key=None,
            provisions_service_user=True,
        )
    )
    sql, args = captured[-1]
    assert "provisions_service_user" in sql
    assert args[-1] is True

    # Default False when omitted.
    asyncio.run(
        queries.insert_invitation(
            _StubConn(),  # type: ignore[arg-type]
            token_hash="h",
            level="service",
            expires_at=datetime.now(UTC),
            created_by="admin_a",
        )
    )
    _sql, args = captured[-1]
    assert args[-1] is False


def test_consume_invitation_returns_provisions_flag() -> None:
    """`consume_invitation` surfaces `provisions_service_user` from the
    row so onboarding can branch on it."""
    from bp_router.db import queries

    row = {
        "level": "service",
        "expires_at": datetime.now(UTC).replace(year=2999),
        "used_at": None,
        "provisions_service_user": True,
    }

    class _StubConn:
        async def fetchrow(self, *_a: Any, **_k: Any) -> Any:
            return row

        async def execute(self, *_a: Any, **_k: Any) -> Any:
            return None

    out = asyncio.run(
        queries.consume_invitation(
            _StubConn(),  # type: ignore[arg-type]
            token_hash="h",
            used_by="chatbot",
        )
    )
    assert out == {"level": "service", "provisions_service_user": True}


# ---------------------------------------------------------------------------
# Pydantic guards
# ---------------------------------------------------------------------------


def test_create_user_rejects_reserved_service_prefix() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import CreateUserRequest

    with pytest.raises(ValidationError):
        CreateUserRequest(
            email="a@b.com", level="tier0", user_id="usr_service_chatbot"
        )
    # A normal usr_ id is still accepted.
    ok = CreateUserRequest(email="a@b.com", level="tier0", user_id="usr_alice0001")
    assert ok.user_id == "usr_alice0001"


def test_issue_invitation_request_carries_provisions_flag() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import IssueInvitationRequest

    assert IssueInvitationRequest(level="service").provisions_service_user is False
    assert (
        IssueInvitationRequest(
            level="service", provisions_service_user=True
        ).provisions_service_user
        is True
    )


# ---------------------------------------------------------------------------
# onboard endpoint behaviour
# ---------------------------------------------------------------------------


def _drive_onboard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provisions: bool,
    existing_agent: Any = None,
    existing_svc: Any = None,
    agent_id: str = "chatbot",
) -> tuple[Any, dict[str, Any]]:
    """Drive `onboard()` against a fully stubbed pool + queries layer.
    Returns (response, mocks)."""
    pytest.importorskip("fastapi")
    import bp_router.catalog as catalog_mod
    from bp_protocol.types import AgentInfo
    from bp_router.api import onboard as onboard_mod

    agent_row = MagicMock()
    agent_row.agent_id = agent_id
    agent_row.status = "active"

    mocks = {
        "insert_user": AsyncMock(),
        "insert_refresh_token": AsyncMock(),
        "append_audit_event": AsyncMock(),
    }
    monkeypatch.setattr(
        onboard_mod.queries,
        "get_agent_for_update",
        AsyncMock(return_value=existing_agent),
    )
    monkeypatch.setattr(
        onboard_mod.queries,
        "consume_invitation",
        AsyncMock(
            return_value={"level": "service", "provisions_service_user": provisions}
        ),
    )
    monkeypatch.setattr(
        onboard_mod.queries, "insert_agent", AsyncMock(return_value=agent_row)
    )
    monkeypatch.setattr(
        onboard_mod.queries, "get_user_by_id", AsyncMock(return_value=existing_svc)
    )
    monkeypatch.setattr(onboard_mod.queries, "insert_user", mocks["insert_user"])
    monkeypatch.setattr(
        onboard_mod.queries, "insert_refresh_token", mocks["insert_refresh_token"]
    )
    monkeypatch.setattr(
        onboard_mod.queries, "append_audit_event", mocks["append_audit_event"]
    )
    monkeypatch.setattr(onboard_mod.queries, "list_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        onboard_mod,
        "issue_agent_token",
        lambda **_k: ("agent-jwt", datetime.now(UTC), "jti"),
    )
    monkeypatch.setattr(onboard_mod, "available_destinations", lambda *_a, **_k: {})
    monkeypatch.setattr(
        catalog_mod, "push_catalog_update_to_all", AsyncMock()
    )

    class _FakeTx:
        async def __aenter__(self) -> _FakeTx:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

    conn = MagicMock()
    conn.transaction = _FakeTx
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    state = MagicMock()
    state.db_pool = pool
    state.settings.refresh_token_ttl_s = 3600
    state.settings.agent_token_ttl_s = 3600
    request = MagicMock()
    request.app.state.bp = state

    req = onboard_mod.OnboardRequest(
        invitation_token="tok",
        agent_info=AgentInfo(agent_id=agent_id, description="x"),
    )
    resp = asyncio.run(onboard_mod.onboard(req, request))
    return resp, mocks


def test_onboard_provisions_service_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp, mocks = _drive_onboard(monkeypatch, provisions=True)

    # A new level=service user under the reserved id.
    mocks["insert_user"].assert_awaited_once()
    kw = mocks["insert_user"].await_args.kwargs
    assert kw["user_id"] == "usr_service_chatbot"
    assert kw["level"] == "service"
    assert kw["email"] is None
    assert kw["auth_kind"] == "api_key"

    # Response carries the service credential.
    assert resp.service_user_id == "usr_service_chatbot"
    assert isinstance(resp.service_refresh_token, str) and resp.service_refresh_token
    assert resp.service_token_expires_at is not None

    # The persisted hash is the hash of the returned plaintext — so the
    # token will validate at /v1/auth/refresh (which hashes identically).
    rt_kw = mocks["insert_refresh_token"].await_args.kwargs
    assert rt_kw["user_id"] == "usr_service_chatbot"
    assert (
        rt_kw["token_hash"]
        == hashlib.sha256(resp.service_refresh_token.encode("utf-8")).hexdigest()
    )


def test_onboard_without_flag_provisions_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp, mocks = _drive_onboard(monkeypatch, provisions=False)
    mocks["insert_user"].assert_not_awaited()
    mocks["insert_refresh_token"].assert_not_awaited()
    assert resp.service_user_id is None
    assert resp.service_refresh_token is None
    assert resp.service_token_expires_at is None


def test_onboard_recovery_reuses_existing_service_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = MagicMock()
    existing.level = "service"
    existing.deleted_at = None
    resp, mocks = _drive_onboard(
        monkeypatch, provisions=True, existing_svc=existing
    )

    # Reuse: no new user row, but a FRESH token is minted.
    mocks["insert_user"].assert_not_awaited()
    mocks["insert_refresh_token"].assert_awaited_once()
    assert resp.service_refresh_token

    prov = [
        c
        for c in mocks["append_audit_event"].await_args_list
        if c.kwargs.get("event") == "agent.service_principal_provisioned"
    ]
    assert len(prov) == 1
    assert prov[0].kwargs["payload"]["reused"] is True


def test_onboard_refuses_conflicting_service_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    conflicting = MagicMock()
    conflicting.level = "tier0"  # not a service user
    conflicting.deleted_at = None
    with pytest.raises(HTTPException) as ei:
        _drive_onboard(monkeypatch, provisions=True, existing_svc=conflicting)
    assert ei.value.status_code == 409


def test_onboard_provisions_inside_transaction() -> None:
    """Source pin: the service-user insert + refresh-token mint sit
    inside the same `conn.transaction()` block as the agent insert, so
    a failure rolls the whole onboarding back (atomic)."""
    pytest.importorskip("fastapi")
    from bp_router.api import onboard as onboard_mod

    src = inspect.getsource(onboard_mod.onboard)
    tx_idx = src.index("conn.transaction()")
    insert_user_idx = src.index("queries.insert_user(")
    mint_idx = src.index("queries.insert_refresh_token(")
    # Everything provisioning-related is after the transaction opens, and
    # the response build (post-commit) is after both.
    assert tx_idx < insert_user_idx
    assert tx_idx < mint_idx
    return_idx = src.index("return OnboardResponse(")
    assert insert_user_idx < return_idx
    assert mint_idx < return_idx


# ---------------------------------------------------------------------------
# SDK credential persistence
# ---------------------------------------------------------------------------


def _config(tmp_path: Any) -> Any:
    from bp_sdk.settings import AgentConfig

    return AgentConfig(state_dir=tmp_path)


def test_persist_credentials_merge_preserves_service_fields(tmp_path: Any) -> None:
    """The agent-token refresh loop calls `_persist_credentials` with
    only the agent fields; it must NOT wipe the service credential."""
    from bp_sdk.onboarding import _credentials_path, _persist_credentials

    cfg = _config(tmp_path)
    _persist_credentials(
        cfg,
        agent_id="chatbot",
        auth_token="A1",
        expires_at="2099-01-01T00:00:00+00:00",
        service_user_id="usr_service_chatbot",
        service_refresh_token="RT1",
        service_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    # Simulate an agent-token rotation (service fields omitted).
    _persist_credentials(
        cfg, agent_id="chatbot", auth_token="A2", expires_at="2099-02-01T00:00:00+00:00"
    )

    data = json.loads(_credentials_path(cfg).read_text())
    assert data["auth_token"] == "A2"
    assert data["service_user_id"] == "usr_service_chatbot"
    assert data["service_refresh_token"] == "RT1"


def test_persist_service_token_writeback(tmp_path: Any) -> None:
    from bp_sdk.onboarding import (
        _credentials_path,
        _persist_credentials,
        persist_service_token,
    )

    cfg = _config(tmp_path)
    _persist_credentials(
        cfg,
        agent_id="chatbot",
        auth_token="A1",
        expires_at=None,
        service_user_id="usr_service_chatbot",
        service_refresh_token="RT1",
        service_token_expires_at=None,
    )
    persist_service_token(
        cfg, refresh_token="RT2", expires_at="2099-01-01T00:00:00+00:00"
    )

    data = json.loads(_credentials_path(cfg).read_text())
    assert data["service_refresh_token"] == "RT2"
    assert data["auth_token"] == "A1"  # agent token preserved
    assert cfg.service_refresh_token == "RT2"


def test_onboard_or_resume_restores_service_fields(tmp_path: Any) -> None:
    """Resume (valid creds on disk) reloads the service credential onto
    config so the agent doesn't need to re-onboard (invitation is
    single-use)."""
    from bp_protocol.types import AgentInfo
    from bp_sdk.onboarding import _persist_credentials, onboard_or_resume

    cfg = _config(tmp_path)
    _persist_credentials(
        cfg,
        agent_id="chatbot",
        auth_token="A1",
        expires_at="2099-01-01T00:00:00+00:00",
        service_user_id="usr_service_chatbot",
        service_refresh_token="RT1",
        service_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    # Fresh config (drops the in-memory values); resume must reload them.
    cfg2 = _config(tmp_path)
    asyncio.run(onboard_or_resume(AgentInfo(agent_id="chatbot", description="x"), cfg2))
    assert cfg2.auth_token == "A1"
    assert cfg2.service_user_id == "usr_service_chatbot"
    assert cfg2.service_refresh_token == "RT1"
