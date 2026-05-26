"""Tests for the admin-API correctness review fixes
(Adm-H1, Adm-H2, Adm-H3, Adm-H4, Adm-M3, Adm-M4, Adm-M5).

Covers:
  - PATCH sentinel semantics: explicit JSON null clears nullable
    columns; omitted fields stay unchanged. NOT NULL columns
    surface as 400 with the column name.
  - `assert` removed from allowlist enforcement (would be
    stripped under `python -O`); replaced with explicit raise.
  - `_ensure_utc` coerces naive datetimes on the `/audit`
    endpoint so queries against `timestamptz` aren't silently
    shifted by the Postgres session timezone.
  - `list_invitations` filters in SQL (not after pagination),
    sorts by `created_at DESC`, with `token_hash` tie-break.

Most tests are fastapi-gated source-level pins so they can run
on the CI matrix without a live router. The pure-Python helper
tests (`_ensure_utc`, the SQL shape of `list_invitations`) run
on every matrix.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

# ===========================================================================
# Adm-H4: _ensure_utc helper
# ===========================================================================


def test_ensure_utc_passes_through_aware_datetime() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _ensure_utc

    aware = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    assert _ensure_utc(aware) is aware  # exact same object


def test_ensure_utc_attaches_utc_to_naive_datetime() -> None:
    """Naive datetimes get UTC tzinfo attached. Without this, the
    /audit endpoint's `since`/`until` query params would be compared
    against `timestamptz` columns under the Postgres session
    timezone — silently shifting results around DST."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _ensure_utc

    naive = datetime(2024, 1, 15, 12, 0)
    out = _ensure_utc(naive)
    assert out is not None
    assert out.tzinfo == UTC
    # Hour and minute preserved (UTC interpretation, not converted).
    assert (out.year, out.month, out.day, out.hour, out.minute) == (
        2024, 1, 15, 12, 0,
    )


def test_ensure_utc_returns_none_for_none() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import _ensure_utc

    assert _ensure_utc(None) is None


def test_audit_endpoint_uses_ensure_utc() -> None:
    """Source-level pin: the `/audit` GET handler calls
    `_ensure_utc` on both `since` and `until` before composing the
    SQL. A regression that drops the coercion would let a naive
    datetime reach asyncpg unchanged and produce wrong results."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.get_audit_log)
    assert "_ensure_utc(since)" in src
    assert "_ensure_utc(until)" in src


# ===========================================================================
# Adm-H1, Adm-H2: PATCH sentinel semantics on update_llm_preset
# ===========================================================================


def test_update_llm_preset_uses_exclude_unset_not_exclude_none() -> None:
    """Source-level pin: `update_llm_preset` switched from
    `exclude_none=True` to `exclude_unset=True` so an explicit
    `{"description": null}` reaches the SQL UPDATE as `SET
    description = NULL`. The previous behaviour silently dropped
    the field, leaving admins unable to clear nullable columns."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_llm_preset)
    # The actual `model_dump` call uses `exclude_unset=True`. The
    # word "exclude_none" may still appear in a comment explaining
    # what we DON'T do — match the call shape, not the substring.
    assert "model_dump(exclude_unset=True)" in src
    assert "model_dump(exclude_none=True)" not in src


def test_update_llm_preset_handles_not_null_violation() -> None:
    """Source-level pin: an admin trying to clear a NOT NULL
    column (e.g. `provider`) must surface as HTTP 400 with the
    column name, not a 500 from an unhandled `NotNullViolationError`.

    The `exclude_unset=True` change above is what enables admins
    to send explicit nulls in the first place — without the matching
    error handler, those nulls would propagate to asyncpg and 500."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_llm_preset)
    assert "asyncpg.NotNullViolationError" in src
    # The handler raises 400 with the column name extracted from the
    # exception so the admin sees which field can't be cleared.
    assert "status_code=400" in src
    assert "cannot be set to null" in src


def test_update_llm_preset_clear_api_key_mutex_with_explicit_null() -> None:
    """Source-level pin: `clear_api_key=True` is incompatible with
    `api_key` being explicitly set to a non-null value. The check
    used to look at any presence of `api_key` in `raw`, but with
    `exclude_unset=True` the key now appears in raw EVEN when set
    to None (which is what `clear_api_key=True` does itself). The
    fix narrows the conflict to `api_key is not None`."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_llm_preset)
    # Conflict detection narrowed to the not-None case.
    assert 'raw["api_key"] is not None' in src


# ===========================================================================
# Adm-H3: assert -> RuntimeError for column allowlist enforcement
# ===========================================================================


