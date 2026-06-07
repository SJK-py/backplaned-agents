"""`replace_acl_rules` honours the supplied `ord` field for ordering.

R4 second-pass review found that `replace_acl_rules` silently
ignored the `ord` on every input rule and renumbered them via
`enumerate(rules)`. An admin submitting

    [{"ord": 10, ...A},
     {"ord":  5, ...B}]

saw rule A evaluated FIRST (ord=0 after renumber) despite
specifying ord=10 — contradicting the documented "lower ord
wins" first-match-wins semantics in docs/backplaned/acl.md §4.

R4 fix: sort by `ord` ascending before insert; dense-pack
storage at consecutive 0..N-1 so the relative order matches
the admin's intent and a future targeted UPDATE works on
contiguous integers.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_replace_acl_rules_sorts_by_ord_before_insert() -> None:
    """Functional pin: rules submitted out of `ord` order are
    inserted in `ord`-ascending order."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    rules = [
        {"ord": 30, "name": "C", "effect": "allow", "user_level": "*",
         "caller_pattern": "*/*", "callee_pattern": "*/*"},
        {"ord": 10, "name": "A", "effect": "deny", "user_level": "*",
         "caller_pattern": "*/*", "callee_pattern": "*/*"},
        {"ord": 20, "name": "B", "effect": "allow", "user_level": "*",
         "caller_pattern": "*/*", "callee_pattern": "*/*"},
    ]

    conn = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    asyncio.run(
        queries.replace_acl_rules(conn, rules, created_by="usr_admin")
    )

    # Filter to the INSERT calls (skip the leading DELETE).
    inserts = [
        c for c in conn.execute.call_args_list
        if "INSERT INTO acl_rules" in str(c.args[0])
    ]
    assert len(inserts) == 3

    # The `name` is the 4th positional arg (after rule_id, ord, name).
    # `ord` is the 2nd positional arg.
    insert_names = [c.args[3] for c in inserts]
    insert_ords = [c.args[2] for c in inserts]

    # Rules were sorted by ord ASCENDING — A (ord=10) first.
    assert insert_names == ["A", "B", "C"]
    # Storage is dense-packed at consecutive 0..N-1.
    assert insert_ords == [0, 1, 2]


def test_replace_acl_rules_stable_sort_preserves_caller_order_on_tie() -> None:
    """When two rules share the same `ord`, caller list order
    breaks the tie. Standard library `sorted` is stable; pin the
    behaviour so a future swap to an unstable sort fails here."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    rules = [
        {"ord": 5, "name": "first_in_list", "effect": "allow",
         "user_level": "*", "caller_pattern": "*/*", "callee_pattern": "*/*"},
        {"ord": 5, "name": "second_in_list", "effect": "deny",
         "user_level": "*", "caller_pattern": "*/*", "callee_pattern": "*/*"},
    ]

    conn = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    asyncio.run(
        queries.replace_acl_rules(conn, rules, created_by="usr_admin")
    )

    inserts = [
        c for c in conn.execute.call_args_list
        if "INSERT INTO acl_rules" in str(c.args[0])
    ]
    insert_names = [c.args[3] for c in inserts]
    assert insert_names == ["first_in_list", "second_in_list"]


def test_replace_acl_rules_handles_missing_ord_field() -> None:
    """Defensive: a rule dict without `ord` should default to 0
    rather than raising — covers any caller that bypasses the
    Pydantic request model."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    rules = [
        {"name": "no_ord_rule", "effect": "allow",
         "user_level": "*", "caller_pattern": "*/*", "callee_pattern": "*/*"},
    ]

    conn = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    out = asyncio.run(
        queries.replace_acl_rules(conn, rules, created_by="usr_admin")
    )
    assert out == 1


def test_replace_acl_rules_source_pin_sorts_by_ord() -> None:
    """Source pin: a future refactor that removes the sort step
    re-introduces the documented bug — fails this pin."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.replace_acl_rules)
    assert "sorted(rules, key=" in src
    # And the docstring documents the new contract.
    assert "HONOURED" in src or "honour" in src.lower() or "honor" in src.lower()
