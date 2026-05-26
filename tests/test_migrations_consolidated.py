"""The Alembic history is consolidated to a single `0001` baseline.

Backplaned (and all derivatives) are pre-release — no deployment
carries an intermediate schema, so the historical incremental
migrations (0002–0008) were folded into `0001_initial_schema`. A
fresh deployment runs ONE migration and lands on the final schema.

These invariants replace the per-increment chain tests that the
deleted migrations used to carry. They guard against:
  * a stray re-introduced intermediate file (splitting the head),
  * `0001` accidentally gaining a `down_revision` (no longer a root),
  * a multi-head Alembic graph (ambiguous `upgrade head`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_VERSIONS_DIR = (
    Path(__file__).parent.parent
    / "bp_router"
    / "db"
    / "migrations"
    / "versions"
)


def _migration_files() -> list[Path]:
    return sorted(
        p
        for p in _VERSIONS_DIR.glob("*.py")
        if p.name != "__init__.py"
    )


def test_exactly_one_migration_file() -> None:
    files = _migration_files()
    assert [p.name for p in files] == ["0001_initial_schema.py"], (
        f"expected a single consolidated migration, found "
        f"{[p.name for p in files]}"
    )


def test_baseline_is_a_root_revision() -> None:
    """The lone migration must be the root: `down_revision = None`
    and `revision = '0001_initial_schema'`."""
    spec = importlib.util.spec_from_file_location(
        "_m1", _VERSIONS_DIR / "0001_initial_schema.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0001_initial_schema"
    assert mod.down_revision is None


def test_no_residual_down_revision_chains() -> None:
    """No migration file may reference a prior revision — a
    consolidated history has no chain. Catches a half-reverted
    consolidation (a re-added 000N still pointing at 000(N-1))."""
    for p in _migration_files():
        body = p.read_text()
        assert 'down_revision = "' not in body, (
            f"{p.name} declares a down_revision — the history must "
            f"stay consolidated to the single 0001 root"
        )


def test_alembic_resolves_single_head() -> None:
    """If Alembic is installed, the script directory must resolve
    exactly one head (an unambiguous `upgrade head`)."""
    pytest.importorskip("alembic")
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).parent.parent / "bp_router" / "db" / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["0001_initial_schema"], f"expected single head, got {heads}"
