"""Tests for the Adm-M2 review fix — `POST /invitations`
idempotency via the `Idempotency-Key` header.

Plus a small sanity test that the 0006 migration's
`created_at` column is now declared on the `InvitationRow` model
(catches the latent bug PR #67 introduced when it ordered by a
column that didn't exist).
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Migration 0006: created_at column exists + idempotency_key column
# ===========================================================================


def test_invitation_row_model_declares_created_at() -> None:
    """PR #67 (Adm-M3/M4) added `ORDER BY created_at DESC` to the
    invitation list query without verifying the column existed.
    Migration 0006 now adds it. Pin the model field so a refactor
    that removes `created_at` from the model surfaces the missing
    SQL column too."""
    from bp_router.db.models import InvitationRow

    assert "created_at" in InvitationRow.model_fields
    assert "idempotency_key" in InvitationRow.model_fields
    # idempotency_key is nullable (older rows have None).
    fld = InvitationRow.model_fields["idempotency_key"]
    assert fld.default is None or fld.is_required() is False


def test_invitations_schema_has_created_at_and_idempotency_key() -> None:
    """Source-level: the consolidated 0001 migration declares both
    columns + the partial unique index. (This used to live in a
    separate 0006 migration; folded into 0001 as part of the
    pre-release migration consolidation — no production deployment
    carries an intermediate schema.)"""
    # Migration filenames start with a digit so they can't be `import`ed
    # by name. Read the source directly.
    from pathlib import Path
    mig_path = Path(__file__).parent.parent / (
        "bp_router/db/migrations/versions/0001_initial_schema.py"
    )
    src = mig_path.read_text()
    # The invitations table inline-declares created_at + idempotency_key
    # in the consolidated form (no ADD COLUMN — it's the initial CREATE).
    assert "created_at       timestamptz NOT NULL DEFAULT now()" in src
    assert "idempotency_key  text" in src
    # Partial unique index (NULL keys don't conflict).
    assert "CREATE UNIQUE INDEX" in src
    assert "invitations_created_by_idempotency_key_uniq" in src
    assert "(created_by, idempotency_key)" in src
    assert "WHERE idempotency_key IS NOT NULL" in src
    # The downgrade drops the whole `invitations` table — no need to
    # walk individual columns since this is the initial schema. The
    # f-string template iterates over a tuple that includes
    # "invitations".
    assert '"invitations"' in src
    assert 'DROP TABLE IF EXISTS {table} CASCADE' in src


# ===========================================================================
# queries.insert_invitation accepts idempotency_key
# ===========================================================================


def test_insert_invitation_passes_idempotency_key_to_sql() -> None:
    """Behavioral: `insert_invitation(idempotency_key=...)` binds
    the value as parameter $5 in the INSERT. None is allowed."""
    from bp_router.db import queries

    captured: list[tuple] = []

    class _StubConn:
        async def execute(self, query: str, *args: Any) -> Any:
            captured.append((query, args))
            return None

    asyncio.run(queries.insert_invitation(
        _StubConn(),  # type: ignore[arg-type]
        token_hash="hash_a",
        level="tier0",
        expires_at=datetime.now(UTC),
        created_by="admin_alice",
        idempotency_key="client-retry-key-1",
    ))
    assert len(captured) == 1
    sql, args = captured[0]
    assert "idempotency_key" in sql
    # idempotency_key is $5; provisions_service_user ($6) is now last.
    assert args[4] == "client-retry-key-1"
    assert args[-1] is False  # provisions_service_user default


def test_insert_invitation_default_key_is_none() -> None:
    """Backward-compat: callers omitting `idempotency_key` get
    None bound (the column is nullable)."""
    from bp_router.db import queries

    captured: list[tuple] = []

    class _StubConn:
        async def execute(self, query: str, *args: Any) -> Any:
            captured.append((query, args))
            return None

    asyncio.run(queries.insert_invitation(
        _StubConn(),  # type: ignore[arg-type]
        token_hash="hash_a",
        level="tier0",
        expires_at=datetime.now(UTC),
        created_by="admin_alice",
    ))
    sql, args = captured[0]
    # 6 params bound: token_hash, level, expires_at, created_by,
    # idempotency_key=None ($5), provisions_service_user=False ($6).
    assert args[4] is None  # idempotency_key
    assert args[-1] is False  # provisions_service_user default


# ===========================================================================
# queries.find_invitation_by_idempotency_key
# ===========================================================================


def test_find_invitation_by_idempotency_key_query_shape() -> None:
    """Source pin: scoped by `created_by AND idempotency_key`."""
    from bp_router.db import queries

    src = inspect.getsource(queries.find_invitation_by_idempotency_key)
    assert "WHERE created_by = $1 AND idempotency_key = $2" in src


def test_find_invitation_returns_none_when_no_match() -> None:
    from bp_router.db import queries

    class _StubConn:
        async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
            return None

    out = asyncio.run(queries.find_invitation_by_idempotency_key(
        _StubConn(),  # type: ignore[arg-type]
        created_by="admin_alice",
        idempotency_key="never-issued",
    ))
    assert out is None


# ===========================================================================
# Endpoint: POST /invitations honours Idempotency-Key
# ===========================================================================


def test_issue_invitation_accepts_idempotency_key_header() -> None:
    """Source pin: the endpoint signature declares
    `idempotency_key: Optional[str] = Header(default=None,
    alias="Idempotency-Key")`."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    sig = inspect.signature(admin.issue_invitation)
    params = sig.parameters
    assert "idempotency_key" in params
    # The Header default carries the alias.
    default = params["idempotency_key"].default
    # FastAPI's Header() returns a `Header` instance with `.alias`.
    alias = getattr(default, "alias", None)
    assert alias == "Idempotency-Key"


