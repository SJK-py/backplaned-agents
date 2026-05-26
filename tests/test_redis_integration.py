"""Real Redis-protocol integration tests via `fakeredis`.

The Redis-completeness audit found that every existing Redis-using
test was wired to a `MagicMock` or a hand-rolled `_FakeRedis` stub
with a partial async surface — meaning contract drift between our
code and a real Redis server (e.g. a typo in a SET / EXISTS / EVAL
flag) wouldn't surface until production. These tests use
`fakeredis.aioredis.FakeRedis`, which speaks the actual Redis
protocol (full EVAL / Lua / EXPIRE / TTL semantics) in-process,
so the contract is exercised end-to-end without needing a live
container in CI.

Coverage:
  - JTI revocation (`bp_router.security.jwt.revoke_jti` /
    `is_jti_revoked`) — write/read round-trip + TTL eviction.
  - Token bucket (`bp_router.security.rate_limit.TokenBucket`) —
    refill, exhaustion, retry-after math, atomic two-worker race.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fakeredis fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis():
    """Async fakeredis client. Decode-responses=True matches
    `bp_router.db.connection.open_redis`'s real client config so
    the Lua script return values come back as `str`, not `bytes`,
    in both paths."""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# ===========================================================================
# JTI revocation round-trip
# ===========================================================================


async def test_revoke_then_is_revoked_returns_true(redis) -> None:
    """`revoke_jti` followed by `is_jti_revoked` for the same jti
    must return True. The previous mock-only tests only verified
    the call was made — not that the read sees the write."""
    from bp_router.security.jwt import is_jti_revoked, revoke_jti

    await revoke_jti(redis, "jti_alice", ttl_s=300)
    assert await is_jti_revoked(redis, "jti_alice") is True


async def test_unrevoked_jti_returns_false(redis) -> None:
    from bp_router.security.jwt import is_jti_revoked

    assert await is_jti_revoked(redis, "jti_never_seen") is False


async def test_revoke_isolates_per_jti(redis) -> None:
    """Revoking one jti must NOT mark a different jti as revoked.
    Catches a regression where someone swaps the per-key SET for
    a SADD against a shared set without preserving the per-jti
    EXISTS contract."""
    from bp_router.security.jwt import is_jti_revoked, revoke_jti

    await revoke_jti(redis, "jti_alice", ttl_s=300)
    assert await is_jti_revoked(redis, "jti_bob") is False
    assert await is_jti_revoked(redis, "jti_alice") is True


async def test_revoked_jti_has_ttl_set(redis) -> None:
    """The revocation key MUST carry the TTL the caller passed —
    not -1 (no expiry, would pin keyspace forever) and not 0
    (would never accept the write). Validates the underlying
    `SET ... EX ttl_s` flag, which a refactor to `SETEX` or
    `PERSIST` would silently break."""
    from bp_router.security.jwt import _revoked_jti_key, revoke_jti

    await revoke_jti(redis, "jti_with_ttl", ttl_s=600)
    ttl = await redis.ttl(_revoked_jti_key("jti_with_ttl"))
    # Allow a small slack window — the TTL is set the moment the
    # SET runs and a later TTL check sees a slightly-decremented
    # value.
    assert 590 <= ttl <= 600


async def test_revoke_jti_no_op_without_redis() -> None:
    """When `redis is None` (single-worker dev), both helpers MUST
    silently no-op — `revoke_jti` returns None, `is_jti_revoked`
    returns False. Keeps single-worker deployments working without
    Redis; the prod-mode settings validator blocks the
    misconfigured multi-worker case at startup."""
    from bp_router.security.jwt import is_jti_revoked, revoke_jti

    await revoke_jti(None, "jti_x", ttl_s=300)  # must not raise
    assert await is_jti_revoked(None, "jti_x") is False


# ===========================================================================
# TokenBucket — Redis-backed
# ===========================================================================


async def test_bucket_consumes_until_empty(redis) -> None:
    """Burst capacity drains exactly `burst` requests then the
    next `try_consume` is denied with a positive `retry_after_s`."""
    from bp_router.security.rate_limit import TokenBucket

    bucket = TokenBucket(redis=redis, prefix="t1")
    # rate=10/s, burst=3 → first 3 calls allowed, 4th denied.
    for _ in range(3):
        d = await bucket.try_consume("u1:tier1", rate_per_s=10.0, burst=3)
        assert d.allowed
    d = await bucket.try_consume("u1:tier1", rate_per_s=10.0, burst=3)
    assert not d.allowed
    assert d.retry_after_s > 0
    # `retry_after_s` should be approximately 0.1s (1 token at
    # 10/s rate). Allow ±50% slack.
    assert 0.05 < d.retry_after_s < 0.2


async def test_bucket_refills_after_sleep(redis) -> None:
    """After waiting fill_time = 1/rate, exactly one token should
    refill. Drain → wait → consume one is allowed."""
    from bp_router.security.rate_limit import TokenBucket

    bucket = TokenBucket(redis=redis, prefix="t2")
    for _ in range(2):
        await bucket.try_consume("u2:tier1", rate_per_s=20.0, burst=2)
    # Both spent; next call denied.
    d = await bucket.try_consume("u2:tier1", rate_per_s=20.0, burst=2)
    assert not d.allowed
    # Sleep enough for one token to refill (1 / 20 = 0.05 s; pad).
    await asyncio.sleep(0.12)
    d = await bucket.try_consume("u2:tier1", rate_per_s=20.0, burst=2)
    assert d.allowed


async def test_bucket_per_key_isolation(redis) -> None:
    """Two different bucket keys (different users, different
    levels) MUST NOT share state."""
    from bp_router.security.rate_limit import TokenBucket

    bucket = TokenBucket(redis=redis, prefix="t3")
    # Drain user A.
    for _ in range(3):
        d = await bucket.try_consume("ua:tier1", rate_per_s=1.0, burst=3)
        assert d.allowed
    d = await bucket.try_consume("ua:tier1", rate_per_s=1.0, burst=3)
    assert not d.allowed
    # User B at the same tier should still have full burst.
    d = await bucket.try_consume("ub:tier1", rate_per_s=1.0, burst=3)
    assert d.allowed
    assert d.tokens_remaining == pytest.approx(2.0, abs=0.01)


async def test_bucket_atomic_under_concurrent_consume(redis) -> None:
    """The whole point of the Lua-scripted version: two concurrent
    `try_consume`s against a bucket with `burst=1` must ONLY admit
    one of them. Without the Lua atomicity, both could read tokens=1
    and both deduct."""
    from bp_router.security.rate_limit import TokenBucket

    bucket = TokenBucket(redis=redis, prefix="t4")
    results = await asyncio.gather(
        bucket.try_consume("uc:tier1", rate_per_s=0.001, burst=1),
        bucket.try_consume("uc:tier1", rate_per_s=0.001, burst=1),
    )
    allowed = [r for r in results if r.allowed]
    assert len(allowed) == 1, (
        f"two concurrent try_consume calls both saw a free token: {results!r}"
    )


async def test_bucket_in_memory_fallback_works_without_redis() -> None:
    """When `redis=None`, the bucket falls back to per-process
    state. Same shape — drain to denial, refill, consume again."""
    from bp_router.security.rate_limit import TokenBucket

    bucket = TokenBucket(redis=None, prefix="t5")
    for _ in range(3):
        d = await bucket.try_consume("um:tier1", rate_per_s=10.0, burst=3)
        assert d.allowed
    d = await bucket.try_consume("um:tier1", rate_per_s=10.0, burst=3)
    assert not d.allowed
    await asyncio.sleep(0.12)
    d = await bucket.try_consume("um:tier1", rate_per_s=10.0, burst=3)
    assert d.allowed


async def test_bucket_falls_back_when_redis_eval_raises(redis, monkeypatch) -> None:
    """If Redis is up but the Lua eval blows up (driver bug, server
    crash mid-call), the bucket must NOT fail-closed across the
    entire admit_task path. Falls back to the per-process bucket;
    operator sees a warning log line. Keeps the router alive at
    the cost of cross-worker correctness during the outage."""
    from bp_router.security.rate_limit import TokenBucket

    async def boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated redis flake")

    monkeypatch.setattr(redis, "eval", boom)
    bucket = TokenBucket(redis=redis, prefix="t6")
    # Should still admit via the in-memory path.
    d = await bucket.try_consume("ux:tier1", rate_per_s=5.0, burst=2)
    assert d.allowed


async def test_bucket_zero_rate_treated_as_uncapped() -> None:
    """Defensive shape: a zero/negative rate is treated as
    'no cap' rather than a divide-by-zero or instant denial.
    The admit_task call site short-circuits before reaching here
    when the level has `rate=None`, but the helper itself still
    accepts the shape."""
    from bp_router.security.rate_limit import TokenBucket

    bucket = TokenBucket(redis=None, prefix="t7")
    d = await bucket.try_consume("uz:tier1", rate_per_s=0.0, burst=0)
    assert d.allowed
    assert d.retry_after_s == 0.0
