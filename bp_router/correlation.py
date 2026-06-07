"""bp_router.correlation — In-memory pending-ack futures.

Frame-level acks live here. Task-level outcomes live in the `tasks`
table — they don't need a second pending-results map at the router
because SQL + timeout_sweep already serve that role
(`docs/backplaned/router/protocol.md` §4).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    future: asyncio.Future[Any]
    deadline: float = field(default=0.0)


class PendingAcks:
    """`correlation_id → Future` map for frames the router is waiting to ack.

    Used when the router sends a frame to an agent and needs to know
    whether it was accepted. Bounded; entries past their deadline are
    reaped by `_reap_loop`.
    """

    def __init__(self, *, default_timeout_s: float = 30.0) -> None:
        self._pending: dict[str, _Pending] = {}
        self._default_timeout = default_timeout_s
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, correlation_id: str, *, timeout_s: float | None = None) -> asyncio.Future:
        """Reserve a slot. Caller awaits the returned future."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        deadline = loop.time() + (timeout_s or self._default_timeout)
        self._pending[correlation_id] = _Pending(fut, deadline)
        return fut

    def resolve(self, correlation_id: str, value: Any) -> bool:
        """Resolve an awaiting Future. Returns True if a future was found."""
        entry = self._pending.pop(correlation_id, None)
        if entry is None:
            return False
        if not entry.future.done():
            entry.future.set_result(value)
        return True

    def reject(self, correlation_id: str, exc: BaseException) -> bool:
        entry = self._pending.pop(correlation_id, None)
        if entry is None:
            return False
        if not entry.future.done():
            entry.future.set_exception(exc)
        return True

    def reject_ids(self, correlation_ids) -> int:  # type: ignore[no-untyped-def]
        """Fail the pending futures for the given correlation ids.

        Used on agent disconnect. Pre-R8 this was `reject_all_for`,
        which scanned the ENTIRE global `_pending` map (every
        pending ack across every socket) and ran a predicate on
        each — O(total_pending) on every disconnect even though the
        caller only ever needs its OWN socket's (small)
        `inflight_correlations` set. Iterating the caller-supplied
        ids and doing an O(1) `reject` per id makes a flapping-
        reconnect storm O(this_socket_inflight) instead of
        O(all_sockets_inflight) per disconnect.

        Unknown ids (already acked / reaped) are skipped silently —
        `reject` returns False for a missing key.
        """
        rejected = 0
        for cid in list(correlation_ids):
            if self.reject(cid, ConnectionError("agent_disconnected")):
                rejected += 1
        return rejected

    # ------------------------------------------------------------------
    # Reaper
    # ------------------------------------------------------------------

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def stop_reaper(self) -> None:
        """Cancel the reaper task and wait for it to exit. No-op if the
        reaper was never started or already finished. Safe to call from
        the lifespan `finally` block."""
        reaper = self._reaper
        if reaper is None or reaper.done():
            return
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass

    async def _reap_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(1.0)
                now = loop.time()
                expired = [cid for cid, p in self._pending.items() if p.deadline <= now]
                for cid in expired:
                    self.reject(cid, TimeoutError("ack_timeout"))
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("pending_acks_reaper_failed")
