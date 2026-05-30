"""Second-pass concurrency fixes for the session lock.

C2 — renewal/release were a non-atomic GET-then-PEXPIRE/DELETE: between the
     two round-trips the key could expire and be re-taken by another instance,
     and this holder would then extend / delete a FOREIGN lock. Now both go
     through an atomic WATCH/MULTI compare-and-act (`_cas`).
C3 — `_local` held one asyncio.Lock per distinct session_id forever (a slow
     leak over a long-lived channel process). It's now refcounted and evicted
     when no guard references it — without ever handing overlapping guards
     different lock objects for the same session.
"""

from __future__ import annotations

import asyncio

import pytest

from bp_agents.session_lock import SessionLockManager

# --- C3: refcounted eviction of the local-lock dict ------------------------


def test_release_local_refcount_evicts_at_zero() -> None:
    mgr = SessionLockManager(None)
    lock1 = mgr._acquire_local("s")
    lock2 = mgr._acquire_local("s")
    assert lock1 is lock2  # same object for overlapping references
    assert mgr._local_refs["s"] == 2
    mgr._release_local("s")
    assert mgr._local_refs["s"] == 1 and "s" in mgr._local  # one ref remains
    mgr._release_local("s")
    assert mgr._local == {} and mgr._local_refs == {}  # evicted at zero


def test_local_dict_empty_after_guard_exits() -> None:
    async def _drive() -> None:
        mgr = SessionLockManager(None)
        async with mgr("s"):
            assert "s" in mgr._local and mgr._local_refs["s"] == 1
        assert mgr._local == {} and mgr._local_refs == {}

    asyncio.run(_drive())


def test_overlapping_same_session_share_lock_then_evict() -> None:
    """Two overlapping guards on one session must serialize on the SAME lock
    (refcount 2 while both are in flight) and the entry is evicted only once
    both have exited."""
    async def _drive() -> None:
        mgr = SessionLockManager(None)
        g1 = mgr("s")
        await g1.__aenter__()
        lock = mgr._local["s"]
        assert mgr._local_refs["s"] == 1

        entered_second = asyncio.Event()

        async def _second() -> None:
            async with mgr("s"):
                entered_second.set()

        task = asyncio.create_task(_second())
        await asyncio.sleep(0.01)  # let _second reach _acquire_local + block
        # Second guard registered its reference and reuses the SAME lock.
        assert mgr._local_refs["s"] == 2
        assert mgr._local["s"] is lock
        assert not entered_second.is_set()  # still blocked behind g1

        await g1.__aexit__(None, None, None)  # release → second acquires
        await asyncio.wait_for(task, timeout=1.0)
        assert entered_second.is_set()
        assert mgr._local == {} and mgr._local_refs == {}  # both gone → evicted

    asyncio.run(_drive())


# --- C2: atomic compare-and-act never touches a foreign lock ---------------


def test_release_does_not_delete_a_foreign_key() -> None:
    """If our key expired and another instance re-took it (a different token),
    our release must NOT delete it."""
    async def _drive() -> None:
        fakeredis = pytest.importorskip("fakeredis")
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        mgr = SessionLockManager(r, ttl_s=5.0, renew_s=10.0)  # no renew in-test
        key = "bp:session-lock:s"
        try:
            guard = mgr("s")
            await guard.__aenter__()
            assert await r.get(key) == guard._token
            # Simulate our key expiring + another instance taking it.
            await r.set(key, "FOREIGN-TOKEN", px=5000)
            await guard.__aexit__(None, None, None)
            # CAS-delete saw a non-matching token → left the foreign lock.
            assert await r.get(key) == "FOREIGN-TOKEN"
        finally:
            await r.aclose()

    asyncio.run(_drive())


def test_cas_extend_reports_not_held_for_foreign_key() -> None:
    """The renewal CAS returns 0 (→ 'lost while held') when the key is no
    longer ours, rather than extending a foreign holder's lock."""
    async def _drive() -> None:
        fakeredis = pytest.importorskip("fakeredis")
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        mgr = SessionLockManager(r, ttl_s=5.0, renew_s=10.0)
        key = "bp:session-lock:s"
        try:
            guard = mgr("s")
            await guard.__aenter__()
            assert await guard._cas(extend=True) == 1  # still ours → extended
            await r.set(key, "FOREIGN-TOKEN", px=5000)
            assert await guard._cas(extend=True) == 0  # not ours → not extended
            assert await r.get(key) == "FOREIGN-TOKEN"
            await guard.__aexit__(None, None, None)
        finally:
            await r.aclose()

    asyncio.run(_drive())
