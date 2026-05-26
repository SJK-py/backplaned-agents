"""bp_router.delivery — Helpers to push frames to live agent sockets.

Lives outside ws_hub.py so tasks.py can deliver without circular imports.

**Result-frame durability** (R7 design decision, recorded so the
question doesn't re-open without context).

A `deliver_frame(Result, ...)` call can hit `asyncio.QueueFull` on
a saturated outbox. The R6 #174 metric (`router_deliver_frame_dropped_total`)
makes this observable. Durable recovery via a `pending_outbound`
table + replay-on-reconnect was considered and DEFERRED. The
reasoning:

  * The task row IS persisted in the DB at the moment the Result
    frame is fanned out (`tasks.complete_task` writes BEFORE the
    `deliver_frame` call). So the missed-on-wire Result doesn't
    lose the outcome — only the synchronous notification path.
  * `router_deliver_frame_dropped_total{frame_type="Result"}`
    surfaces the rate. Operators alerting on it can size the
    `per_socket_outbox_max` setting (#167) up if the rate matters.
  * Persistence of full Result payloads in a new table would
    widen the in-DB attack surface for sensitive output (the
    payload may carry user-bearing content) without serving an
    OBSERVED need until the rate is non-zero.

The reopen-the-question criteria: once `deliver_frame_dropped_total`
shows sustained non-zero in production, revisit. Options at that
point (in rough preference order): SDK-side polling of a new
user-facing `/v1/tasks/{id}` endpoint on reconnect (smaller
surface than persistence), or the full `pending_outbound` table
(maximal recovery, biggest surface). See R7 PR discussion.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bp_protocol.frames import AckFrame, Frame

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


class AgentNotConnected(Exception):
    """Raised when the destination agent has no live socket."""


def _record_deliver_dropped(frame_type: str) -> None:
    """Increment the per-frame-type drop counter. Wrapped so a
    metric-import / registry hiccup doesn't fail the caller's
    delivery path (which is already failing with QueueFull —
    don't compound the error)."""
    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            deliver_frame_dropped_total,
        )
        deliver_frame_dropped_total.labels(frame_type=frame_type).inc()
    except Exception:  # noqa: BLE001
        pass


async def deliver_frame(
    state: AppState,
    agent_id: str,
    frame: Frame,
    *,
    await_ack: bool = True,
    timeout_s: float | None = None,
) -> AckFrame | None:
    """Push a frame to `agent_id`'s outbox and (optionally) await its ack.

    Returns the AckFrame if `await_ack=True`, else None. Raises
    `AgentNotConnected` if no live socket is registered, and re-raises
    `TimeoutError` if the ack doesn't arrive in time.
    """
    entry = state.socket_registry.get(agent_id)  # type: ignore[attr-defined]
    if entry is None:
        raise AgentNotConnected(agent_id)

    fut = None
    if await_ack:
        fut = state.correlation.register(  # type: ignore[attr-defined]
            frame.correlation_id,
            timeout_s=timeout_s,
        )
        entry.inflight_correlations.add(frame.correlation_id)

    try:
        entry.outbox.put_nowait(frame)
    except asyncio.QueueFull:
        # Backpressure — caller decides what to do. Drop the pending future
        # we just registered so we don't leak it.
        if fut is not None:
            state.correlation.reject(  # type: ignore[attr-defined]
                frame.correlation_id, RuntimeError("backpressure")
            )
            entry.inflight_correlations.discard(frame.correlation_id)
        # Increment per-frame-type counter so operators can see
        # `Result` saturations distinctly from `NewTask` ones. R6
        # third-pass review: pre-fix Result-frame saturations were
        # silent — the task row reflected the terminal state in
        # the DB but the calling agent missed the wire notification.
        # Persistent recovery would require a `pending_outbound`
        # table + replay; that's larger architectural work. This
        # PR makes the saturation observable so operators can
        # alert on the rate and operators know agents must poll
        # task state via the admin API for terminal recovery.
        _record_deliver_dropped(frame.type)
        logger.warning(
            "deliver_frame_queue_full",
            extra={
                "event": "deliver_frame_queue_full",
                "bp.agent_id": agent_id,
                "bp.frame.type": frame.type,
                "bp.correlation_id": frame.correlation_id,
            },
        )
        raise

    if fut is None:
        return None

    try:
        ack = await fut
    finally:
        entry.inflight_correlations.discard(frame.correlation_id)
    return ack


def fanout_frame(
    state: AppState,
    agent_ids: list[str],
    frame: Frame,
) -> int:
    """Best-effort delivery to many agents. No ack; drops on missing peer.

    Used for Progress fan-out (the canonical fire-and-forget path).
    Returns the number of sockets the frame was queued on.
    """
    delivered = 0
    for agent_id in agent_ids:
        entry = state.socket_registry.get(agent_id)  # type: ignore[attr-defined]
        if entry is None:
            continue
        try:
            entry.outbox.put_nowait(frame)
            delivered += 1
        except asyncio.QueueFull:
            logger.warning(
                "fanout_drop",
                extra={
                    "event": "fanout_drop",
                    "bp.agent_id": agent_id,
                    "bp.frame.type": frame.type,
                },
            )
            _record_deliver_dropped(frame.type)
    return delivered
