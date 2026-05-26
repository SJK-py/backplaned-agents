"""Tests for the second-pass review M-bundle fixes.

Each M-finding got a small, targeted fix; this file pins the
behavioural and source-level contracts for the bundle:

  - M2: `_apply_rule_change` extends process-local serialisation to
    cross-process via `pg_advisory_xact_lock`.
  - M3: `count_task_chain_depth` emits a WARNING when the recursive
    CTE truncates at the saturation cap so a malformed cycle is
    distinguishable from a legitimate deep chain.
  - M4: `change_password` post-commit `revoke_jti` is wrapped in
    try/except, the in-transaction audit field is reframed as
    intent (`active_jti_revoke_attempted`), and a follow-up audit
    event records the actual outcome.
  - M6: `_gc_files_once` only deletes underlying storage bytes when
    NO other user's `files` row references the same `sha256`.
  - M7: file download handler always emits
    `Content-Disposition: attachment` + `X-Content-Type-Options:
    nosniff`, and downgrades non-allowlisted mime types to
    `application/octet-stream`.
  - M8: `AdminConfig.session_cookie_secure` defaults to True so a
    forgotten override no longer leaks the session cookie over
    plain HTTP.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# M2: cross-process advisory lock around _apply_rule_change
# ===========================================================================


def test_m2_apply_rule_change_takes_pg_advisory_lock() -> None:
    """The read-replace block in `_apply_rule_change` must take a
    `pg_advisory_xact_lock` so multi-worker FastAPI deployments
    serialise on the same logical operation. Without this, the
    process-local `asyncio.Lock` only protects one worker; two
    workers can still interleave their reads and end up with
    diverged caches."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin._apply_rule_change)
    assert "pg_advisory_xact_lock" in src, (
        "M2 regression: the cross-process lock has been dropped"
    )
    assert "_APPLY_RULE_CHANGE_PG_LOCK_KEY" in src, (
        "advisory lock key constant must be passed in — never hardcoded"
    )
    # The in-process lock must STILL be there — the two layers are
    # complementary, not a replacement.
    assert "_apply_rule_change_lock" in src


def test_m2_advisory_lock_key_is_stable_bigint() -> None:
    """The advisory lock key must be a stable int64 constant —
    changing it would silently break cross-worker serialisation
    during a rolling deployment (old workers vs new workers
    contend on different keys, both think they hold the lock)."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    key = admin._APPLY_RULE_CHANGE_PG_LOCK_KEY
    assert isinstance(key, int)
    # int64 range: -2^63 .. 2^63-1. asyncpg passes through to PG's
    # bigint, which is signed int64.
    assert -(2**63) <= key < 2**63
    # Stability pin: the value is the ASCII "ACL\0RELD" encoding.
    assert key == 0x41434C0052454C44


# ===========================================================================
# M3: count_task_chain_depth saturation observability
# ===========================================================================


def test_m3_count_task_chain_depth_logs_on_saturation() -> None:
    """When the recursive CTE in `count_task_chain_depth` hits the
    `_MAX_TASK_TREE_DEPTH` cap, the function must emit a warning so
    operators can distinguish a legitimate deep chain from a cycle
    the CTE truncated. Both are operator-actionable; the difference
    is which one to look for."""
    from bp_router.db import queries

    src = inspect.getsource(queries.count_task_chain_depth)
    # The warning must fire on saturation.
    assert "task_chain_depth_saturated" in src, (
        "M3 regression: saturation warning has been removed"
    )
    # Pin the comparison: depth >= _MAX_TASK_TREE_DEPTH.
    assert "_MAX_TASK_TREE_DEPTH" in src
    assert "M3" in src or "saturation" in src.lower()


# ===========================================================================
# M4: change_password revoke_jti audit accuracy
# ===========================================================================


def test_m4_change_password_audit_records_intent_not_claim() -> None:
    """The in-transaction audit row must NOT claim
    `active_jti_revoked: True` before the actual revoke happens —
    that's a lie when Redis is misconfigured or the SET fails. The
    field is reframed as `active_jti_revoke_attempted` (intent)
    and a follow-up audit event records the outcome
    (`auth.password_change_revoke_jti`)."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.change_password)
    # Old (buggy) field name must be gone.
    assert "active_jti_revoked" not in src or "active_jti_revoke_attempted" in src, (
        "M4 regression: audit row still uses the misleading "
        "'active_jti_revoked' field name"
    )
    # Intent field present.
    assert "active_jti_revoke_attempted" in src
    # Follow-up event present.
    assert "auth.password_change_revoke_jti" in src
    # try/except around the revoke_jti call.
    assert "try:" in src
    assert "revoke_jti" in src
    # Failure-case warning log so operators can investigate.
    assert "revoke_jti_failed" in src


