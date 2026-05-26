"""R9 MEDIUM-1: `complete_task`'s task lookup is `FOR UPDATE` inside
the same transaction as the state transition.

Pre-R9 `complete_task` did a non-`FOR UPDATE` `fetchrow` of
`active_agent_id` in autocommit, BEFORE opening the
`conn.transaction()` for `task_transition`. The
`reporting_agent_id != active_agent_id` auth check used that
unlocked snapshot. `_admit_delegation` Phase C also takes
`SELECT … FOR UPDATE` on the same row to flip `active_agent_id`;
a flip committing between the unlocked read and the transition
dropped a legitimate Result from the new active executor and the
task hung until the deadline sweep (R8 PR #196 only made it
countable via `result_from_wrong_agent_total`).

This pins the principled fix: the lookup is `FOR UPDATE` and
lives in the SAME transaction as the auth check and
`task_transition`, so it serialises against Phase C's lock —
either `complete_task` locks first (Phase C re-reads, sees
terminal, refuses the delegation) or Phase C locks first (flip
commits, this read sees the post-flip active agent, Result
lands). No lost Result either way.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _src() -> str:
    pytest.importorskip("fastapi")
    from bp_router import tasks

    return inspect.getsource(tasks.complete_task)


def test_task_lookup_uses_for_update() -> None:
    src = _src()
    assert "FROM tasks WHERE task_id = $1 FOR UPDATE" in src
    # The pre-R9 unlocked form must be gone.
    assert "FROM tasks WHERE task_id = $1\"" not in src.replace(
        "FOR UPDATE", "FOR UPDATE"
    )


def test_lookup_auth_and_transition_share_one_transaction() -> None:
    """AST: the `fetchrow(... FOR UPDATE)`, the
    `reporting_agent_id != active_agent_id` auth check, and the
    `task_transition(...)` call must all sit inside ONE
    `async with conn.transaction():` — not split across the
    autocommit/transaction boundary, and not in a nested inner
    transaction."""
    src = textwrap.dedent(_src())
    tree = ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)

    # Locate the single conn.transaction() AsyncWith.
    txn_withs = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "transaction"
            for item in n.items
        )
    ]
    assert len(txn_withs) == 1, (
        f"expected exactly ONE conn.transaction() block in "
        f"complete_task, found {len(txn_withs)} — the read/auth/"
        f"transition must not be split, and the redundant nested "
        f"transaction must be removed"
    )
    txn = txn_withs[0]

    def _contains(node: ast.AST, pred) -> bool:  # type: ignore[no-untyped-def]
        return any(pred(n) for n in ast.walk(node))

    # FOR UPDATE fetch inside the txn.
    assert _contains(
        txn,
        lambda n: isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "FOR UPDATE" in n.value,
    ), "the FOR UPDATE lookup must be inside the transaction"

    # task_transition call inside the txn.
    assert _contains(
        txn,
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "task_transition",
    ), "task_transition must be inside the same transaction"

    # The auth-check comparison inside the txn.
    assert _contains(
        txn,
        lambda n: isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Name)
        and n.left.id == "reporting_agent_id",
    ), "the reporting_agent_id auth check must be inside the txn"


def test_no_unlocked_active_agent_read() -> None:
    """Regression guard: there must be no bare `fetchrow` of the
    task row that is NOT `FOR UPDATE` (the exact pre-R9 bug)."""
    src = _src()
    # Every SELECT of the task row in complete_task must carry
    # FOR UPDATE. The only `FROM tasks WHERE task_id` SELECT is the
    # locked one.
    import re

    selects = re.findall(r"FROM tasks WHERE task_id = \$1[^\"']*", src)
    assert selects, "task lookup SELECT not found — test stale"
    for s in selects:
        assert "FOR UPDATE" in s, (
            f"found an unlocked task-row SELECT in complete_task: {s!r}"
        )


def test_fanout_uses_captured_parent_task_id() -> None:
    """`parent_task_id` is captured from the locked row and used in
    the post-release fan-out (not re-read off `row` after the conn
    is gone, and not a second query)."""
    src = _src()
    assert "parent_task_id = row[\"parent_task_id\"]" in src
    assert "parent_task_id=parent_task_id" in src
    assert 'parent_task_id=row["parent_task_id"]' not in src


def test_metric_still_incremented_on_wrong_agent() -> None:
    """The #196 observability counter is retained (the FOR UPDATE
    fix closes the race, but a genuinely misbehaving reporter
    should still be counted)."""
    src = _src()
    assert "result_from_wrong_agent_total.labels(" in src
    assert 'reporter=' in src
