"""`acl_rules` CHECK accepts Phase-10 prefix-globs (`llm.*`).

Pre-fix: `is_valid_pattern` (application layer) accepted prefix-
globs but the column-level CHECK rejected them — POST
/v1/admin/acl/rules with a Phase 10 rule 500'ed (CheckViolation).
The fix was historically standalone migration 0008; it is now
folded into the consolidated `0001_initial_schema` baseline
(pre-release migration consolidation — no deployment carries an
intermediate schema). These tests pin the regex shape + the
named-constraint form on the consolidated migration, source-pin
style (no live Postgres needed).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "bp_router"
    / "db"
    / "migrations"
    / "versions"
    / "0001_initial_schema.py"
)


def _load_migration():
    """Import the consolidated migration by file path so we can read
    the `_ACL_PATTERN_REGEX` constant verbatim (the versions dir is
    not on `sys.path`)."""
    spec = importlib.util.spec_from_file_location(
        "_migration_0001", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_consolidated_migration_exists() -> None:
    assert _MIGRATION_PATH.exists()


def test_acl_pattern_regex_accepts_prefix_glob() -> None:
    """The CHECK regex must accept `<group>/<prefix>.*` for every
    valid prefix in `_CAPABILITY_PREFIX_PATTERN`. We compile the
    EXACT regex the migration installs — guards against a
    paraphrased copy drifting from what Postgres enforces."""
    pat = re.compile(_load_migration()._ACL_PATTERN_REGEX)
    # Phase 10 prefix-globs.
    assert pat.match("*/llm.*")
    assert pat.match("rank0/llm.*")
    assert pat.match("billing/admin.audit.*")
    # And the old full-cap forms still work.
    assert pat.match("rank0/llm.generate")
    assert pat.match("*/echo.invoke")
    # Whole-token `*` still works for the cap half.
    assert pat.match("*/*")
    assert pat.match("rank0/*")
    # `@<agent_id>` form still works.
    assert pat.match("@agt_alice")


def test_acl_pattern_regex_rejects_invalid_glob_shapes() -> None:
    """Phase 10 spec: prefix-globs MUST end in `.*`. Leading
    globs, middle globs, and double-stars are deliberately
    rejected to keep precedence semantics predictable."""
    pat = re.compile(_load_migration()._ACL_PATTERN_REGEX)
    # Leading glob — rejected.
    assert not pat.match("*/.llm.foo")
    # Middle glob — rejected.
    assert not pat.match("rank0/llm.*.foo")
    # Double star — rejected.
    assert not pat.match("rank0/llm.**")
    # Bare `.*` — rejected (no leading segment).
    assert not pat.match("rank0/.*")
    # Empty cap half — rejected.
    assert not pat.match("rank0/")


def test_acl_pattern_uses_explicit_named_constraints() -> None:
    """The caller/callee CHECKs are added as explicitly-named
    constraints (`acl_rules_<col>_pattern_check`) via ALTER, not
    inline column CHECKs — so the baseline carries a stable handle
    for any future relaxation rather than a Postgres auto-name."""
    body = _MIGRATION_PATH.read_text()
    assert "ADD CONSTRAINT acl_rules_caller_pattern_check" in body
    assert "ADD CONSTRAINT acl_rules_callee_pattern_check" in body
    # And the column itself must NOT carry an inline pattern CHECK
    # (that would Postgres-auto-name a second, redundant constraint).
    assert "caller_pattern  text NOT NULL," in body
    assert "callee_pattern  text NOT NULL," in body


def test_application_validator_accepts_what_db_now_accepts() -> None:
    """Symmetric check: every shape `is_valid_pattern` accepts
    must also pass the DB regex. Catches a future divergence
    where someone relaxes the app layer without also relaxing
    the DB."""
    pytest.importorskip("asyncpg")
    from bp_router.acl import is_valid_pattern

    pat = re.compile(_load_migration()._ACL_PATTERN_REGEX)

    samples = [
        "*/*",
        "rank0/llm.generate",
        "rank0/llm.*",
        "*/admin.audit.read",
        "@agt_alice",
        "billing/admin.audit.*",
    ]
    for s in samples:
        assert is_valid_pattern(s), f"app: {s!r}"
        assert pat.match(s), f"db: {s!r}"
