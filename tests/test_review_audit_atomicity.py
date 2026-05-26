"""Tests for the audit-trail atomicity review fix (DB-H1, DB-M5).

Every admin / auth endpoint that performs a privileged mutation
followed by an `append_audit_event` call must wrap both in a
single `conn.transaction()` block. Otherwise the audit insert
opens its own internal transaction and commits separately —
a crash between the two leaves the privileged change committed
without an audit row.

Direct atomicity testing requires a live Postgres for asyncpg
savepoint semantics; the source-level checks below catch
regressions cheaply by verifying every (mutation + audit)
endpoint has the wrap in place. The single behavioral test
stubs the audit helper to raise and verifies the outer
`conn.transaction()` surfaces the failure correctly via
asyncpg's transaction context-manager protocol.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Source-level: every (mutation + audit) endpoint has the transaction wrap
# ---------------------------------------------------------------------------


# Endpoints in `bp_router.api.admin` that perform both a privileged
# mutation AND an `append_audit_event` call. Each tuple is
# `(function_name, expected_mutation_call_pattern)` — the pattern
# is the literal CALL site (with leading dot or paren) so it can't
# match a function name or docstring substring.
#
# When a new admin mutation endpoint lands, add it here. The test
# below ensures the transaction wrap is also present.
ADMIN_MUTATION_AUDIT_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("issue_invitation", "queries.insert_invitation("),
    ("create_user", "queries.insert_user("),
    # update_user runs raw SQL via conn.fetchrow; find the execution
    # site rather than the SQL string itself (which is built earlier).
    ("update_user", "conn.fetchrow(sql,"),
    ("replace_rules", "queries.replace_acl_rules("),
    ("add_rule", "queries.insert_acl_rule("),
    ("update_rule", "queries.update_acl_rule("),
    ("delete_rule", "queries.delete_acl_rule("),
    ("reorder_rules", "queries.reorder_acl_rules("),
    ("suspend_agent", "queries.suspend_agent("),
    ("unsuspend_agent", "queries.unsuspend_agent("),
    ("evict_agent", "queries.evict_agent("),
    ("revoke_invitation", "queries.delete_invitation("),
)


@pytest.mark.parametrize(
    "func_name,mutation_call", ADMIN_MUTATION_AUDIT_ENDPOINTS
)
def test_admin_endpoint_wraps_mutation_and_audit_in_transaction(
    func_name: str, mutation_call: str
) -> None:
    """For each endpoint: source must contain a `conn.transaction()`
    block, and BOTH the mutation call site and the audit call site
    must appear inside that block.

    Searches use call-site patterns (with parens / dots) so they
    don't false-match function names, docstrings, or comments that
    mention these symbols.

    This is the regression-catch for DB-H1: any future endpoint
    that forgets the wrap fails this test."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    func = getattr(admin, func_name)
    src = inspect.getsource(func)

    audit_call = "queries.append_audit_event("

    assert "conn.transaction()" in src, (
        f"{func_name}: no `conn.transaction()` wrap — audit will "
        "commit separately from the mutation (review item DB-H1)"
    )
    assert mutation_call in src, (
        f"{func_name}: expected mutation call site "
        f"{mutation_call!r} not found — test fixture stale?"
    )
    assert audit_call in src, (
        f"{func_name}: no audit call — wrong endpoint in fixture?"
    )

    tx_idx = src.index("conn.transaction()")
    audit_idx = src.index(audit_call)
    mut_idx = src.index(mutation_call)
    assert tx_idx < audit_idx, (
        f"{func_name}: `append_audit_event(` call appears before "
        "`conn.transaction()` opens — audit isn't inside the wrap"
    )
    assert tx_idx < mut_idx, (
        f"{func_name}: mutation call {mutation_call!r} appears "
        "before the transaction opens — not inside the wrap"
    )


