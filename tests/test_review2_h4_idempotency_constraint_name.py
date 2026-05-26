"""Tests for the second-pass review H4 fix — `POST /invitations`
narrows its `UniqueViolationError` translation to the SPECIFIC
`invitations_created_by_idempotency_key_uniq` constraint.

Without the discrimination, ANY unique violation (e.g. the
`token_hash` PK collision, or any future unique index added
to the `invitations` table) would silently masquerade as an
"Idempotency-Key already used" 409 response — misleading
operators and potentially hiding real bugs.

The matching legacy tests in
`test_review_adm_m2_invitation_idempotency.py` were updated to
set `exc.constraint_name = ...` on the raised
`UniqueViolationError` so they continue to exercise the 409
path; this file pins the new negative case + the source-level
contract.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# H4: constraint_name discrimination
# ===========================================================================


def _build_request_state() -> tuple[Any, Any, Any]:
    """Stub state + request + principal for the endpoint."""
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
    return state, request, principal


def test_idempotency_409_only_fires_for_specific_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 409-with-Idempotency-Key-message response must only
    fire when the violated constraint is
    `invitations_created_by_idempotency_key_uniq`. A
    `UniqueViolationError` carrying any OTHER constraint name
    must be re-raised as-is so the underlying 500 surfaces the
    real bug rather than being misleadingly framed as an
    idempotency conflict (review item H4)."""
    pytest.importorskip("fastapi")
    import asyncpg

    from bp_router.api import admin

    async def _raising_insert(*args: Any, **kwargs: Any) -> None:
        exc = asyncpg.UniqueViolationError("token_hash collision")
        # NOT the idempotency constraint — simulate a token_hash
        # PK collision (astronomically unlikely with 32-byte
        # `secrets.token_urlsafe`, but a future schema change
        # adding ANY other unique index would fall into this case).
        exc.constraint_name = "invitations_pkey"
        raise exc

    monkeypatch.setattr(admin.queries, "insert_invitation", _raising_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    _state, request, principal = _build_request_state()
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    # WITH an Idempotency-Key but the violation is NOT the
    # idempotency constraint → re-raise (NOT converted to 409).
    with pytest.raises(asyncpg.UniqueViolationError):
        asyncio.run(admin.issue_invitation(
            req, request, principal,
            idempotency_key="some-key",
        ))


def test_idempotency_409_fires_when_constraint_name_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-pin the happy path: `UniqueViolationError` whose
    `constraint_name` IS the idempotency-key index produces the
    409 with the actionable message."""
    pytest.importorskip("fastapi")
    import asyncpg
    from fastapi import HTTPException

    from bp_router.api import admin

    async def _raising_insert(*args: Any, **kwargs: Any) -> None:
        exc = asyncpg.UniqueViolationError("duplicate key value")
        exc.constraint_name = "invitations_created_by_idempotency_key_uniq"
        raise exc

    monkeypatch.setattr(admin.queries, "insert_invitation", _raising_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    _state, request, principal = _build_request_state()
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(admin.issue_invitation(
            req, request, principal,
            idempotency_key="dup-key",
        ))
    assert excinfo.value.status_code == 409
    assert "Idempotency-Key" in excinfo.value.detail


def test_idempotency_409_skipped_when_constraint_name_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncpg ALWAYS sets `constraint_name` on
    `UniqueViolationError` in practice, but defence-in-depth: if
    a future asyncpg version omits it (or the attribute is None
    for some adapter wrapping), we re-raise rather than guessing
    that the omission means 'must be ours.'"""
    pytest.importorskip("fastapi")
    import asyncpg

    from bp_router.api import admin

    async def _raising_insert(*args: Any, **kwargs: Any) -> None:
        # Bare exception — no constraint_name attribute set.
        raise asyncpg.UniqueViolationError("opaque duplicate key")

    monkeypatch.setattr(admin.queries, "insert_invitation", _raising_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    _state, request, principal = _build_request_state()
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    # `constraint_name` defaults to None on a freshly-raised exc;
    # `getattr(exc, "constraint_name", None) != "invitations_..."`
    # is True → re-raise.
    with pytest.raises(asyncpg.UniqueViolationError):
        asyncio.run(admin.issue_invitation(
            req, request, principal,
            idempotency_key="some-key",
        ))


def test_no_header_no_constraint_check_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `idempotency_key is None`, the handler short-circuits
    BEFORE checking constraint_name. Pin that order so a
    constraint name happening to match doesn't accidentally
    surface a misleading 409 for a non-idempotent call."""
    pytest.importorskip("fastapi")
    import asyncpg

    from bp_router.api import admin

    async def _raising_insert(*args: Any, **kwargs: Any) -> None:
        exc = asyncpg.UniqueViolationError("dup key")
        # Even if some future bug populates this field for a
        # non-idempotent call, the absence of the header takes
        # priority.
        exc.constraint_name = "invitations_created_by_idempotency_key_uniq"
        raise exc

    monkeypatch.setattr(admin.queries, "insert_invitation", _raising_insert)
    monkeypatch.setattr(
        admin.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    _state, request, principal = _build_request_state()
    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    with pytest.raises(asyncpg.UniqueViolationError):
        asyncio.run(admin.issue_invitation(
            req, request, principal,
            idempotency_key=None,
        ))


def test_handler_source_inspects_constraint_name() -> None:
    """Source pin: the handler checks
    `exc.constraint_name == "invitations_created_by_idempotency_key_uniq"`.
    A regression that drops the constraint check (or hard-codes the
    wrong name) is caught immediately."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.issue_invitation)
    # Constraint discrimination is wired.
    assert "constraint_name" in src
    assert "invitations_created_by_idempotency_key_uniq" in src
    # And the no-header short-circuit still appears (preserves the
    # existing re-raise contract for plain non-idempotent calls).
    assert "idempotency_key is None" in src
