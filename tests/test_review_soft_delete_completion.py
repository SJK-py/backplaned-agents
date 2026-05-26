"""Soft-delete is enforced at every authenticated boundary.

Phase 9b added `users.deleted_at` + the four-step `soft_delete_user`
cascade, plus checks in login / refresh / reset_password and the
mint endpoints. A pre-ship review surfaced five OTHER paths that
silently still authorised soft-deleted users — covered here:

  1. `_principal_from_request` (security/jwt.py) — the session-JWT
     dependency every authenticated route uses. A user deleted
     AFTER their JWT was issued passed through until natural exp.
  2. `change_password` (api/auth.py) — fetched the user but only
     404'd on `is None`; no lifecycle check.
  3. `resolve_user_level` (llm/service.py) — checked suspended_at
     only; a deleted user's cached level satisfied tier gates.
  4. `test_task` (api/admin.py) — checked suspended_at only.
  5. `delete_user` (api/admin.py) — did not invalidate the LLM
     service's level cache, so a deleted user's cached tier
     persisted up to 10 min after the soft-delete.

These tests pin the fixes.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# 1. JWT principal boundary
# ===========================================================================


def test_principal_from_request_refuses_deleted_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_principal_from_request` consults `users.deleted_at` after
    the JTI revocation check. A deleted user's still-valid JWT is
    rejected with 401."""
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.security import jwt

    monkeypatch.setattr(
        jwt, "verify_token",
        lambda *a, **kw: {
            "sub": "usr_deleted",
            "jti": "j1",
            "level": "tier1",
            "exp": int(
                (datetime.now(UTC) + timedelta(hours=1)).timestamp()
            ),
        },
    )
    monkeypatch.setattr(
        jwt, "is_jti_revoked", AsyncMock(return_value=False)
    )

    # Stub queries.get_user_by_id → user is soft-deleted.
    from bp_router.db import queries
    deleted_user = MagicMock()
    deleted_user.deleted_at = datetime.now(UTC)
    monkeypatch.setattr(
        queries, "get_user_by_id", AsyncMock(return_value=deleted_user)
    )

    request = MagicMock()
    request.headers = {"authorization": "Bearer x"}
    state = MagicMock()
    # Force a cache miss so the DB-fetch + deleted_at branch runs.
    state.llm_service.peek_user_level_cached.return_value = None
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.redis = MagicMock()
    request.app.state.bp = state

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(jwt._principal_from_request(request))
    assert exc_info.value.status_code == 401


def test_principal_from_request_refuses_missing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user whose row was hard-deleted between JWT issue and now
    is also refused. Covers admin DB cleanup races."""
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.security import jwt

    monkeypatch.setattr(
        jwt, "verify_token",
        lambda *a, **kw: {
            "sub": "usr_gone",
            "jti": "j1",
            "level": "tier1",
            "exp": int(
                (datetime.now(UTC) + timedelta(hours=1)).timestamp()
            ),
        },
    )
    monkeypatch.setattr(
        jwt, "is_jti_revoked", AsyncMock(return_value=False)
    )

    from bp_router.db import queries
    monkeypatch.setattr(
        queries, "get_user_by_id", AsyncMock(return_value=None)
    )

    request = MagicMock()
    request.headers = {"authorization": "Bearer x"}
    state = MagicMock()
    # Force a cache miss so the DB-fetch + deleted_at branch runs.
    state.llm_service.peek_user_level_cached.return_value = None
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.redis = MagicMock()
    request.app.state.bp = state

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(jwt._principal_from_request(request))
    assert exc_info.value.status_code == 401


def test_principal_from_request_allows_active_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-check companion: an active (non-deleted) user passes
    through. Asserts the SessionPrincipal is built correctly."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt

    monkeypatch.setattr(
        jwt, "verify_token",
        lambda *a, **kw: {
            "sub": "usr_active",
            "jti": "j1",
            "level": "tier1",
            "exp": int(
                (datetime.now(UTC) + timedelta(hours=1)).timestamp()
            ),
        },
    )
    monkeypatch.setattr(
        jwt, "is_jti_revoked", AsyncMock(return_value=False)
    )

    from bp_router.db import queries
    active_user = MagicMock()
    active_user.deleted_at = None
    monkeypatch.setattr(
        queries, "get_user_by_id", AsyncMock(return_value=active_user)
    )

    request = MagicMock()
    request.headers = {"authorization": "Bearer x"}
    state = MagicMock()
    # Cache miss: force the DB-fetch path.
    state.llm_service.peek_user_level_cached.return_value = None
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.redis = MagicMock()
    request.app.state.bp = state

    principal = asyncio.run(jwt._principal_from_request(request))
    assert principal.user_id == "usr_active"
    assert principal.level == "tier1"


def test_principal_from_request_cache_hit_skips_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast-path: a cached fresh user-level entry means the user was
    active when cached. The DB lookup is skipped entirely. Pins the
    perf-critical short-circuit added in the simplify pass."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt

    monkeypatch.setattr(
        jwt, "verify_token",
        lambda *a, **kw: {
            "sub": "usr_hot",
            "jti": "j1",
            "level": "tier1",
            "exp": int(
                (datetime.now(UTC) + timedelta(hours=1)).timestamp()
            ),
        },
    )
    monkeypatch.setattr(
        jwt, "is_jti_revoked", AsyncMock(return_value=False)
    )

    # If the DB lookup is taken, this raises — the test fails
    # immediately, surfacing the regression.
    from bp_router.db import queries

    def _should_not_be_called(*a, **kw):
        raise AssertionError(
            "DB lookup must be skipped on cache hit — "
            "peek_user_level_cached returned a non-None level"
        )

    monkeypatch.setattr(queries, "get_user_by_id", _should_not_be_called)

    request = MagicMock()
    request.headers = {"authorization": "Bearer x"}
    state = MagicMock()
    # Cache HIT: peek returns a non-None level.
    state.llm_service.peek_user_level_cached.return_value = "tier1"
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.db_pool = MagicMock()
    state.redis = MagicMock()
    request.app.state.bp = state

    principal = asyncio.run(jwt._principal_from_request(request))
    assert principal.user_id == "usr_hot"
    # The cache lookup was called exactly once with the right user_id.
    state.llm_service.peek_user_level_cached.assert_called_once_with("usr_hot")


# ===========================================================================
# 2. change_password
# ===========================================================================


def test_change_password_source_pin_refuses_deleted_and_suspended() -> None:
    """`change_password` rejects deleted + suspended users via the
    `queries.user_is_active` helper. The helper folds both
    lifecycle flags into one predicate so a regression that adds
    a new flag covers every call site at once."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.change_password)
    assert "user_is_active(" in src


