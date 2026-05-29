"""R10 HIGH: Redis-degradation hardening.

HIGH-4 — `TokenBucket._mem` was an UNBOUNDED dict. A Redis outage
during a fleet reconnect (per-IP handshake keys) or a
credential-stuffing sweep (per-email keys) created unbounded
distinct keys → RSS growth until OOM, on the day Redis is already
degraded. Now a `BoundedLRUDict` (evicting a bucket merely resets
it to full — fail-open is the chosen Redis-down posture).

HIGH-5 — `redis_health` was set to 0 ONLY by the rate-limit
fallback and back to 1 ONLY by `/readyz` (polled every few
seconds). During flapping Redis the gauge oscillated 0→1→0, so
`router_redis_health == 0` never fired reliably while revocation
+ quota silently degraded per-fallback. The gauge is now authored
by the subsystem: rate_limit sets 1 on a real successful Redis
op, 0 on fallback; `/readyz` no longer touches it.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

# ===========================================================================
# HIGH-4 — bounded fallback table
# ===========================================================================


def test_mem_fallback_is_bounded_lru() -> None:
    from bp_router.lru_cache import BoundedLRUDict
    from bp_router.security.rate_limit import TokenBucket

    tb = TokenBucket(redis=None, prefix="quota:test")
    assert isinstance(tb._mem, BoundedLRUDict)


def test_mem_fallback_constant_sane() -> None:
    from bp_router.security import rate_limit as rl

    assert isinstance(rl._MEM_FALLBACK_MAX, int)
    # Generous (degraded-mode fallback) but bounded.
    assert 1_000 <= rl._MEM_FALLBACK_MAX <= 1_000_000


def test_mem_fallback_evicts_under_unbounded_distinct_keys() -> None:
    """The OOM scenario: Redis is None (forced fallback) and a flood
    of DISTINCT keys arrives (fleet reconnect / credential sweep).
    The table must stay bounded, not grow unbounded."""
    from bp_router.security.rate_limit import TokenBucket

    # Shrink the cap so the test is fast but exercises eviction.
    tb = TokenBucket(redis=None, prefix="quota:test")
    cap = 64
    from bp_router.lru_cache import BoundedLRUDict

    tb._mem = BoundedLRUDict(maxsize=cap)

    async def _run() -> None:
        for i in range(cap * 10):  # 10x the cap of distinct keys
            await tb.try_consume(
                f"ip:{i}", rate_per_s=1.0, burst=5, cost=1
            )
        assert len(tb._mem) <= cap, (
            f"_mem grew to {len(tb._mem)} — unbounded fallback "
            f"(OOM vector) regressed"
        )

    asyncio.run(_run())


def test_mem_fallback_still_rate_limits_correctly() -> None:
    """Bounding must not break the fallback's actual rate-limiting:
    a burst is allowed up to `burst`, then denied."""
    from bp_router.security.rate_limit import TokenBucket

    tb = TokenBucket(redis=None, prefix="quota:test")

    async def _run() -> None:
        allowed = 0
        for _ in range(10):
            d = await tb.try_consume(
                "same-key", rate_per_s=0.0001, burst=3, cost=1
            )
            if d.allowed:
                allowed += 1
        # ~burst requests allowed before depletion (rate ~0 so no
        # meaningful refill across the loop).
        assert allowed == 3

    asyncio.run(_run())


# ===========================================================================
# HIGH-5 — redis_health authored by the subsystem, not /readyz
# ===========================================================================


def test_readyz_does_not_set_redis_health() -> None:
    """`/readyz` is polled every few seconds; it must NOT author the
    degradation signal (that made it oscillate during flapping)."""
    pytest.importorskip("fastapi")
    from bp_router.api import health

    src = inspect.getsource(health.readiness)
    assert "redis_health" not in src or "set(" not in src
    # Stronger: no `.set(` call at all in the readiness probe.
    assert ".set(" not in src


def test_rate_limit_sets_health_1_on_success_and_0_on_fallback() -> None:
    """The gauge is authored where it belongs: success → 1,
    fallback → 0, both in the rate-limit Redis path."""
    from bp_router.security.rate_limit import TokenBucket

    src = inspect.getsource(TokenBucket.try_consume)
    assert "redis_health.set(1)" in src  # on successful redis op
    assert "redis_health.set(0)" in src  # on fallback
    # set(1) must be on the success path (before the fallback
    # except / before returning the redis Decision), set(0) in the
    # except.
    s1 = src.index("redis_health.set(1)")
    s0 = src.index("redis_health.set(0)")
    except_idx = src.index("except Exception")
    assert s1 < except_idx < s0, (
        "set(1) must be on the success path, set(0) in the "
        "fallback except"
    )


def test_redis_health_behaviourally_sticks_at_0_until_real_recovery() -> None:
    """End-to-end of the fix: a fallback flips the gauge to 0; it
    stays 0 across `/readyz`-equivalent activity and only returns
    to 1 when a real Redis op succeeds."""
    pytest.importorskip("prometheus_client")
    from bp_router.observability import metrics
    from bp_router.security.rate_limit import TokenBucket

    def _h() -> float:
        try:
            return metrics.redis_health._value.get()  # type: ignore[attr-defined]
        except Exception:
            return -1.0

    class _FlakyRedis:
        def __init__(self) -> None:
            self.ok = False

        async def eval(self, *a, **k):  # type: ignore[no-untyped-def]
            if not self.ok:
                raise RuntimeError("redis down")
            return [1, 0.0, 4.0]

    redis = _FlakyRedis()
    tb = TokenBucket(redis=redis, prefix="quota:test")

    async def _run() -> None:
        # Redis down → fallback → health 0.
        await tb.try_consume("k", rate_per_s=1.0, burst=5)
        assert _h() == 0.0
        # More fallbacks keep it at 0 (no probe resets it).
        await tb.try_consume("k", rate_per_s=1.0, burst=5)
        assert _h() == 0.0
        # Genuine recovery: a real successful Redis op flips it to 1.
        redis.ok = True
        await tb.try_consume("k", rate_per_s=1.0, burst=5)
        assert _h() == 1.0

    asyncio.run(_run())
