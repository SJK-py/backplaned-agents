"""The Alembic history has a single consolidated `0001` baseline root.

Backplaned (and all derivatives) consolidated the pre-release incremental
migrations (0002–0008) into `0001_initial_schema`, so a fresh deployment runs
one migration to reach the baseline schema. Post-release schema changes get
fresh sequence numbers (0002+) that chain linearly off `0001` — see the
docstring in `0001_initial_schema.py`.

These invariants guard against:
  * `0001` accidentally gaining a `down_revision` (no longer the root),
  * more than one root revision (a re-introduced parallel baseline),
  * a branched / multi-head Alembic graph (ambiguous `upgrade head`).
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


def _revisions() -> list[tuple[str, str | None]]:
    """(revision, down_revision) for every migration file."""
    out: list[tuple[str, str | None]] = []
    for p in _migration_files():
        spec = importlib.util.spec_from_file_location(f"_m_{p.stem}", p)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out.append((mod.revision, mod.down_revision))
    return out


def test_consolidated_baseline_present() -> None:
    names = [p.name for p in _migration_files()]
    assert "0001_initial_schema.py" in names


def test_baseline_is_the_only_root_revision() -> None:
    """Exactly one root (`down_revision = None`), and it is `0001`."""
    roots = [rev for rev, down in _revisions() if down is None]
    assert roots == ["0001_initial_schema"], f"expected single root, got {roots}"


def test_history_is_a_single_linear_chain() -> None:
    """Every non-root migration chains off an existing revision, and no two
    migrations share a parent (no branches → a single head)."""
    revs = _revisions()
    known = {rev for rev, _ in revs}
    parents: list[str] = []
    for rev, down in revs:
        if down is None:
            continue
        assert down in known, f"{rev} chains off unknown revision {down!r}"
        parents.append(down)
    assert len(parents) == len(set(parents)), (
        f"branched history — a revision has multiple children: {parents}"
    )


def test_alembic_resolves_single_head() -> None:
    """If Alembic is installed, the script directory must resolve exactly one
    head (an unambiguous `upgrade head`)."""
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
    assert len(heads) == 1, f"expected single head, got {heads}"
