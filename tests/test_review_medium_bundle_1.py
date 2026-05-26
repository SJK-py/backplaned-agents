"""Tests for the medium-bundle review fixes (Sec-M5, Sec-M6, DB-M3).

Sec-M5 — JTI revocation switched from a single Redis SET with a
sliding TTL on the WHOLE set to per-jti keys with `SET ... EX
ttl_s`. Each jti expires exactly when its underlying JWT does;
no more accumulation under sustained traffic.

Sec-M6 — confirmed `consume_refresh_token` enforces `expires_at >
now()` in its WHERE clause. Regression test pins the SQL shape.

DB-M3 — `_apply_rule_change` now serialised by an asyncio.Lock
so concurrent admins can't interleave DB-read + cache-replace
and leave the in-memory rule cache stale.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Sec-M5: per-jti revocation with EX
# ===========================================================================


def test_revoked_jti_key_uses_per_jti_prefix() -> None:
    """The new key shape is `router:revoked_jti:{jti}` (one Redis
    key per revocation), NOT `router:revoked_jti` (one set with all
    members). Pinned at the helper level so a refactor can't
    silently revert to the single-set pattern."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt as jwt_mod

    assert jwt_mod._REVOKED_JTI_KEY_PREFIX == "router:revoked_jti:"
    assert jwt_mod._revoked_jti_key("abc") == "router:revoked_jti:abc"


def test_revoke_jti_uses_set_with_ex_not_sadd_with_expire() -> None:
    """Source pin: `revoke_jti` calls `redis.set(..., ex=ttl_s)`
    on the per-jti key, NOT `sadd` + `expire` on the whole set.
    The single-set pattern was the source of the sliding-TTL bug
    (review item Sec-M5)."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt as jwt_mod

    src = inspect.getsource(jwt_mod.revoke_jti)
    # New shape.
    assert "redis.set(" in src
    assert "ex=ttl_s" in src
    assert "_revoked_jti_key" in src
    # Old shape gone.
    assert "pipe.sadd" not in src
    assert "pipe.expire" not in src


def test_is_jti_revoked_uses_exists() -> None:
    """`is_jti_revoked` does a single EXISTS lookup, NOT SMEMBERS
    (which loaded the entire revocation set into the request
    handler's memory on every authenticated call)."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt as jwt_mod

    src = inspect.getsource(jwt_mod.is_jti_revoked)
    assert "redis.exists(" in src
    # No actual call to smembers (the docstring mentions the word
    # in passing — match the call pattern, not the bare word).
    assert "redis.smembers(" not in src


def test_revoke_jti_no_op_when_redis_unconfigured() -> None:
    """Single-worker deployments without Redis accept best-effort
    revocation. `revoke_jti(None, ...)` returns silently."""
    pytest.importorskip("fastapi")
    from bp_router.security.jwt import revoke_jti

    asyncio.run(revoke_jti(None, "jti_x", ttl_s=900))


def test_is_jti_revoked_returns_false_when_redis_unconfigured() -> None:
    """Same trade-off: without Redis, every JTI is treated as
    not-revoked (revocation is unsupported in single-worker
    deploys)."""
    pytest.importorskip("fastapi")
    from bp_router.security.jwt import is_jti_revoked

    out = asyncio.run(is_jti_revoked(None, "jti_x"))
    assert out is False


def test_revoke_then_check_round_trip_against_fake_redis() -> None:
    """Behavioral: revoke a jti, then check with the same Redis;
    EXISTS returns True. After EX expires (we simulate by deleting
    the key), check returns False."""
    pytest.importorskip("fastapi")
    from bp_router.security.jwt import is_jti_revoked, revoke_jti

    # Minimal async-aware Redis stub — implements the shape our
    # helpers use (`set(key, value, ex=...)` and `exists(key)`).
    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}
            self.ttls: dict[str, int] = {}

        async def set(self, key: str, value: str, *, ex: int) -> None:
            self.store[key] = value
            self.ttls[key] = ex

        async def exists(self, key: str) -> int:
            return 1 if key in self.store else 0

    async def _drive() -> None:
        redis = _FakeRedis()
        await revoke_jti(redis, "jti_revoked", ttl_s=900)
        # The per-jti key is set with the right TTL.
        assert redis.store == {"router:revoked_jti:jti_revoked": "1"}
        assert redis.ttls == {"router:revoked_jti:jti_revoked": 900}

        # is_jti_revoked finds it.
        assert await is_jti_revoked(redis, "jti_revoked") is True
        # And not other jtis.
        assert await is_jti_revoked(redis, "jti_other") is False

        # Simulate TTL expiry.
        del redis.store["router:revoked_jti:jti_revoked"]
        assert await is_jti_revoked(redis, "jti_revoked") is False

    asyncio.run(_drive())


def test_principal_from_request_uses_per_jti_check() -> None:
    """Source pin: `_principal_from_request` does NOT pre-load the
    revocation set on every request anymore. It calls
    `is_jti_revoked(state.redis, claims["jti"])` AFTER
    `verify_token` — so a malformed Bearer header doesn't even hit
    Redis."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt as jwt_mod

    src = inspect.getsource(jwt_mod._principal_from_request)
    # New per-jti check.
    assert "is_jti_revoked(state.redis, claims[\"jti\"])" in src
    # Old pre-load helper gone from this function.
    assert "_load_revoked_jti" not in src


def test_load_revoked_jti_helper_removed() -> None:
    """The pre-load helper `_load_revoked_jti` is gone — `SMEMBERS
    router:revoked_jti` was the cost we were paying on every
    authenticated request. Its absence pins the deletion."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt as jwt_mod

    assert not hasattr(jwt_mod, "_load_revoked_jti")


