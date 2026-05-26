"""Tests for Phase 1 of the Gemini-fork nice-to-haves.

Each section covers one feature:

  * F1 — priority kwarg on `PeerClient.spawn` and `PeerClient.delegate`
  * F4 — `Scope.update_session_metadata` shallow-merge helper
  * F10 — bootstrap-friendly invitation tokens (optional `token`,
    re-run handling)
  * F11 — bootstrap-friendly user creation (optional `user_id`,
    `initial_refresh_token`)

F8's `serviced_by` field is intentionally NOT exercised here — it
lands with Phase 2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_protocol.frames import NewTaskFrame
from bp_protocol.types import TaskPriority

# ===========================================================================
# F1 — priority kwarg on PeerClient.spawn / delegate
# ===========================================================================


def test_spawn_defaults_priority_to_normal() -> None:
    """Backward-compat: agents that don't pass `priority=` still emit
    NORMAL on the wire."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    sig = PeerClient.spawn.__annotations__
    # The defaulted parameter is keyword-only; just confirm the
    # function signature accepts `priority` and the default behaviour
    # is unchanged. The actual frame plumbing is exercised below.
    import inspect

    params = inspect.signature(PeerClient.spawn).parameters
    assert "priority" in params
    assert params["priority"].default is TaskPriority.NORMAL


def test_delegate_defaults_priority_to_normal() -> None:
    pytest.importorskip("fastapi")
    import inspect

    from bp_sdk.peers import PeerClient

    params = inspect.signature(PeerClient.delegate).parameters
    assert "priority" in params
    assert params["priority"].default is TaskPriority.NORMAL


def test_new_task_frame_round_trips_priority_values() -> None:
    """Wire-level pin: each TaskPriority value survives the frame
    constructor unchanged. (The SDK just passes the kwarg through.)"""
    from bp_protocol.frames import parse_frame, serialize_frame

    for prio in (TaskPriority.LOW, TaskPriority.NORMAL, TaskPriority.HIGH):
        frame = NewTaskFrame(
            agent_id="agt",
            trace_id="0" * 32,
            span_id="0" * 16,
            destination_agent_id="agt_dst",
            user_id="usr",
            session_id="ses",
            priority=prio,
        )
        round_tripped = parse_frame(serialize_frame(frame))
        assert isinstance(round_tripped, NewTaskFrame)
        assert round_tripped.priority is prio


# ===========================================================================
# F4 — update_session_metadata uses Postgres `||` shallow merge
# ===========================================================================


def test_update_session_metadata_uses_jsonb_concat() -> None:
    """Source pin: the query uses `metadata || $3::jsonb` so the merge
    happens server-side under row lock. Concurrent writers with
    disjoint keys both land."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.db import queries

    src = inspect.getsource(queries.Scope.update_session_metadata)
    # Server-side concat — NOT a Python-side `{**old, **merge}` then
    # full overwrite.
    assert "metadata || $3::jsonb" in src
    # User-scoped — the WHERE clause MUST include user_id.
    assert "user_id = $2" in src


def test_update_session_metadata_passes_dict_not_json_string() -> None:
    """Defence against the asyncpg jsonb-codec foot-gun. The merge
    dict must be bound directly; `json.dumps`-ing first double-encodes
    and stores a STRING that `||` then unions into a LIST."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.db import queries

    src = inspect.getsource(queries.Scope.update_session_metadata)
    # Must NOT pre-serialise the merge dict.
    assert "json.dumps(merge)" not in src
    assert "json_dumps" not in src


# ===========================================================================
# F10 — bootstrap-friendly invitation tokens
# ===========================================================================


def test_issue_invitation_request_accepts_caller_supplied_token() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import IssueInvitationRequest

    req = IssueInvitationRequest(
        level="tier0",
        expires_in_s=3600,
        token="x" * 32,
    )
    assert req.token == "x" * 32


