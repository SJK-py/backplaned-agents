"""Consolidated schema creates an `audit_log(actor_id, ts DESC)`
partial index.

The admin UI's `/admin/audit?actor_id=...` filter and the user /
agent detail pages' linked audit views all filter the audit_log by
`actor_id`. `audit_log_ts_idx` / `audit_log_event_idx` don't help
that filter — the plan falls back to a `ts DESC`-scan filtered by
`actor_id`, table-scan-equivalent at scale.

Historically standalone migration 0007 (which used CREATE INDEX
CONCURRENTLY + autocommit_block to avoid locking a *populated*
audit_log during an online migration). Now folded into the
consolidated `0001_initial_schema`: on the initial empty schema
the lock concern doesn't apply, so it's a plain CREATE INDEX in
the single migration transaction — the correct baseline shape.

Partial index pinned because:
  - `actor_id` is nullable for system-initiated events.
  - The admin page never queries with `actor_id IS NULL` — those
    are surfaced via `actor_kind = 'system'` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "bp_router"
    / "db"
    / "migrations"
    / "versions"
    / "0001_initial_schema.py"
)


def test_consolidated_migration_exists() -> None:
    assert _MIGRATION_PATH.exists(), "Consolidated 0001 must exist."


def test_creates_partial_index_on_actor_id_ts() -> None:
    """Pin the index column order + partial WHERE clause.

    `(actor_id, ts DESC)` covers both the equality filter and the
    sort. `WHERE actor_id IS NOT NULL` keeps the index small —
    system-event rows have null actor_id and are queried by
    `actor_kind` instead."""
    body = _MIGRATION_PATH.read_text()
    assert "CREATE INDEX audit_log_actor_ts_idx" in body
    assert "ON audit_log (actor_id, ts DESC)" in body
    assert "WHERE actor_id IS NOT NULL" in body


def test_baseline_index_is_plain_not_concurrently() -> None:
    """On the initial empty schema CONCURRENTLY is unnecessary (and
    would force an autocommit_block, splitting the otherwise-atomic
    baseline migration). Pin the plain form so a future edit doesn't
    reintroduce the online-migration shape into the baseline.

    AST-scoped to executable code — a docstring/comment may
    legitimately *explain* why CONCURRENTLY was dropped (substring
    checks would trip on that explanation)."""
    tree = ast.parse(_MIGRATION_PATH.read_text())

    # No SQL string passed to op.execute(...) may use CONCURRENTLY.
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                assert "CONCURRENTLY" not in arg.value.upper(), (
                    "baseline migration must not CREATE INDEX "
                    "CONCURRENTLY"
                )

    # And no `autocommit_block()` call anywhere in the module.
    autocommit_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "autocommit_block"
    ]
    assert not autocommit_calls, (
        "baseline migration must stay a single transaction "
        "(no autocommit_block)"
    )
