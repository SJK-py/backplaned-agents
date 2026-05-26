"""bp_router.security.rate_limit — Token-bucket rate limiter.

Atomic, multi-worker-safe when Redis is configured; falls back to
per-process state when not. The Redis path runs the
read-update-write through a Lua script so two workers admitting
concurrent tasks against the same user can't both see "1 token left"
and both spend it.

Backed by the design in `docs/design/quota-enforcement.md` §3-§5.
The shared infrastructure also covers the deferred per-agent
inbound-frame rate cap (§11) — same bucket, different key prefix.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from bp_router.lru_cache import BoundedLRUDict

logger = logging.getLogger(__name__)

# Hard cap on the per-process fallback table used when Redis is
# unreachable. Pre-R10 this was an UNBOUNDED dict: a Redis outage
# during a fleet reconnect (per-IP handshake keys) or a
# credential-stuffing sweep (per-email keys) created unbounded
# distinct keys → RSS growth until OOM, on the exact day Redis is
# already degraded (the same hazard R8 capped for
# `caller_agent_cache`). Bounded with LRU eviction: evicting a
# bucket merely resets it to full, which is acceptable — fail-open
# is the already-chosen posture for the Redis-down path. 50k
# entries × ~64 B ≈ 3 MiB worst case.
_MEM_FALLBACK_MAX = 50_000


# ---------------------------------------------------------------------------
# Lua script — atomic check-and-deduct
# ---------------------------------------------------------------------------
#
# Returns: {allowed (0|1), retry_after_s_str, tokens_remaining_str}.
# Strings on the back of two values that can be sub-second floats —
# Redis Lua's number type is double, but the integer-coercion path
# in the redis-py client truncates fractional results unless we
# round-trip through string. We parse them back on the Python side.
#
# Storage shape: a hash with two fields (`tokens`, `ts`). We use
# `HMGET` / `HSET` on a single key + `EXPIRE` to bound the keyspace
# (TTL = bucket-fill-time × 2, see `_ttl_for_bucket`).
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
    tokens = burst
    ts = now
end

local elapsed = now - ts
if elapsed > 0 then
    tokens = math.min(burst, tokens + elapsed * rate)
end

local allowed = 0
local retry_after = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    retry_after = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', key, ttl)

return {allowed, tostring(retry_after), tostring(tokens)}
"""


def _ttl_for_bucket(rate_per_s: float, burst: int) -> int:
    """TTL for a bucket key. Long enough that the bucket survives a
    full refill (fill-time = burst / rate) plus a safety multiple,
    short enough that idle keys age out so the keyspace stays bounded.

    `2 ×` the fill time, with a 60 s floor so very high-rate buckets
    don't expire mid-burst and a 1 h cap so very low-rate buckets
    don't pin keyspace forever (a `tier3` bucket at 1 token/s with
    burst=2 has fill_time=2s; cap dominates if rate were 0.001/s).
    """
    fill_time = burst / rate_per_s if rate_per_s > 0 else 0
    return max(60, min(3600, int(fill_time * 2) + 1))


@dataclass
class Decision:
    allowed: bool
    retry_after_s: float
    tokens_remaining: float


