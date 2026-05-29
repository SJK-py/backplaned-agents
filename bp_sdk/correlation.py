"""bp_sdk.correlation — SDK-side pending acks and pending peer results."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    future: asyncio.Future[Any]
    deadline: float = field(default=0.0)


class PendingMap:
    """Generic correlation_id → Future map with timeout reaping.

    Late-arriving resolve()s for keys that have not been registered yet
    are buffered for a short window so a subsequent register() picks
    them up immediately. This handles the unavoidable race when the
    receive loop processes a result before the awaiting coroutine has
    registered its future (e.g. after a multi-step spawn → ack →
    register-result-future flow).

    `_buffered` is capped at `BUFFER_MAX_SIZE` and ordered insertion-
    first so the oldest entry is evicted when a new resolve would
    overflow. The reaper trims stale entries every second; the cap
    is the safety net for the window before `start_reaper` runs and
    for high-churn periods that exceed the reaper's tick rate.

    `BUFFER_RESOLVES_S` and `BUFFER_MAX_SIZE` are class attributes
    so an `AgentConfig` can override them per-deployment via
    `PendingMap.BUFFER_RESOLVES_S = config.pending_buffer_window_s`
    at process start. Slow-network deployments tune these without
    forking the SDK.
    """

    BUFFER_RESOLVES_S: float = 5.0
    BUFFER_MAX_SIZE: int = 1024

    def __init__(self, *, default_timeout_s: float) -> None:
        self._pending: dict[str, _Pending] = {}
        # `OrderedDict` so eviction picks the oldest insertion when
        # the cap fires — without ordering we'd evict at random.
        self._buffered: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._default_timeout = default_timeout_s
        self._reaper: asyncio.Task | None = None

    @property
    def default_timeout_s(self) -> float:
        """Reaper deadline applied when `register(timeout_s=None)`."""
        return self._default_timeout

    def __contains__(self, correlation_id: object) -> bool:
        """True while a future for `correlation_id` is still pending (not yet
        resolved/rejected). Lets callers distinguish "awaiting this id" from
        "never registered / already settled"."""
        return correlation_id in self._pending

    def register(
        self, correlation_id: str, *, timeout_s: float | None = None
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        # If a value was buffered for this id, hand it back immediately.
        buffered = self._buffered.pop(correlation_id, None)
        if buffered is not None:
            value, _ts = buffered
            if isinstance(value, BaseException):
                fut.set_exception(value)
            else:
                fut.set_result(value)
            return fut

        self._pending[correlation_id] = _Pending(
            fut, loop.time() + (timeout_s or self._default_timeout)
        )
        return fut

    def resolve(self, correlation_id: str, value: Any) -> bool:
        entry = self._pending.pop(correlation_id, None)
        if entry is None:
            # Buffer for late register(). `get_running_loop` (NOT
            # `get_event_loop`) — the latter is deprecated in 3.12+
            # and raises when no loop is running. Both resolve and
            # reject only run inside the dispatcher's recv loop, so
            # there's always a running loop on the call.
            self._buffer_late_value(correlation_id, value)
            return False
        if not entry.future.done():
            entry.future.set_result(value)
        return True

    def reject(self, correlation_id: str, exc: BaseException) -> bool:
        entry = self._pending.pop(correlation_id, None)
        if entry is None:
            self._buffer_late_value(correlation_id, exc)
            return False
        if not entry.future.done():
            entry.future.set_exception(exc)
        return True

    def _buffer_late_value(self, correlation_id: str, value: Any) -> None:
        """Insert into `_buffered` with FIFO eviction at the cap.

        Without the cap, a runaway recv loop or a stalled-then-restored
        consumer can let `_buffered` grow unbounded. With it, the
        oldest unmatched correlation gets evicted to make room.
        """
        loop = asyncio.get_running_loop()
        # Evict the oldest if at cap. `popitem(last=False)` pulls the
        # earliest insertion; OrderedDict preserves insertion order.
        if (
            len(self._buffered) >= self.BUFFER_MAX_SIZE
            and correlation_id not in self._buffered
        ):
            self._buffered.popitem(last=False)
        self._buffered[correlation_id] = (value, loop.time())

    def reject_all(self, exc: BaseException) -> int:
        rejected = 0
        for cid in list(self._pending):
            if self.reject(cid, exc):
                rejected += 1
        return rejected

    def start_reaper(self) -> None:
        """Start the background reaper that times out pending entries.

        **Interaction with `asyncio.shield`.** Callers that wrap
        their `await fut` in `asyncio.shield` (e.g. `SpawnStream`
        manages its own cancellation via aclose) won't observe the
        reaper's `TimeoutError`: the reaper calls `reject()`, which
        sets the exception on the inner future, but the shielded
        outer wait was already abandoned. The TimeoutError lands on
        a future no one is awaiting — silently swallowed.

        This is BY DESIGN for SpawnStream et al., which own their
        own lifecycle and don't want the correlation reaper to
        surface a timeout while they're still iterating. Direct
        `await pending.future` callers DO see the timeout.

        Cancellation propagation: `asyncio.CancelledError` raised
        on the reaper task itself is logged at DEBUG and re-raised
        so process shutdown unwinds cleanly.
        """
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(1.0)
                now = loop.time()
                expired = [cid for cid, p in self._pending.items() if p.deadline <= now]
                for cid in expired:
                    self.reject(cid, TimeoutError("correlation_timeout"))
                # Drop buffered entries older than BUFFER_RESOLVES_S.
                stale = [
                    cid
                    for cid, (_, ts) in self._buffered.items()
                    if now - ts > self.BUFFER_RESOLVES_S
                ]
                for cid in stale:
                    self._buffered.pop(cid, None)
            except asyncio.CancelledError:
                # Log so a stray cancellation (e.g. a bug in the
                # supervisor that cancels the reaper task mid-run)
                # is visible to operators. Pre-R6 the reaper
                # exited silently — process shutdown is fine, but
                # an accidental cancel would have caused all
                # subsequent correlation timeouts to go unhandled
                # with no log trail.
                logger.debug(
                    "correlation_reaper_cancelled",
                    extra={"event": "correlation_reaper_cancelled"},
                )
                return

    async def stop_reaper(self) -> None:
        """Symmetric teardown for `start_reaper()`. Cancel the
        background reaper and await its exit so process shutdown
        doesn't orphan it ('Task was destroyed but it is pending').
        Idempotent / safe if the reaper was never started."""
        if self._reaper is None:
            return
        self._reaper.cancel()
        try:
            await self._reaper
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._reaper = None