def test_admin_test_task_remains_audit_only() -> None:
    """`test_task` issues an audit event but no preceding mutation
    in the same connection — `admit_task` runs in its own pool
    acquisition. This endpoint is NOT in the wrap-required list;
    the test pins that classification so future work doesn't
    accidentally enroll it."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.test_task)
    # Audit call exists.
    assert "append_audit_event" in src
    # The audit call's outer `async with state.db_pool.acquire()`
    # block contains ONLY the audit — no other queries.* mutation
    # call in the same block.
    audit_section_start = src.index("append_audit_event")
    audit_section = src[audit_section_start:audit_section_start + 800]
    # Verify no other mutation calls appear in the immediate audit
    # block. (admit_task is called earlier, outside this block.)
    bad_neighbors = [
        "insert_", "update_", "delete_", "replace_", "suspend_",
        "unsuspend_", "evict_", "reorder_",
    ]
    for needle in bad_neighbors:
        if needle in audit_section:
            # Some words like "level" contain "lev" but not these
            # exact prefixes; if any matches it's a real concern.
            # Allow it ONLY if the test_task body has changed and a
            # mutation was added — that case should bring the
            # endpoint into ADMIN_MUTATION_AUDIT_ENDPOINTS above.
            raise AssertionError(
                f"test_task now contains {needle!r} alongside the "
                "audit call. If a mutation was added, enroll "
                "test_task in ADMIN_MUTATION_AUDIT_ENDPOINTS."
            )


# ---------------------------------------------------------------------------
# Source-level: auth.py atomic paths
# ---------------------------------------------------------------------------


def test_login_refresh_token_path_is_atomic() -> None:
    """`/login` success path: refresh-token insert + audit must be
    atomic (review item DB-H1). A failure on the audit append after
    the refresh token row commits would issue a token to the client
    that the audit log never saw."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.login)
    # Find the SECOND `pool.acquire()` block (the post-token-mint one).
    # It contains insert_refresh_token + audit.
    acquires = [i for i in range(len(src))
                if src[i:].startswith("pool.acquire()")]
    assert len(acquires) >= 2, "expected two pool.acquire() blocks in login"
    second_block = src[acquires[1]:]
    assert "conn.transaction()" in second_block
    # Both call sites (with paren) are inside the wrap.
    tx_idx = second_block.index("conn.transaction()")
    mut_idx = second_block.index("queries.insert_refresh_token(")
    audit_idx = second_block.index("queries.append_audit_event(")
    assert tx_idx < mut_idx < audit_idx


def test_change_password_success_path_is_atomic() -> None:
    """`/change-password` success path: UPDATE users + delete refresh
    tokens + audit must be atomic. Critical because the JTI
    revocation (`revoke_jti`) lives OUTSIDE the transaction
    intentionally — if any of the three DB writes fail, we mustn't
    have stranded the user's only access token in the revocation
    set."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.change_password)
    assert "conn.transaction()" in src
    tx_idx = src.index("conn.transaction()")
    # The actual SQL UPDATE is executed via `conn.execute("UPDATE
    # users SET auth_secret_hash...")`. Look for the SET clause
    # (which only appears inside the success path) so the search
    # is robust against indentation changes.
    update_idx = src.index('"UPDATE users SET auth_secret_hash')
    delete_idx = src.index("queries.delete_user_refresh_tokens(")
    # The failure path also calls `queries.append_audit_event(`
    # BEFORE the transaction; pick the success-path call by starting
    # the search after `update_idx`.
    success_audit_idx = src.index(
        "queries.append_audit_event(", update_idx
    )
    assert tx_idx < update_idx
    assert tx_idx < delete_idx
    assert tx_idx < success_audit_idx
    # `revoke_jti(` stays AFTER the success audit (post-commit path).
    revoke_idx = src.index("revoke_jti(")
    assert revoke_idx > success_audit_idx


# ---------------------------------------------------------------------------
# Behavioral: a raise from append_audit_event inside the transaction
# propagates out (asyncpg's __aexit__ rolls back on exception).
# ---------------------------------------------------------------------------


def test_audit_failure_propagates_through_outer_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral check: when `append_audit_event` raises inside the
    `conn.transaction()` block, the exception propagates out of the
    endpoint (FastAPI then surfaces a 500). The transaction context
    manager's `__aexit__(exc, ...)` is what calls ROLLBACK in
    asyncpg — we verify the path leaves the exception intact rather
    than swallowing it."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    # Stub the queries layer.
    monkeypatch.setattr(
        admin.queries, "insert_invitation", AsyncMock(return_value=None)
    )

    class _AuditBoom(RuntimeError):
        pass

    async def _raising_audit(*_args: Any, **_kwargs: Any) -> None:
        raise _AuditBoom("audit chain busy")

    monkeypatch.setattr(admin.queries, "append_audit_event", _raising_audit)

    # Stub conn + pool. asyncpg's `conn.transaction()` is an async
    # context manager; the simplest faithful mock is a class whose
    # __aenter__ / __aexit__ pass exceptions through unchanged.
    class _FakeTx:
        async def __aenter__(self) -> _FakeTx:
            return self
        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            # Returning False (default) re-raises the exception —
            # matches asyncpg.Transaction behaviour: it calls ROLLBACK
            # in __aexit__ when an exception is in flight, then
            # propagates.
            return False

    conn = MagicMock()
    conn.transaction = lambda: _FakeTx()
    state = MagicMock()
    pool = MagicMock()
    state.db_pool = pool
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    request = MagicMock()
    request.app.state.bp = state

    principal = MagicMock()
    principal.user_id = "usr_admin"

    req = admin.IssueInvitationRequest(level="tier0", expires_in_s=600)

    # The audit raise propagates out — FastAPI would convert this
    # to 500 in production. We assert the raise reaches the caller.
    with pytest.raises(_AuditBoom):
        asyncio.run(admin.issue_invitation(req, request, principal))
