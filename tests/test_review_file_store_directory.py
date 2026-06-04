"""Router-managed file store — Phase 1: the `file_names` directory
table, its Scope query primitives, and the per-user storage quota
setting.

Implements `docs/design/router-managed-file-store.md` §3 (storage
model), §7 (quota), §11 phase 1. This phase is purely additive — it
introduces the named directory over the content-addressed `files`
blob registry.

DB-touching behaviour (insert/resolve/list/delete/quota-SUM) needs a
live Postgres and is exercised by the integration suite; here we pin
the schema shape (migration), the query surface + SQL shape (source/
AST), and the settings field + validator (pure).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

_MIGRATION = (
    Path(__file__).parent.parent
    / "bp_router" / "db" / "migrations" / "versions"
    / "0001_initial_schema.py"
)


# ---------------------------------------------------------------------------
# Migration — file_names table shape
# ---------------------------------------------------------------------------


def test_migration_creates_file_names_table() -> None:
    body = _MIGRATION.read_text()
    assert "CREATE TABLE file_names" in body
    # The PK is the atomic name-allocation guard.
    assert "PRIMARY KEY (user_id, scope, filename)" in body
    # FK to the blob registry.
    assert "file_id     text NOT NULL REFERENCES files(file_id)" in body
    # Denormalised byte_size for the quota SUM.
    assert "byte_size   bigint NOT NULL" in body
    # Refcount index on file_id.
    assert "CREATE INDEX file_names_file_idx ON file_names(file_id)" in body


def test_migration_drops_file_names_before_files() -> None:
    """FK dependency: `file_names` references `files`, so the
    downgrade must drop the child first (explicit order; CASCADE
    would also handle it but the order documents the dependency)."""
    body = _MIGRATION.read_text()
    fn_idx = body.index('"file_names",')
    files_idx = body.index('"files",')
    assert fn_idx < files_idx


def test_file_names_table_only_in_baseline() -> None:
    """The directory table is folded INTO `0001`, not added as a separate
    migration. Post-baseline migrations for OTHER features are fine (the
    single-root invariant lives in test_migrations_consolidated.py); this pins
    that `file_names` itself isn't re-introduced in a chain."""
    versions = _MIGRATION.parent
    creators = sorted(
        p.name
        for p in versions.glob("*.py")
        if p.name != "__init__.py" and "CREATE TABLE file_names" in p.read_text()
    )
    assert creators == ["0001_initial_schema.py"]


# ---------------------------------------------------------------------------
# FileNameRow model
# ---------------------------------------------------------------------------


def test_file_name_row_shape() -> None:
    from bp_router.db.models import FileNameRow

    fields = set(FileNameRow.model_fields)
    assert fields == {
        "user_id", "scope", "filename", "file_id",
        "byte_size", "created_at", "updated_at",
    }


# ---------------------------------------------------------------------------
# Scope query primitives — surface + SQL shape
# ---------------------------------------------------------------------------


def test_scope_exposes_directory_primitives() -> None:
    from bp_router.db.queries import Scope

    for name in (
        "resolve_file_name",
        "insert_file_name",
        "repoint_file_name",
        "list_file_names",
        "delete_file_name",
        "delete_file_names_glob",
        "delete_file_names_for_scope",
        "count_user_storage_bytes",
        "count_names_for_file",
    ):
        assert hasattr(Scope, name), f"Scope missing {name}"
        assert inspect.iscoroutinefunction(getattr(Scope, name))


def test_insert_file_name_is_atomic_allocation_guard() -> None:
    """`insert_file_name` uses `ON CONFLICT DO NOTHING` and reports
    whether the row landed — the PK conflict is how concurrent
    same-name stores are serialised (loser bumps the dedup counter)."""
    from bp_router.db.queries import Scope

    src = inspect.getsource(Scope.insert_file_name)
    assert "ON CONFLICT (user_id, scope, filename) DO NOTHING" in src
    # Returns True only when a row was actually inserted.
    assert 'return result.endswith(" 1")' in src


