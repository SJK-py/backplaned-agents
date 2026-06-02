"""chatbot.kakao_registry — the KakaoTalk deadline/next-touch state.

KakaoTalk gives one single-use `callbackUrl` per webhook (~1 min TTL),
but a dispatch routinely outlives it. When a turn overruns the callback
deadline the channel spends that callback on a "still working" status and
**parks** the in-flight turn here; the user's next touch (the `[확인]`
button or any message) delivers the finished answer on its fresh callback
([../../../docs/design/kakao-channel.md] §7).

Redis-backed (reusing the suite's `_redis`) so a parked turn survives a
process restart of the *channel loop* and is visible across instances.
Keyed by `chat_id`:

  * `kakao:turn:{chat_id}` — a hash: `state` (`pending`|`ready`),
    `user_id`, `task_id` (the router task, for `[중지]` cancel), `result`
    (the parked answer once ready), `stopped` (`1` after a `[중지]`).
  * `kakao:seen:{msg_id}` — a short-lived flag deduping the queue's
    at-least-once redelivery ([kakao-channel.md] §13).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _decode_map(data: dict[Any, Any]) -> dict[str, str]:
    """Normalize a Redis hash to str→str, whether or not the client was
    opened with `decode_responses` (the suite's is, but don't assume it)."""
    def _s(v: Any) -> str:
        return v.decode() if isinstance(v, bytes) else v
    return {_s(k): _s(v) for k, v in data.items()}


class KakaoTaskRegistry:
    """Per-chat in-flight/parked turn state over Redis."""

    def __init__(self, redis: Any, *, ttl_s: int) -> None:
        self._r = redis
        self._ttl = ttl_s

    @staticmethod
    def _turn_key(chat_id: str) -> str:
        return f"kakao:turn:{chat_id}"

    @staticmethod
    def _seen_key(msg_id: str) -> str:
        return f"kakao:seen:{msg_id}"

    # -- at-least-once dedupe -------------------------------------------

    async def seen(self, msg_id: str) -> bool:
        """Mark `msg_id` processed; return True if it was *already* seen.

        `SET NX` succeeds (returns truthy) only the first time, so a prior
        delivery — i.e. a duplicate — is exactly the case where it fails.
        """
        was_new = await self._r.set(self._seen_key(msg_id), "1", nx=True, ex=self._ttl)
        return not bool(was_new)

    # -- in-flight / parked turn ----------------------------------------

    async def try_begin(self, chat_id: str) -> bool:
        """Atomically claim the chat for a new turn: create the `pending`
        marker only if no turn exists. Returns True if claimed, False if a
        turn is already in flight (or a result is parked) for this chat —
        the race-free "one turn per chat" gate, safe under concurrent
        consumers. `HSETNX` is the atomic primitive (single-field create)."""
        key = self._turn_key(chat_id)
        created = await self._r.hsetnx(key, "state", "pending")
        if created:
            await self._r.expire(key, self._ttl)
        return bool(created)

    async def set_task(self, chat_id: str, user_id: str, task_id: str) -> None:
        """Attach the spawned router task to the already-claimed pending turn
        (so `[중지]` can cancel it)."""
        key = self._turn_key(chat_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"user_id": user_id, "task_id": task_id})
            pipe.expire(key, self._ttl)
            await pipe.execute()

    async def get_turn(self, chat_id: str) -> dict[str, str] | None:
        """The current turn hash, or None when there is no active turn."""
        data = await self._r.hgetall(self._turn_key(chat_id))
        return _decode_map(data) if data else None

    async def mark_stopped(self, chat_id: str) -> None:
        """Flag that the user pressed `[중지]`, so a turn that finishes after
        the cancel parks nothing (they already saw the stop ack). Re-applies
        the TTL so a key recreated by this `hset` (had it just lapsed) can't
        leak untouched."""
        key = self._turn_key(chat_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(key, "stopped", "1")
            pipe.expire(key, self._ttl)
            await pipe.execute()

    async def store_ready_unless_stopped(
        self, chat_id: str, result: str, images: str = ""
    ) -> bool:
        """Park a completed turn's answer for the next touch — but ONLY if the
        turn still exists and wasn't `/stop`'d. Atomic via WATCH/MULTI so a
        concurrent `[중지]` (`mark_stopped`) or `/new` (`clear`) can't be lost
        and an answer can't resurrect a turn the user abandoned. Returns True
        if parked, False if the turn was stopped/cleared underneath."""
        try:
            from redis.exceptions import WatchError  # noqa: PLC0415
        except Exception:  # pragma: no cover — redis present when a client is
            WatchError = ()  # type: ignore[assignment, misc]
        key = self._turn_key(chat_id)
        async with self._r.pipeline() as pipe:
            try:
                await pipe.watch(key)
                data = _decode_map(await pipe.hgetall(key))
                if not data or data.get("stopped"):
                    await pipe.unwatch()
                    return False
                pipe.multi()
                pipe.hset(
                    key, mapping={"state": "ready", "result": result, "images": images}
                )
                pipe.hdel(key, "task_id")
                pipe.expire(key, self._ttl)
                await pipe.execute()
                return True
            except WatchError:
                return False

    async def take_ready(self, chat_id: str) -> tuple[str, str] | None:
        """Atomically pop a parked answer `(text, images_json)` if one is
        ready, else None. WATCH/MULTI so two concurrent next-touch deliveries
        can't both pop the same answer (double-deliver)."""
        try:
            from redis.exceptions import WatchError  # noqa: PLC0415
        except Exception:  # pragma: no cover
            WatchError = ()  # type: ignore[assignment, misc]
        key = self._turn_key(chat_id)
        async with self._r.pipeline() as pipe:
            try:
                await pipe.watch(key)
                data = _decode_map(await pipe.hgetall(key))
                if data.get("state") != "ready":
                    await pipe.unwatch()
                    return None
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
                return data.get("result", ""), data.get("images", "")
            except WatchError:
                return None

    async def clear(self, chat_id: str) -> None:
        await self._r.delete(self._turn_key(chat_id))
