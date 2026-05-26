"""`replace_acl_rules` takes a Postgres advisory xact lock.

R6 third-pass review (HIGH): pre-R6 two admins racing `PUT
/v1/admin/acl/rules` could lose-update each other. Sequence:

  - A opens transaction → `DELETE FROM acl_rules` (row-locks
    every existing rule)
  - B opens transaction → `DELETE FROM acl_rules` (blocks)
  - A inserts its full ruleset → commits
  - B unblocks → its `DELETE` now sees A's freshly-committed
    rows → succeeds (deletes A's rules) → B inserts its full
    ruleset → commits

Net: A's intent is silently overwritten. Both admins think their
edit landed; the audit log records two `acl.rules_replaced`
events but the rules in the table only reflect B's.

R6 fix: take `pg_advisory_xact_lock` at the top of the
`async with conn.transaction()` block. The lock is held for the
duration of the transaction and releases automatically on
commit/abort. A queued concurrent caller blocks at the lock
acquire, so by the time it runs its DELETE+INSERT it sees the
first admin's committed state.
"""

from __future__ import annotations

import inspect

import pytest


def test_advisory_lock_key_constant_exists() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    assert hasattr(queries, "_ACL_REPLACE_LOCK_KEY")
    # Sanity: the key fits in a 64-bit signed int (Postgres
    # advisory locks accept bigint).
    assert -(2**63) <= queries._ACL_REPLACE_LOCK_KEY < 2**63


def test_lock_key_distinct_from_audit() -> None:
    """Distinct key from `_AUDIT_LOCK_KEY` so the two locks don't
    cross-serialise — a concurrent audit write doesn't block a
    rule replace (or vice versa)."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    assert (
        queries._ACL_REPLACE_LOCK_KEY != queries._AUDIT_LOCK_KEY
    )


def test_replace_acl_rules_acquires_advisory_lock() -> None:
    """Source pin: the function calls
    `SELECT pg_advisory_xact_lock($1)` BEFORE the `DELETE FROM
    acl_rules`. Without the lock the lost-update race opens up."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.replace_acl_rules)
    assert "pg_advisory_xact_lock" in src
    assert "_ACL_REPLACE_LOCK_KEY" in src
    # Lock acquire must precede the DELETE.
    lock_idx = src.index("pg_advisory_xact_lock")
    delete_idx = src.index("DELETE FROM acl_rules")
    assert lock_idx < delete_idx


def test_advisory_lock_inside_transaction() -> None:
    """The lock must be inside the `async with conn.transaction()`
    so it releases automatically on commit / rollback. A lock
    acquired OUTSIDE the transaction would persist if the caller
    crashed before commit.

    The docstring mentions `pg_advisory_xact_lock` early; pin via
    the ACTUAL `conn.execute(...)` call site instead."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.replace_acl_rules)
    tx_idx = src.index("async with conn.transaction")
    # The call site is `conn.execute("SELECT pg_advisory_xact_lock` —
    # specific enough to skip the docstring mention.
    exec_idx = src.index(
        'conn.execute(\n            "SELECT pg_advisory_xact_lock'
    )
    assert tx_idx < exec_idx


def test_docstring_explains_lost_update_motivation() -> None:
    """Doc-pin: a future reader needs to know WHY the lock is
    there. A refactor that removes it without understanding
    re-opens the lost-update race."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    doc = queries.replace_acl_rules.__doc__ or ""
    assert "lose-update" in doc.lower() or "lost-update" in doc.lower()
    assert "advisory" in doc.lower()


def test_sort_step_still_runs_before_lock() -> None:
    """The Python-side sort is pure CPU; we don't need to hold
    the lock for it. Source-order check: sort happens before
    `async with`, lock acquire is the FIRST statement inside the
    transaction."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.replace_acl_rules)
    sort_idx = src.index("sorted_rules = sorted(rules")
    tx_idx = src.index("async with conn.transaction")
    assert sort_idx < tx_idx