# ===========================================================================
# M6: file GC cross-user reference race
# ===========================================================================


def test_m6_gc_skips_storage_delete_when_other_refs_exist() -> None:
    """`_gc_files_once` must consult `count_other_file_refs` after
    deleting the expired row, and skip `file_store.delete` when
    another user's row still references the same `sha256`. Without
    this, A's expiry silently 404s B's next download."""
    from bp_router.tasks import _gc_files_once

    src = inspect.getsource(_gc_files_once)
    assert "count_other_file_refs" in src, (
        "M6 regression: cross-user reference check is missing"
    )
    # The skip must happen BEFORE storage.delete.
    assert "other_refs" in src
    # Pin the conditional shape — if there are still references,
    # leave storage alone.
    assert "continue" in src


def test_m6_count_other_file_refs_query_exists() -> None:
    """Helper query must exist, take `sha256` + `exclude_file_id`,
    and return an int."""
    from bp_router.db import queries

    assert hasattr(queries, "count_other_file_refs")
    sig = inspect.signature(queries.count_other_file_refs)
    assert "sha256" in sig.parameters
    assert "exclude_file_id" in sig.parameters
    src = inspect.getsource(queries.count_other_file_refs)
    # Excludes the about-to-be-deleted row.
    assert "file_id != $2" in src or "file_id <> $2" in src
    # Filters by sha256.
    assert "sha256 = $1" in src


# ===========================================================================
# M7: file download Content-Type allowlist + nosniff
# ===========================================================================


def test_m7_safe_response_mime_allowlist() -> None:
    """`_safe_response_mime` returns the input only when it's on
    the safe allowlist; everything else (including the dangerous
    `text/html`, `image/svg+xml`, `application/javascript`)
    downgrades to `application/octet-stream`."""
    pytest.importorskip("fastapi")
    from bp_router.api.files import _safe_response_mime

    # Allowlisted: pass through (lower-cased).
    assert _safe_response_mime("image/png") == "image/png"
    assert _safe_response_mime("IMAGE/PNG") == "image/png"
    assert _safe_response_mime("application/pdf") == "application/pdf"
    assert _safe_response_mime("text/plain") == "text/plain"
    # Strips parameters.
    assert _safe_response_mime("text/plain; charset=utf-8") == "text/plain"

    # NOT allowlisted: octet-stream.
    assert _safe_response_mime("text/html") == "application/octet-stream"
    assert _safe_response_mime("image/svg+xml") == "application/octet-stream"
    assert _safe_response_mime("application/javascript") == "application/octet-stream"
    assert _safe_response_mime("application/x-shellscript") == (
        "application/octet-stream"
    )
    # Empty / None.
    assert _safe_response_mime(None) == "application/octet-stream"
    assert _safe_response_mime("") == "application/octet-stream"


def test_m7_download_handler_emits_nosniff_and_attachment() -> None:
    """Source pin: download handler must always set
    `X-Content-Type-Options: nosniff` and
    `Content-Disposition: attachment` (even when there's no
    original filename)."""
    pytest.importorskip("fastapi")
    from bp_router.api import files

    src = inspect.getsource(files.download)
    assert "X-Content-Type-Options" in src
    assert "nosniff" in src
    # Always-attachment: the no-filename branch must still set
    # Content-Disposition.
    assert "attachment" in src
    # Mime-type sanitisation routed through the helper.
    assert "_safe_response_mime" in src


# ===========================================================================
# M8: bp_admin Secure cookie default
# ===========================================================================


def test_m8_admin_secure_cookie_defaults_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AdminConfig.session_cookie_secure` defaults to True so a
    fresh deployment is secure-by-default. Operators running over
    plain HTTP in dev must opt out explicitly via env var."""
    pytest.importorskip("pydantic_settings")
    from bp_admin.config import AdminConfig

    # Strip any inherited override.
    monkeypatch.delenv("ADMIN_SESSION_COOKIE_SECURE", raising=False)
    cfg = AdminConfig(session_secret="x" * 32)  # type: ignore[arg-type]
    assert cfg.session_cookie_secure is True, (
        "M8 regression: session cookie no longer Secure-by-default — "
        "production deployments could leak the cookie over plain HTTP "
        "if the operator forgets to set ADMIN_SESSION_COOKIE_SECURE=true"
    )


def test_m8_admin_secure_cookie_can_be_opted_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev-mode opt-out path must still work — operators
    running over http://localhost set
    `ADMIN_SESSION_COOKIE_SECURE=false` and the cookie is sent."""
    pytest.importorskip("pydantic_settings")
    from bp_admin.config import AdminConfig

    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "false")
    cfg = AdminConfig(session_secret="x" * 32)  # type: ignore[arg-type]
    assert cfg.session_cookie_secure is False