class TokenBucket:
    """Multi-worker-safe token bucket.

    Construction takes a `redis` handle (may be None) and the
    in-memory fallback table is created lazily. `try_consume` is the
    only public entry point.

    Per-key state lives at `<prefix>:<key>` in Redis. The prefix
    namespacing keeps the admit-quota buckets distinct from any
    future bucket (per-agent frame rate, etc.) without coupling them
    to a shared key encoding.
    """

    def __init__(
        self,
        *,
        redis: Any | None,
        prefix: str = "quota:bucket",
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        # Per-process fallback. Bounded LRU (see _MEM_FALLBACK_MAX) +
        # asyncio.Lock — single event loop per worker, so the lock
        # is enough to serialise the read-update-write.
        self._mem: BoundedLRUDict = BoundedLRUDict(
            maxsize=_MEM_FALLBACK_MAX
        )
        self._mem_lock = asyncio.Lock()

    async def try_consume(
        self,
        key: str,
        *,
        rate_per_s: float,
        burst: int,
        cost: int = 1,
    ) -> Decision:
        """Attempt to consume `cost` tokens from bucket `key`.

        Returns `Decision(allowed, retry_after_s, tokens_remaining)`.
        When denied, `retry_after_s` is the time until enough tokens
        accumulate to satisfy this request; the caller turns that
        into a `Retry-After` header.

        `rate_per_s` and `burst` are passed per-call (rather than
        baked into construction) because the same bucket-key can be
        configured per tier — admit-quota tiers, per-agent frame
        caps — and the bucket helper itself is policy-agnostic.
        """
        if rate_per_s <= 0 or burst <= 0:
            # Treat zero/negative as "no cap configured for this key".
            # The caller is responsible for short-circuiting before
            # calling `try_consume` when the level has no cap, but
            # be defensive.
            return Decision(allowed=True, retry_after_s=0.0, tokens_remaining=float(burst))

        full_key = f"{self._prefix}:{key}"
        now = time.time()
        ttl = _ttl_for_bucket(rate_per_s, burst)

        if self._redis is not None:
            try:
                result = await self._redis.eval(
                    _TOKEN_BUCKET_LUA,
                    1,
                    full_key,
                    str(now),
                    str(rate_per_s),
                    str(burst),
                    str(cost),
                    str(ttl),
                )
                # redis-py returns the multi-result as a list. Items
                # come back as bytes when `decode_responses=False`,
                # str when True; coerce explicitly.
                allowed = int(_to_str(result[0])) == 1
                retry_after = float(_to_str(result[1]))
                tokens_left = float(_to_str(result[2]))
                # Redis op succeeded — author the recovery signal
                # HERE, in the subsystem that owns it, not in
                # `/readyz` (R10). Pre-R10 only the fallback set it
                # to 0 and only the frequently-polled readiness
                # probe set it back to 1, so during flapping Redis
                # the gauge oscillated and the
                # `router_redis_health == 0` alert never fired
                # reliably while revocation/quota silently degraded.
                try:
                    from bp_router.observability.metrics import (  # noqa: PLC0415
                        redis_health,
                    )
                    redis_health.set(1)
                except Exception:  # noqa: BLE001
                    pass
                return Decision(
                    allowed=allowed,
                    retry_after_s=retry_after,
                    tokens_remaining=tokens_left,
                )
            except Exception:  # noqa: BLE001
                # Redis flake. Don't fail-closed — silently fall
                # through to the in-memory path so a Redis blip
                # doesn't cascade into a global admit-task outage.
                # Logged for the operator. Single-worker correctness
                # is preserved; cross-worker correctness was already
                # broken the moment Redis was unreachable.
                logger.warning(
                    "rate_limit_redis_eval_failed_falling_back",
                    extra={"event": "rate_limit_redis_eval_failed"},
                    exc_info=True,
                )
                try:
                    from bp_router.observability.metrics import (  # noqa: PLC0415
                        redis_fallback_total,
                        redis_health,
                    )
                    redis_fallback_total.labels(subsystem="rate_limit").inc()
                    redis_health.set(0)
                except Exception:  # noqa: BLE001
                    pass

        # Per-process fallback. Single asyncio loop, single lock,
        # so the read-update-write is atomic within the worker.
        async with self._mem_lock:
            tokens, ts = self._mem.get(full_key, (float(burst), now))
            elapsed = now - ts
            if elapsed > 0:
                tokens = min(float(burst), tokens + elapsed * rate_per_s)
            if tokens >= cost:
                tokens -= cost
                self._mem[full_key] = (tokens, now)
                return Decision(
                    allowed=True, retry_after_s=0.0, tokens_remaining=tokens
                )
            retry_after = (cost - tokens) / rate_per_s
            self._mem[full_key] = (tokens, now)
            return Decision(
                allowed=False,
                retry_after_s=retry_after,
                tokens_remaining=tokens,
            )


def _to_str(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v)


__all__ = ["Decision", "TokenBucket"]
