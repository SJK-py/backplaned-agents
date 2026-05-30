"""bp_agents.session_lock — per-session serialization for channels.

[sessions.md §4] requires one in-flight op per `session_id` (message turns
+ summarization), so history appends don't interleave and the
`delegated_to` / `incumbent` flips don't race. In a single channel process
an `asyncio.Lock` suffices; to run more than one instance (a webapp
alongside the Telegram bot, or a horizontally-scaled channel) the lock must
be **cross-process**.

`SessionLockManager` provides both. Each acquisition takes:

  1. a process-local `asyncio.Lock` (FIFO/fairness within the process, and
     it means only ONE coroutine per process ever contends for Redis), then
  2. — when a Redis client is supplied — a Redis lock
     (`SET key token NX PX ttl`), held with a renewal watchdog and released
     with a compare-and-delete. A live holder keeps it via renewal; a
     crashed holder's key self-expires after `ttl_s` so another instance
     can take over.

Redis errors **fail open**: the acquisition degrades to local-only (logged
+ metric-free best-effort), never blocking a turn — the same availability
tradeoff the router takes for revocation/quota. With no Redis client the
manager is purely the in-process lock (today's behaviour).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

logger = logging.getLogger(__name__)


class SessionLockManager:
    """Hands out per-`session_id` async-context locks (see module docstring).

    `__call__(session_id)` returns an `async with`-able guard, so existing
    call sites (`async with core.session_lock(sid):`) are unchanged.
    """

    def __init__(
        self,
        redis: Any | None = None,
        *,
        ttl_s: float = 30.0,
        renew_s: float | None = None,
        poll_s: float = 0.1,
        key_prefix: str = "bp:session-lock:",
    ) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        # Renew comfortably inside the TTL so a slow refresh never lets it lapse.
        self._renew_s = renew_s if renew_s is not None else ttl_s / 3
        self._poll_s = poll_s
        self._prefix = key_prefix
        self._local: dict[str, asyncio.Lock] = {}
        # Refcount of guards that have ENTERED but not exited (held OR
        # waiting) per session_id, so `_release_local` can evict an idle
        # lock and `_local` can't grow without bound over a long-lived
        # process's lifetime (one Lock per distinct session id otherwise
        # leaks forever).
        self._local_refs: dict[str, int] = {}

    def _acquire_local(self, session_id: str) -> asyncio.Lock:
        """Get-or-create the per-session local lock and bump its refcount.
        Runs synchronously (no await) at guard entry, so two overlapping
        guards for the same session ALWAYS observe the same lock object —
        eviction can never hand a later guard a fresh lock while an earlier
        one still holds/awaits the old one (which would break exclusion)."""
        lock = self._local.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._local[session_id] = lock
        self._local_refs[session_id] = self._local_refs.get(session_id, 0) + 1
        return lock

    def _release_local(self, session_id: str) -> None:
        """Drop one guard's reference; evict the lock once none remain."""
        n = self._local_refs.get(session_id, 0) - 1
        if n <= 0:
            self._local_refs.pop(session_id, None)
            self._local.pop(session_id, None)
        else:
            self._local_refs[session_id] = n

    def __call__(self, session_id: str) -> _Guard:
        return _Guard(self, session_id)


class _Guard:
    def __init__(self, mgr: SessionLockManager, session_id: str) -> None:
        self._mgr = mgr
        self._sid = session_id
        # The local lock is acquired (and refcounted) in __aenter__ so the
        # refcount tracks entry→exit symmetrically and the manager can evict
        # idle entries.
        self._local: asyncio.Lock | None = None
        self._key = f"{mgr._prefix}{session_id}"
        self._token: str | None = None
        self._renewer: asyncio.Task | None = None

    async def __aenter__(self) -> _Guard:
        self._local = self._mgr._acquire_local(self._sid)
        await self._local.acquire()
        if self._mgr._redis is not None:
            try:
                await self._acquire_redis()
            except Exception:  # noqa: BLE001 — fail open to local-only
                logger.warning(
                    "session_lock_redis_unavailable_degraded",
                    extra={"event": "session_lock_redis_unavailable",
                           "bp.session_id": self._sid},
                )
                self._token = None
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self._renewer is not None:
                self._renewer.cancel()
                self._renewer = None
            if self._token is not None and self._mgr._redis is not None:
                try:
                    # Atomic compare-and-delete (WATCH/MULTI): only drop the
                    # key while it still holds OUR token, so we never delete a
                    # key another instance re-took after ours expired.
                    await self._cas(extend=False)
                except Exception:  # noqa: BLE001 — TTL reaps it anyway
                    logger.warning(
                        "session_lock_release_failed",
                        extra={"event": "session_lock_release_failed",
                               "bp.session_id": self._sid},
                    )
                self._token = None
        finally:
            if self._local is not None:
                self._local.release()
                self._mgr._release_local(self._sid)
                self._local = None

    async def _acquire_redis(self) -> None:
        token = secrets.token_urlsafe(16)
        ttl_ms = int(self._mgr._ttl_s * 1000)
        # Only one coroutine per process reaches here (the local lock is
        # held), so this poll contends solely with OTHER instances.
        while True:
            ok = await self._mgr._redis.set(
                self._key, token, nx=True, px=ttl_ms
            )
            if ok:
                break
            await asyncio.sleep(self._mgr._poll_s)
        self._token = token
        self._renewer = asyncio.create_task(self._renew_loop())

    async def _cas(self, *, extend: bool) -> int:
        """Atomic compare-and-act on our lock key via a WATCH/MULTI optimistic
        transaction: extend its TTL (``extend=True``) or delete it
        (``extend=False``), but ONLY while it still holds OUR token. Returns a
        truthy value when the op ran, 0 when the key isn't ours.

        The WATCH closes the GET→act race the old GET-then-PEXPIRE had: if the
        key expired and another instance re-took it between our check and our
        write, the WATCH makes EXEC abort (and the GET mismatch short-circuits
        the common case), so we never extend or delete a foreign holder's
        lock. Transient redis errors propagate to the caller (renew retries;
        release logs)."""
        try:
            from redis.exceptions import WatchError  # noqa: PLC0415
        except Exception:  # pragma: no cover — redis is present when a client is
            WatchError = ()  # type: ignore[assignment, misc]
        ttl_ms = int(self._mgr._ttl_s * 1000)
        async with self._mgr._redis.pipeline() as pipe:
            try:
                await pipe.watch(self._key)
                if await pipe.get(self._key) != self._token:
                    await pipe.unwatch()
                    return 0
                pipe.multi()
                if extend:
                    pipe.pexpire(self._key, ttl_ms)
                else:
                    pipe.delete(self._key)
                res = await pipe.execute()
                return res[0] if res else 0
            except WatchError:
                # Key changed under us between WATCH and EXEC → not ours.
                return 0

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self._mgr._renew_s)
            try:
                held = await self._cas(extend=True)
            except Exception:  # noqa: BLE001
                # Transient blip — keep trying; release/exit will clean up.
                continue
            if not held:
                # Lost the lock (TTL lapsed + another took it). Shouldn't
                # happen with renew_s < ttl_s, but surface it loudly.
                logger.warning(
                    "session_lock_lost_while_held",
                    extra={"event": "session_lock_lost_while_held",
                           "bp.session_id": self._sid},
                )
                return
