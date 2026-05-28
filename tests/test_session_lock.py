"""bp_agents.session_lock — the per-session lock that serializes channel
turns ([sessions.md §4]), in-process by default and cross-process when a
Redis client is supplied (the prerequisite for a second channel instance)."""

from __future__ import annotations

import asyncio

import pytest

from bp_agents.session_lock import SessionLockManager


async def _worker(mgr: SessionLockManager, sid: str, tag: str, hold: float, order: list) -> None:
    async with mgr(sid):
        order.append(f"enter:{tag}")
        await asyncio.sleep(hold)
        order.append(f"exit:{tag}")


def test_local_only_serializes_same_session() -> None:
    async def _drive() -> None:
        mgr = SessionLockManager(None)
        order: list[str] = []
        await asyncio.gather(
            _worker(mgr, "s", "a", 0.05, order),
            _worker(mgr, "s", "b", 0.0, order),
        )
        # 'a' acquired first; 'b' must not enter until 'a' exits — no interleave.
        assert order == ["enter:a", "exit:a", "enter:b", "exit:b"]

    asyncio.run(_drive())


def test_local_only_distinct_sessions_run_concurrently() -> None:
    async def _drive() -> None:
        mgr = SessionLockManager(None)
        order: list[str] = []
        await asyncio.gather(
            _worker(mgr, "s1", "a", 0.05, order),
            _worker(mgr, "s2", "b", 0.0, order),
        )
        # Different sessions don't block: 'b' enters before 'a' exits.
        assert order.index("enter:b") < order.index("exit:a")

    asyncio.run(_drive())


def test_cross_instance_mutual_exclusion_via_redis() -> None:
    """Two SEPARATE managers (≈ two channel processes) sharing one Redis
    must not hold the same session concurrently."""
    async def _drive() -> None:
        fakeredis = pytest.importorskip("fakeredis")
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        a = SessionLockManager(r, ttl_s=5.0, poll_s=0.01)
        b = SessionLockManager(r, ttl_s=5.0, poll_s=0.01)
        order: list[str] = []
        try:
            await asyncio.gather(
                _worker(a, "s", "a", 0.05, order),
                _worker(b, "s", "b", 0.0, order),
            )
        finally:
            await r.aclose()
        # Whoever won the NX race runs fully before the other enters.
        assert order[0].startswith("enter:")
        assert order[1].startswith("exit:")
        assert order[1].split(":")[1] == order[0].split(":")[1]

    asyncio.run(_drive())


def test_redis_error_fails_open_to_local() -> None:
    async def _drive() -> None:
        class _BoomRedis:
            async def set(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
                raise RuntimeError("redis down")

            async def eval(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
                raise RuntimeError("redis down")

        mgr = SessionLockManager(_BoomRedis(), ttl_s=5.0)
        # Must not raise or hang — degrades to the local lock.
        async with mgr("s") as guard:
            assert guard._token is None  # Redis layer skipped

    asyncio.run(_drive())


def test_redis_lock_released_on_exit() -> None:
    async def _drive() -> None:
        fakeredis = pytest.importorskip("fakeredis")
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        mgr = SessionLockManager(r, ttl_s=5.0)
        async with mgr("s"):
            assert await r.exists("bp:session-lock:s") == 1
        assert await r.exists("bp:session-lock:s") == 0
        await r.aclose()

    asyncio.run(_drive())


def test_redis_renewal_keeps_long_hold() -> None:
    """A hold longer than the TTL stays locked because the watchdog renews
    it; the key is gone once released."""
    async def _drive() -> None:
        fakeredis = pytest.importorskip("fakeredis")
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        mgr = SessionLockManager(r, ttl_s=0.2, renew_s=0.05)
        async with mgr("s"):
            await asyncio.sleep(0.5)  # > ttl; only renewal keeps it alive
            assert await r.exists("bp:session-lock:s") == 1
        assert await r.exists("bp:session-lock:s") == 0
        await r.aclose()

    asyncio.run(_drive())
