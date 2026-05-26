"""R10 CRITICAL: concurrent `alembic upgrade head` must serialise.

The documented deploy model is multi-replica (k8s / gunicorn).
Pre-R10 `bp_router/db/migrations/env.py` ran migrations with ZERO
locking, so a parallel first deploy/upgrade had several runners
race `CREATE TABLE` / the 0007-0008 `CREATE INDEX CONCURRENTLY`
migrations → schema corruption / wedged `alembic_version`. This
blocks the very first parallel deploy.

Fix: a project-fixed SESSION-scoped `pg_advisory_lock` around
`context.run_migrations()`, released in `finally`. Behavioural
testing needs a live Postgres + true concurrency (the e2e harness
that has a DB is skipped offline), so this pins the invariants by
source — the established convention for env.py-shaped changes.
"""
from __future__ import annotations

import ast
import pathlib

_ENV = (
    pathlib.Path(__file__).resolve().parent.parent
    / "bp_router" / "db" / "migrations" / "env.py"
)


def _src() -> str:
    return _ENV.read_text(encoding="utf-8")


def test_advisory_lock_acquired_and_released() -> None:
    src = _src()
    assert "pg_advisory_lock(" in src, "no advisory lock acquired"
    assert "pg_advisory_unlock(" in src, "lock never released"
    # Released in a finally so a failed migration frees it for a
    # retry / another pod (a wedged lock would block all deploys).
    assert "finally:" in src
    unlock_idx = src.index("pg_advisory_unlock(")
    finally_idx = src.rindex("finally:", 0, unlock_idx)
    try_idx = src.rindex("try:", 0, finally_idx)
    run_idx = src.index("context.run_migrations()", try_idx)
    # run_migrations is inside the try, before the finally/unlock.
    assert try_idx < run_idx < finally_idx < unlock_idx


def test_lock_is_session_scoped_not_xact() -> None:
    """Must be `pg_advisory_lock` (session), NOT
    `pg_advisory_xact_lock` — an xact lock releases at the first
    COMMIT and would NOT span the per-migration autocommit blocks
    that the 0007/0008 CONCURRENTLY migrations open."""
    # Check STRING LITERALS only (the SQL), not comments — the
    # rationale comment legitimately names `pg_advisory_xact_lock`
    # to explain why it's avoided.
    tree = ast.parse(_src())
    sql_strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not any("pg_advisory_xact_lock" in s for s in sql_strings)
    assert any("pg_advisory_lock(" in s for s in sql_strings)


def test_lock_acquired_before_run_migrations() -> None:
    """The lock must be taken BEFORE configure/begin_transaction so
    it covers the whole run (including the CONCURRENTLY autocommit
    blocks), not just part of it."""
    src = _src()
    lock_idx = src.index("pg_advisory_lock(")
    run_idx = src.index("context.run_migrations()", lock_idx)
    begin_idx = src.index("context.begin_transaction()", lock_idx)
    assert lock_idx < begin_idx < run_idx


def test_lock_key_is_a_fixed_module_constant() -> None:
    """A stable shared key — every runner must contend on the SAME
    lock. Parse the module and assert the key is a single int
    constant assigned at module scope, and the lock/unlock calls
    reference that name (not ad-hoc literals that could drift)."""
    src = _src()
    tree = ast.parse(src)
    const_names = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "_MIGRATION_ADVISORY_LOCK_KEY" in const_names
    # Both calls interpolate the constant, not a bare literal.
    assert src.count("_MIGRATION_ADVISORY_LOCK_KEY") >= 3  # def + lock + unlock
    # Key must be a positive in-range bigint.
    ns: dict = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name)
                and t.id == "_MIGRATION_ADVISORY_LOCK_KEY"
                for t in node.targets
            )
        ):
            ns["k"] = ast.literal_eval(node.value)
    assert 0 < ns["k"] < 9_223_372_036_854_775_807


def test_offline_path_not_locked() -> None:
    """`run_migrations_offline` generates SQL with no live
    connection — it cannot (and need not) take a DB lock. The lock
    belongs only to the online `do_run_migrations` path."""
    src = _src()
    off = src.index("def run_migrations_offline()")
    do_run = src.index("def do_run_migrations(")
    offline_body = src[off:do_run]
    assert "pg_advisory_lock(" not in offline_body