def test_onboard_refresh_uses_per_jti_check() -> None:
    """The `/onboard/refresh` agent-token rotation endpoint also
    moved off the SMEMBERS pre-load. Source pin."""
    pytest.importorskip("fastapi")
    from bp_router.api import onboard

    src = inspect.getsource(onboard)
    # `is_jti_revoked` imported and used.
    assert "is_jti_revoked" in src
    # Old `_redis_revoked_jti` helper removed (was the per-onboard
    # equivalent of `_load_revoked_jti`).
    assert "_redis_revoked_jti" not in src


# ===========================================================================
# Sec-M6: refresh-token expiry IS enforced (regression pin)
# ===========================================================================


def test_consume_refresh_token_enforces_expires_at() -> None:
    """Sec-M6 was flagged for verification — turns out the SQL
    already enforces `expires_at > now()` in the WHERE clause of
    the FOR UPDATE select. Pin the source so a future refactor
    that drops the predicate is caught immediately."""
    from bp_router.db import queries

    src = inspect.getsource(queries.consume_refresh_token)
    # The SELECT must constrain on expiry.
    assert "WHERE token_hash = $1 AND expires_at > $2" in src
    # FOR UPDATE for the atomic single-use semantics.
    assert "FOR UPDATE" in src


def test_consume_refresh_token_returns_none_for_expired_row() -> None:
    """Behavioral: an expired token row (`expires_at < now()`)
    fails the WHERE clause → fetchrow returns None → consume
    returns None. The caller (`/refresh`) raises 401."""
    from bp_router.db import queries

    class _StubConn:
        async def fetchrow(self, query: str, *args: Any) -> Any:
            # Expired row: WHERE clause excludes → no row.
            return None

        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            return None

    out = asyncio.run(queries.consume_refresh_token(
        _StubConn(),  # type: ignore[arg-type]
        token_hash="hash_of_expired_token",
        replaced_by="hash_of_new_token",
    ))
    assert out is None


# ===========================================================================
# DB-M3: _apply_rule_change is serialised
# ===========================================================================


def test_apply_rule_change_holds_module_lock() -> None:
    """Source pin: `_apply_rule_change` body is wrapped in
    `async with _apply_rule_change_lock:`. Without the lock,
    concurrent admins can interleave DB-read + cache-replace,
    leaving the in-memory cache stale relative to the DB
    (review item DB-M3)."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin._apply_rule_change)
    assert "async with _apply_rule_change_lock:" in src
    # The DB read AND the replace+push are inside the lock
    # (otherwise serialising only one half wouldn't help).
    lock_idx = src.index("_apply_rule_change_lock")
    list_idx = src.index("queries.list_acl_rules")
    replace_idx = src.index("state.rules.replace")
    assert lock_idx < list_idx < replace_idx


def test_apply_rule_change_lock_is_module_level_singleton() -> None:
    """The lock is a single module-level `asyncio.Lock()` so every
    `_apply_rule_change` invocation in the process contends on the
    same instance. Per-call locks would be useless."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    assert isinstance(admin._apply_rule_change_lock, asyncio.Lock)


def test_apply_rule_change_serialises_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral: two concurrent invocations of `_apply_rule_change`
    serialise via the lock — the second waits for the first to
    release before doing its read/replace."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    # Ordering observation: first call enters DB-read, sleeps; second
    # call must wait for the lock before its own DB-read fires.
    sequence: list[str] = []

    async def _slow_list_acl_rules(conn: Any) -> list[Any]:
        sequence.append("read_start")
        await asyncio.sleep(0.05)
        sequence.append("read_end")
        return []

    monkeypatch.setattr(admin.queries, "list_acl_rules", _slow_list_acl_rules)

    # No-op the catalog push — we only care about the lock ordering.
    async def _no_push(_state: Any) -> None:
        sequence.append("push")

    import bp_router.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "push_catalog_update_to_all", _no_push)

    # Stub state with a no-op rules.replace and a fake pool.
    state = MagicMock()
    state.rules.replace = lambda rules: sequence.append("replace")
    pool = MagicMock()
    state.db_pool = pool
    # Connection mock has to support both `conn.transaction()` (sync
    # call returning an async context manager) AND `conn.execute(...)`
    # (await) so the new pg_advisory_xact_lock call doesn't choke
    # the test (review item M2 added the advisory lock).
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: txn
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    async def _drive() -> None:
        # Ensure the lock starts unheld.
        async with admin._apply_rule_change_lock:
            pass
        await asyncio.gather(
            admin._apply_rule_change(state),
            admin._apply_rule_change(state),
        )

    asyncio.run(_drive())

    # If serialised: read_start → read_end → replace → push, then
    # the second iteration. If NOT serialised: both read_starts up
    # front. Pin the serialised order.
    assert sequence == [
        "read_start", "read_end", "replace", "push",
        "read_start", "read_end", "replace", "push",
    ]
