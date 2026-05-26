"""AgentInfoUpdate read-modify-write is row-locked + transactional.

Backstory: Phase 10e added `_handle_agent_info_update`. The original
shape was:

    async with pool.acquire() as conn:
        row = await queries.get_agent(conn, agent_id)   # SELECT (no lock)
        ...                                              # python merge
        async with conn.transaction():
            await queries.update_agent_info(conn, ...)   # UPDATE
            await queries.append_audit_event(conn, ...)

The SELECT was outside the transaction AND used a plain
`SELECT * FROM agents WHERE agent_id = $1`. Two concurrent
AgentInfoUpdate frames for the same agent (the per-agent rate
limit defaults to burst=5) each read the same pre-patch row, each
merged their own patch onto it, and the second `update_agent_info`
clobbered the first's field changes (lost-update).

The fix wraps the SELECT in `conn.transaction()` and uses
`SELECT ... FOR UPDATE` to serialize concurrent updates. These
tests pin both pieces.
"""

from __future__ import annotations

import inspect

import pytest


def test_get_agent_for_update_query_exists() -> None:
    """`queries.get_agent_for_update` is the row-locking variant
    `_handle_agent_info_update` calls. It MUST emit `FOR UPDATE`
    on the SELECT so two concurrent transactions serialise."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    assert hasattr(queries, "get_agent_for_update"), (
        "queries must expose `get_agent_for_update` — the FOR UPDATE "
        "variant of `get_agent` used by the AgentInfoUpdate handler."
    )
    src = inspect.getsource(queries.get_agent_for_update)
    assert "FOR UPDATE" in src, (
        "get_agent_for_update SELECT must include `FOR UPDATE` — "
        "otherwise no row-level lock and the AgentInfoUpdate race "
        "stays open."
    )


def test_handler_uses_for_update_variant() -> None:
    """`_handle_agent_info_update` calls `get_agent_for_update`
    (NOT plain `get_agent`). Source pin so a refactor that
    accidentally drops the lock surfaces immediately."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "get_agent_for_update" in src, (
        "Handler must use the row-locking variant — replacing it "
        "with plain `get_agent` re-opens the lost-update race."
    )
    # And does NOT call the unlocked `get_agent` for the same row.
    # Allow get_agent calls in other places; this pin only enforces
    # that the line which reads the row for the merge is locked.
    select_calls = [
        line for line in src.splitlines()
        if "queries.get_agent" in line and "for_update" not in line
    ]
    # Filter out the helper name itself (`get_agent_for_update`
    # contains the substring `queries.get_agent` only because of
    # the substring match — the previous check already required
    # `get_agent_for_update` is used; this filter guards against an
    # unlocked `queries.get_agent(...)` call sneaking back in.)
    bare = [
        line for line in select_calls
        if "queries.get_agent(" in line
    ]
    assert not bare, (
        "Handler must not call the unlocked `queries.get_agent(...)` "
        f"for the merge. Found: {bare!r}"
    )


def test_handler_wraps_select_and_update_in_one_transaction() -> None:
    """The SELECT FOR UPDATE and the UPDATE must share ONE
    `conn.transaction()` block. If the SELECT runs outside the
    transaction, the lock is released immediately on row return and
    the race is back. Source pin: the SELECT-line appears BELOW
    the `async with conn.transaction():` line."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    lines = src.splitlines()
    txn_line = next(
        (i for i, line in enumerate(lines)
         if "async with conn.transaction():" in line),
        -1,
    )
    select_line = next(
        (i for i, line in enumerate(lines)
         if "get_agent_for_update(" in line),
        -1,
    )
    update_line = next(
        (i for i, line in enumerate(lines)
         if "update_agent_info(" in line),
        -1,
    )
    assert txn_line >= 0, "conn.transaction() block must exist"
    assert select_line >= 0, "get_agent_for_update call must exist"
    assert update_line >= 0, "update_agent_info call must exist"
    assert txn_line < select_line < update_line, (
        f"Order must be transaction -> SELECT FOR UPDATE -> UPDATE; "
        f"got transaction@{txn_line}, select@{select_line}, "
        f"update@{update_line}"
    )


def test_handler_for_update_called_inside_transaction_async_with() -> None:
    """Functional pin: the SELECT FOR UPDATE call is INDENTED under
    the `async with conn.transaction():` line. A regression that
    nested differently (e.g. moved the SELECT to a sibling block)
    breaks the lock-then-update sequence."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    lines = src.splitlines()
    txn_idx = next(
        (i for i, line in enumerate(lines)
         if "async with conn.transaction():" in line),
        -1,
    )
    assert txn_idx >= 0
    txn_indent = len(lines[txn_idx]) - len(lines[txn_idx].lstrip())
    # The next non-blank line after the txn header should be
    # indented strictly more than the txn line.
    for line in lines[txn_idx + 1:]:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        child_indent = len(line) - len(stripped)
        assert child_indent > txn_indent, (
            f"First child of conn.transaction() should be indented; "
            f"got {child_indent} vs txn indent {txn_indent}"
        )
        # And the first child must be the SELECT FOR UPDATE.
        assert "get_agent_for_update" in line, (
            f"First inside the txn must be the SELECT FOR UPDATE; "
            f"got {line.strip()!r}"
        )
        break