def test_issue_invitation_request_rejects_short_token() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import IssueInvitationRequest

    with pytest.raises(ValidationError) as exc_info:
        IssueInvitationRequest(level="tier0", token="too-short")
    assert "at least 32 characters" in str(exc_info.value)


def test_issue_invitation_request_rejects_non_urlsafe_token() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import IssueInvitationRequest

    with pytest.raises(ValidationError) as exc_info:
        IssueInvitationRequest(level="tier0", token="a" * 31 + "!")
    assert "URL-safe" in str(exc_info.value)


def test_issue_invitation_token_field_defaults_none() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import IssueInvitationRequest

    req = IssueInvitationRequest(level="tier0")
    assert req.token is None


def test_issue_invitation_handler_uses_supplied_token_when_present() -> None:
    """Source pin: the handler reads `req.token` and falls back to
    `_secrets.token_urlsafe(32)` only when None."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin.issue_invitation)
    # The token-selection line.
    assert "req.token if req.token else _secrets.token_urlsafe(32)" in src


def test_issue_invitation_handler_handles_bootstrap_rerun() -> None:
    """Source pin: on `invitations_pkey` collision WITH both
    caller-supplied token AND idempotency_key, the handler defers
    to a verify-and-return path in a fresh transaction."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin.issue_invitation)
    # The bootstrap re-run path triggers on the primary-key collision.
    assert 'constraint == "invitations_pkey"' in src
    assert "req.token is not None" in src
    # And the verify-and-return path checks all four fields match
    # before honoring the re-run.
    assert "existing.created_by != principal.user_id" in src
    assert "existing.idempotency_key != idempotency_key" in src
    assert "existing.level != req.level" in src


# ===========================================================================
# F11 — bootstrap-friendly user creation
# ===========================================================================


def test_create_user_request_accepts_caller_supplied_user_id() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import CreateUserRequest

    req = CreateUserRequest(
        email="alice@example.com",
        level="tier0",
        user_id="usr_alice_001",
    )
    assert req.user_id == "usr_alice_001"


def test_create_user_request_rejects_malformed_user_id() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import CreateUserRequest

    # Wrong prefix.
    with pytest.raises(ValidationError):
        CreateUserRequest(
            email="alice@example.com", level="tier0", user_id="user_alice",
        )
    # Too short.
    with pytest.raises(ValidationError):
        CreateUserRequest(
            email="alice@example.com", level="tier0", user_id="usr_a",
        )
    # Invalid characters.
    with pytest.raises(ValidationError):
        CreateUserRequest(
            email="alice@example.com", level="tier0",
            user_id="usr_alice!" + "x" * 8,
        )


def test_create_user_request_accepts_initial_refresh_token() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import CreateUserRequest

    req = CreateUserRequest(
        email="alice@example.com",
        level="tier0",
        initial_refresh_token="y" * 32,
    )
    assert req.initial_refresh_token == "y" * 32


def test_create_user_request_rejects_short_refresh_token() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import CreateUserRequest

    with pytest.raises(ValidationError):
        CreateUserRequest(
            email="alice@example.com",
            level="tier0",
            initial_refresh_token="too-short",
        )


def test_create_user_handler_seeds_refresh_token_when_supplied() -> None:
    """Source pin: when `initial_refresh_token` is set, the handler
    calls `insert_refresh_token` inside the same transaction as
    `insert_user` — so a refresh-token insert failure aborts the
    user create too."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin.create_user)
    # Both inserts inside one transaction.
    assert "async with conn.transaction():" in src
    assert "insert_refresh_token" in src
    assert "if req.initial_refresh_token is not None:" in src


def test_create_user_handler_pre_checks_user_id_collision() -> None:
    """Source pin: a typed 409 fires when the caller-supplied user_id
    is already taken — not a 500 from a bubbled UniqueViolation."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.api import admin

    src = inspect.getsource(admin.create_user)
    assert "if req.user_id is not None:" in src
    assert "user_id {req.user_id!r} already exists" in src
