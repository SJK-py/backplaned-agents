"""Both Alembic histories are a single consolidated `0001` baseline.

The router (`bp_router/db/migrations`) and the agent suite
(`bp_agents/migrations`) each keep their own chain against their own
database. Both were consolidated a second time — the router absorbing what
had been 0002–0013, the suite 0002–0006 — so a fresh install runs exactly
one migration per database and lands on the final schema.

That second fold is a BREAKING change and is meant to be. A database created
by the previous chain carries an `alembic_version` naming a revision that no
longer exists, so `alembic upgrade head` fails against it instead of doing
something subtle; existing installations are recreated from empty. These
tests cannot assert that property directly — it is the absence of files —
but they do pin the shape that produces it.

The invariants guard against:
  * a stray incremental sneaking back in (the fold silently un-doing itself),
  * `0001` gaining a `down_revision` (no longer the root),
  * more than one root revision (a re-introduced parallel baseline),
  * a branched / multi-head graph (ambiguous `upgrade head`).

The last three keep their value once post-release migrations DO start
chaining off `0001`; only `test_each_chain_is_exactly_one_migration` is
specific to the consolidated moment, and it is the one to delete when the
first 0002 lands.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_CHAINS = {
    "router": _REPO / "bp_router" / "db" / "migrations",
    "suite": _REPO / "bp_agents" / "migrations",
}
_BASELINES = {
    "router": "0001_initial_schema",
    "suite": "0001_suite_initial",
}


def _migration_files(chain: str) -> list[Path]:
    return sorted(
        p
        for p in (_CHAINS[chain] / "versions").glob("*.py")
        if p.name != "__init__.py"
    )


def _revisions(chain: str) -> list[tuple[str, str | None]]:
    """(revision, down_revision) for every migration file in a chain."""
    out: list[tuple[str, str | None]] = []
    for p in _migration_files(chain):
        spec = importlib.util.spec_from_file_location(f"_m_{chain}_{p.stem}", p)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out.append((mod.revision, mod.down_revision))
    return out


@pytest.mark.parametrize("chain", sorted(_CHAINS))
def test_consolidated_baseline_present(chain: str) -> None:
    names = [p.stem for p in _migration_files(chain)]
    assert _BASELINES[chain] in names


@pytest.mark.parametrize("chain", sorted(_CHAINS))
def test_each_chain_is_exactly_one_migration(chain: str) -> None:
    """The consolidation itself. Delete this test — not the fold — when the
    first genuine post-release 0002 lands."""
    names = [p.stem for p in _migration_files(chain)]
    assert names == [_BASELINES[chain]], (
        f"{chain} chain should be the single consolidated baseline, got {names}"
    )


@pytest.mark.parametrize("chain", sorted(_CHAINS))
def test_baseline_is_the_only_root_revision(chain: str) -> None:
    """Exactly one root (`down_revision = None`), and it is `0001`."""
    roots = [rev for rev, down in _revisions(chain) if down is None]
    assert roots == [_BASELINES[chain]], f"expected single root, got {roots}"


@pytest.mark.parametrize("chain", sorted(_CHAINS))
def test_history_is_a_single_linear_chain(chain: str) -> None:
    """Every non-root migration chains off an existing revision, and no two
    migrations share a parent (no branches → a single head)."""
    revs = _revisions(chain)
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


@pytest.mark.parametrize("chain", sorted(_CHAINS))
def test_alembic_resolves_single_head(chain: str) -> None:
    """If Alembic is installed, each script directory must resolve exactly
    one head (an unambiguous `upgrade head`)."""
    pytest.importorskip("alembic")
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(_CHAINS[chain]))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected single head, got {heads}"