def test_quota_sum_is_user_scoped_single_table() -> None:
    """The quota usage figure is a single-table SUM over the user's
    directory rows (byte_size denormalised on file_names so no join)."""
    from bp_router.db.queries import Scope

    src = inspect.getsource(Scope.count_user_storage_bytes)
    assert "SUM(byte_size)" in src
    assert "FROM file_names" in src
    assert "WHERE user_id = $1" in src
    # No join to `files` — byte_size is denormalised.
    assert "JOIN" not in src.upper()


def test_like_escape_escapes_metachars() -> None:
    """The shared `_like_escape` helper turns the LIKE metacharacters
    (`%`, `_`) and the escape char (`\\`) literal."""
    from bp_router.db.queries import _like_escape

    assert _like_escape("a_b%c") == r"a\_b\%c"
    assert _like_escape("x\\y") == r"x\\y"
    assert _like_escape("plain.txt") == "plain.txt"


def test_glob_delete_escapes_like_metachars() -> None:
    """A `*` glob translates to SQL LIKE `%`, but literal `%`/`_` in a
    filename must be escaped (via `_like_escape`) so they aren't
    treated as wildcards — otherwise `delete('a_b')` could match
    `axb`."""
    from bp_router.db.queries import Scope

    src = inspect.getsource(Scope.delete_file_names_glob)
    assert "_like_escape(pattern)" in src
    assert '.replace("*", "%")' in src
    assert "ESCAPE" in src


def test_list_query_escapes_like_metachars() -> None:
    """`list_file_names(query=…)` is a LITERAL substring match — the
    query's `%`/`_` are escaped (via `_like_escape`) so they don't act
    as wildcards."""
    from bp_router.db.queries import Scope

    src = inspect.getsource(Scope.list_file_names)
    assert "_like_escape(query)" in src
    assert "ESCAPE" in src


def test_directory_queries_are_user_scoped() -> None:
    """Every directory primitive calls `_require_user()` — none
    operates cross-user (the named store's isolation is per-user by
    construction)."""
    from bp_router.db.queries import Scope

    for name in (
        "resolve_file_name", "insert_file_name", "repoint_file_name",
        "list_file_names", "delete_file_name", "delete_file_names_glob",
        "delete_file_names_for_scope", "count_user_storage_bytes",
        "count_names_for_file",
    ):
        src = inspect.getsource(getattr(Scope, name))
        assert "self._require_user()" in src, f"{name} not user-scoped"


# ---------------------------------------------------------------------------
# Settings — per-level storage quota
# ---------------------------------------------------------------------------


def _base_settings_kwargs() -> dict:
    return dict(
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
        metrics_token="m" * 32,
        redis_url="redis://localhost:6379/0",
    )


def test_file_storage_quota_default_shape() -> None:
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    s = Settings(**_base_settings_kwargs())
    q = s.file_storage_quota_bytes
    # Levels match the quota_admit vocabulary.
    assert set(q) == {"admin", "service", "tier0", "tier1", "tier2", "tier3"}
    # Privileged levels uncapped by default; lower tiers bounded.
    assert q["admin"] is None
    assert q["service"] is None
    assert q["tier1"] == 1024 * 1024 * 1024
    assert q["tier3"] == 64 * 1024 * 1024


def test_file_storage_quota_rejects_non_positive() -> None:
    pytest.importorskip("pydantic_settings")
    from pydantic import ValidationError

    from bp_router.settings import Settings

    with pytest.raises(ValidationError, match="must be > 0 or None"):
        Settings(
            **_base_settings_kwargs(),
            file_storage_quota_bytes={"tier1": 0},
        )
    with pytest.raises(ValidationError, match="must be > 0 or None"):
        Settings(
            **_base_settings_kwargs(),
            file_storage_quota_bytes={"tier2": -5},
        )


def test_file_storage_quota_allows_none_uncapped() -> None:
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    s = Settings(
        **_base_settings_kwargs(),
        file_storage_quota_bytes={"tier1": None},
    )
    assert s.file_storage_quota_bytes["tier1"] is None