def test_issue_invitation_passes_key_to_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral: when a client sends `Idempotency-Key`, the value
    flows through to `queries.insert_invitation`."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    captured: dict[str, Any] = {}

    async def _fake_insert(
        conn: Any, *, token_hash: str, level: str,
        expires_at: Any, created_by: str,
        idempotency_key: Any = None,
        provisions_service_user: Any = False,
    ) -> None:
        captured["idempotency_key"] = idempotency_key
        captured["created_by"] = created_by

    monkeypatch.setattr(admin.queries, "insert_invitation", _fake_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    request = MagicMock()
    request.app.state.bp = state

    principal = MagicMock()
    principal.user_id = "admin_alice"

    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    out = asyncio.run(admin.issue_invitation(
        req, request, principal, idempotency_key="client-retry-1",
    ))
    assert out.invitation_token  # plaintext token returned
    assert captured["idempotency_key"] == "client-retry-1"
    assert captured["created_by"] == "admin_alice"


def test_issue_invitation_no_header_passes_none_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No header → idempotency_key=None bound to the INSERT;
    legacy non-idempotent behavior preserved."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    captured: dict[str, Any] = {}

    async def _fake_insert(
        conn: Any, *, token_hash: str, level: str,
        expires_at: Any, created_by: str,
        idempotency_key: Any = None,
        provisions_service_user: Any = False,
    ) -> None:
        captured["idempotency_key"] = idempotency_key

    monkeypatch.setattr(admin.queries, "insert_invitation", _fake_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    request = MagicMock()
    request.app.state.bp = state

    principal = MagicMock()
    principal.user_id = "admin_alice"

    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    asyncio.run(admin.issue_invitation(
        req, request, principal, idempotency_key=None,
    ))
    assert captured["idempotency_key"] is None


def test_issue_invitation_unique_violation_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an admin retries with the same `Idempotency-Key` AND
    the violated constraint is the idempotency-key uniqueness
    index, asyncpg raises `UniqueViolationError`. The endpoint
    catches it and returns 409 with a clear message — we can't
    return the original plaintext token (server only stored the
    hash) so the client must reuse the token from the first call.

    Updated for review item H4: the handler now checks
    `constraint_name` so it only translates the SPECIFIC
    idempotency-key collision. The stub here sets the attribute
    to match — the negative case is covered by the new
    `test_issue_invitation_other_unique_violation_re_raised`
    below."""
    pytest.importorskip("fastapi")
    import asyncpg
    from fastapi import HTTPException

    from bp_router.api import admin

    async def _raising_insert(*args: Any, **kwargs: Any) -> None:
        exc = asyncpg.UniqueViolationError("duplicate key")
        exc.constraint_name = "invitations_created_by_idempotency_key_uniq"
        raise exc

    monkeypatch.setattr(admin.queries, "insert_invitation", _raising_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    request = MagicMock()
    request.app.state.bp = state
    principal = MagicMock()
    principal.user_id = "admin_alice"
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(admin.issue_invitation(
            req, request, principal, idempotency_key="dup-key",
        ))
    assert excinfo.value.status_code == 409
    assert "Idempotency-Key" in excinfo.value.detail


def test_issue_invitation_unique_violation_without_header_re_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a `UniqueViolationError` fires without an `Idempotency-Key`
    header, it's some OTHER unique constraint (e.g. token_hash
    collision — astronomically rare but possible). Re-raise rather
    than misleadingly returning 409 about idempotency."""
    pytest.importorskip("fastapi")
    import asyncpg

    from bp_router.api import admin

    async def _raising_insert(*args: Any, **kwargs: Any) -> None:
        raise asyncpg.UniqueViolationError("token_hash collision")

    monkeypatch.setattr(admin.queries, "insert_invitation", _raising_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    request = MagicMock()
    request.app.state.bp = state
    principal = MagicMock()
    principal.user_id = "admin_alice"
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    # No Idempotency-Key header → UniqueViolationError is some other
    # constraint, re-raised as-is (not converted to 409).
    with pytest.raises(asyncpg.UniqueViolationError):
        asyncio.run(admin.issue_invitation(
            req, request, principal, idempotency_key=None,
        ))


def test_issue_invitation_audit_payload_records_idempotent_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit payload now records whether the call was idempotent
    so operators can correlate retries with logs."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    audit_calls: list[dict[str, Any]] = []

    async def _capture_audit(conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(admin.queries, "append_audit_event", _capture_audit)
    monkeypatch.setattr(
        admin.queries, "insert_invitation", AsyncMock(return_value=None)
    )

    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn

    request = MagicMock()
    request.app.state.bp = state
    principal = MagicMock()
    principal.user_id = "admin_alice"
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    # With key → idempotent=True.
    asyncio.run(admin.issue_invitation(
        req, request, principal, idempotency_key="key-1",
    ))
    # Without key → idempotent=False.
    asyncio.run(admin.issue_invitation(
        req, request, principal, idempotency_key=None,
    ))

    assert audit_calls[0]["payload"]["idempotent"] is True
    assert audit_calls[1]["payload"]["idempotent"] is False