@pytest.mark.parametrize("func_name", ["update_rule", "update_llm_preset"])
def test_patch_endpoint_uses_explicit_raise_not_assert(func_name: str) -> None:
    """`assert` is stripped under `python -O`. The PATCH endpoints'
    allowlist checks must use a real raise that survives
    optimization. The third-pass review's L-2 fix promoted the bare
    `RuntimeError` to a structured `HTTPException(500, ...)` so the
    client sees a clean error envelope, but the pin is the same:
    NO `assert` for the safety net."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    func = getattr(admin, func_name)
    src = inspect.getsource(func)
    # No bare `assert all(c in ...` against the patchable column set.
    assert "assert all(c in _PRESET_PATCHABLE" not in src
    assert "assert all(c in _RULE_PATCHABLE_COLUMNS" not in src
    # An explicit raise must be present for the bad-column path —
    # accept either the legacy `RuntimeError` form OR the L-2
    # `HTTPException(500, ...)` form.
    assert "raise RuntimeError" in src or "raise HTTPException(" in src


def test_update_user_uses_explicit_raise() -> None:
    """Pin the prior-PR fix: `update_user` does the allowlist
    check with a real raise (not `assert`). The third-pass review's
    L-2 fix moved this from `RuntimeError` to a structured
    `HTTPException(500, ...)` — pin both the no-assert invariant
    and the L-2 envelope."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_user)
    # Either form of explicit raise is acceptable.
    assert "raise RuntimeError" in src or "raise HTTPException(" in src
    assert "_USER_PATCHABLE_COLUMNS" in src


# ===========================================================================
# Adm-M3, Adm-M4, Adm-M5: list_invitations filter / sort / tie-break
# ===========================================================================


def test_list_invitations_query_orders_by_created_at_with_tiebreak() -> None:
    """The previous SQL ordered by `expires_at DESC` (review item
    Adm-M4 — wrong field, contradicts the docstring). Now orders
    by `created_at DESC` with `token_hash DESC` tie-break (review
    item Adm-M5) so pagination is deterministic when multiple rows
    share the same `created_at`."""
    from bp_router.db import queries

    src = inspect.getsource(queries.list_invitations)
    # New ordering shape.
    assert "ORDER BY created_at DESC, token_hash DESC" in src
    # Old ordering removed.
    assert "ORDER BY expires_at DESC" not in src


def test_list_invitations_query_filters_in_sql_not_in_python() -> None:
    """Filtering must happen INSIDE the SQL, not after pagination
    (review item Adm-M3). With Python-side filtering after
    `LIMIT 100`, requesting `?status=valid` could return zero rows
    even when many valid invitations exist if the first 100 by
    sort order all happened to be expired/used."""
    from bp_router.db import queries

    src = inspect.getsource(queries.list_invitations)
    # Function takes a status_filter arg now.
    assert "status_filter" in src
    # And builds a WHERE clause server-side.
    assert "WHERE used_at IS NULL AND expires_at > $1" in src
    assert "WHERE used_at IS NOT NULL" in src
    assert "WHERE used_at IS NULL AND expires_at <= $1" in src


def test_list_invitations_rejects_unknown_status() -> None:
    """An unknown `status_filter` value raises rather than silently
    returning unfiltered rows. The API enforces the regex at the
    FastAPI layer, but defense-in-depth here catches future call
    sites that bypass the regex."""
    import asyncio

    from bp_router.db import queries

    class _StubConn:
        async def fetch(self, *args: Any, **kwargs: Any) -> Any:
            return []

    with pytest.raises(ValueError, match="unknown status_filter"):
        asyncio.run(queries.list_invitations(
            _StubConn(),  # type: ignore[arg-type]
            status_filter="bogus",
        ))


def test_list_invitations_passes_status_to_db_layer() -> None:
    """API endpoint forwards `status_filter` to the queries layer
    instead of doing post-pagination filtering."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.list_invitations)
    # Filter forwarded to the queries layer.
    assert "status_filter=status_filter" in src
    # Old "filter views afterwards" pattern removed.
    assert "v for v in views if v.status == status_filter" not in src


# ===========================================================================
# Adm-M3 behavior: end-to-end stub test
# ===========================================================================


def test_list_invitations_filter_valid_returns_only_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral: with status_filter='valid', the SQL parameters
    include the comparison timestamp and the WHERE clause filters
    on `used_at IS NULL AND expires_at > $1`."""
    import asyncio

    from bp_router.db import queries

    captured_sql: list[str] = []
    captured_args: list[tuple] = []

    class _StubConn:
        async def fetch(self, sql: str, *args: Any) -> Any:
            captured_sql.append(sql)
            captured_args.append(args)
            return []

    now = datetime(2024, 1, 15, tzinfo=UTC)
    asyncio.run(queries.list_invitations(
        _StubConn(),  # type: ignore[arg-type]
        status_filter="valid",
        now=now,
        limit=50,
        offset=10,
    ))
    assert len(captured_sql) == 1
    sql = captured_sql[0]
    args = captured_args[0]
    assert "used_at IS NULL AND expires_at > $1" in sql
    # First arg = now timestamp; then limit, offset.
    assert args[0] == now
    assert args[1] == 50
    assert args[2] == 10


def test_list_invitations_no_filter_omits_where_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status_filter=None → no WHERE clause, all rows returned
    (subject to LIMIT/OFFSET). Smoke test that the unfiltered
    path still works."""
    import asyncio

    from bp_router.db import queries

    captured_sql: list[str] = []
    captured_args: list[tuple] = []

    class _StubConn:
        async def fetch(self, sql: str, *args: Any) -> Any:
            captured_sql.append(sql)
            captured_args.append(args)
            return []

    asyncio.run(queries.list_invitations(
        _StubConn(),  # type: ignore[arg-type]
        status_filter=None,
        limit=100,
        offset=0,
    ))
    sql = captured_sql[0]
    args = captured_args[0]
    # No WHERE clause when filter is None.
    assert "WHERE" not in sql
    # Just limit + offset.
    assert args == (100, 0)