# ===========================================================================
# 3. resolve_user_level — refuses deleted (mirrors suspended)
# ===========================================================================


def test_resolve_user_level_refuses_deleted_user() -> None:
    """`resolve_user_level` returns None for a deleted user — tier
    gates that consult this then deny every preset except
    `min_user_level='*'`."""
    pytest.importorskip("fastapi")

    async def _run() -> None:
        from bp_router.llm.service import LlmService

        svc = LlmService.__new__(LlmService)
        svc._presets = {}  # type: ignore[attr-defined]
        svc._adapters = {}  # type: ignore[attr-defined]
        svc._user_level_cache = {}  # type: ignore[attr-defined]
        from collections import OrderedDict  # noqa: PLC0415
        svc._user_level_cache = OrderedDict()  # type: ignore[attr-defined]

        from bp_router.db import queries  # noqa: PLC0415

        deleted_user = MagicMock()
        deleted_user.suspended_at = None
        deleted_user.deleted_at = datetime.now(UTC)
        deleted_user.level = "tier1"

        orig = queries.get_user_by_id

        async def fake_get(conn, user_id):
            return deleted_user

        queries.get_user_by_id = fake_get  # type: ignore[assignment]
        try:
            conn = MagicMock()
            level = await svc.resolve_user_level(conn, "usr_x")
            assert level is None, (
                "resolve_user_level must refuse deleted users by "
                "returning None"
            )
            assert "usr_x" not in svc._user_level_cache, (
                "Don't cache the None — admin un-delete should "
                "(in theory) take effect on next invalidate."
            )
        finally:
            queries.get_user_by_id = orig  # type: ignore[assignment]

    asyncio.run(_run())


def test_resolve_user_level_source_pin_checks_both_flags() -> None:
    """The boolean expression on line 387ish must check BOTH
    suspended_at and deleted_at. Source-pin so a refactor that
    splits them can't accidentally drop the deleted_at half."""
    pytest.importorskip("fastapi")
    from bp_router.llm.service import LlmService

    src = inspect.getsource(LlmService.resolve_user_level)
    found = False
    for line in src.splitlines():
        if (
            "user.suspended_at is not None" in line
            and "user.deleted_at is not None" in line
        ):
            found = True
            break
    assert found, (
        "resolve_user_level must check suspended_at AND deleted_at "
        "in the same expression"
    )


# ===========================================================================
# 4. test_task — refuses deleted acting_user
# ===========================================================================


def test_test_task_source_pin_refuses_deleted_acting_user() -> None:
    """admin.test_task: the acting-user lookup must refuse
    `deleted_at IS NOT NULL` users. Source-pin since the actual
    flow needs a full router stack to exercise end-to-end."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.test_task)
    assert "acting_user.deleted_at is not None" in src, (
        "test_task must refuse deleted acting users — close the "
        "admin_test_allow_act_as bypass"
    )


# ===========================================================================
# 5. delete_user invalidates the level cache
# ===========================================================================


def test_delete_user_invalidates_level_cache() -> None:
    """`admin.delete_user` calls
    `state.llm_service.invalidate_user_level(target_user_id)` after
    successful soft-delete. Without this, a deleted user's cached
    level persists for up to 10 min (USER_LEVEL_TTL_S) and continues
    to satisfy tier gates for any code path that reads the cache.

    Source pin (source-of-truth: the admin endpoint string)."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.delete_user)
    assert "invalidate_user_level(target_user_id)" in src, (
        "admin.delete_user must invalidate the LLM service's "
        "user-level cache after successful soft-delete; "
        "otherwise the cached tier persists for up to 10 minutes."
    )
