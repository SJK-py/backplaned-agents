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

    def _local_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._local.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._local[session_id] = lock
        return lock

    def __call__(self, session_id: str) -> _Guard:
        return _Guard(self, session_id)


class _Guard:
    def __init__(self, mgr: SessionLockManager, session_id: str) -> None:
        self._mgr = mgr
        self._sid = session_id
        self._local = mgr._local_lock(session_id)
        self._key = f"{mgr._prefix}{session_id}"
        self._token: str | None = None
        self._renewer: asyncio.Task | None = None

    async def __aenter__(self) -> _Guard:
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
                    # Compare-and-delete: only drop the key if it's still
                    # ours. Not atomic with the GET, but the only race is
                    # when our key already expired (renewal lapsed for a
                    # full TTL — already degraded), so it's negligible; the
                    # TTL reaps a missed delete anyway.
                    if await self._mgr._redis.get(self._key) == self._token:
                        await self._mgr._redis.delete(self._key)
                except Exception:  # noqa: BLE001 — TTL reaps it anyway
                    logger.warning(
                        "session_lock_release_failed",
                        extra={"event": "session_lock_release_failed",
                               "bp.session_id": self._sid},
                    )
                self._token = None
        finally:
            self._local.release()

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

    async def _renew_loop(self) -> None:
        ttl_ms = int(self._mgr._ttl_s * 1000)
        while True:
            await asyncio.sleep(self._mgr._renew_s)
            try:
                # Extend only while the key is still ours (so a renewal
                # can't revive/extend a lock another instance took over).
                if await self._mgr._redis.get(self._key) != self._token:
                    held = False
                else:
                    held = await self._mgr._redis.pexpire(self._key, ttl_ms)
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
