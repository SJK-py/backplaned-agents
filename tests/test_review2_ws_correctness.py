"""Tests for the second-pass review WS-correctness bundle (C3, H2).

C3 — `_send_loop` re-queues an un-sent frame when `send_text`
raises, so a subsequent resume connection picks it up. Without
this, a frame popped from the outbox right before the socket
dropped is lost (silent drop for Result/Ack frames).

H2 — `_handle_ack` and `_handle_pong` only resolve correlations
that this socket actually has outstanding (per-socket
`inflight_correlations`). Without the check, agent A1 could
craft a Pong/Ack with another socket's correlation_id and
resolve that socket's pending future — keeping a wedged peer
alive past its heartbeat-timeout, or short-circuiting another
agent's pending operations.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# H2: per-socket Ack/Pong resolution
# ===========================================================================


def test_handle_ack_drops_when_correlation_not_in_socket_inflight() -> None:
    """An Ack with a correlation_id NOT in this socket's
    `inflight_correlations` is silently dropped — does NOT
    reach `state.correlation.resolve(...)`. Pins the cross-socket
    impersonation guard."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import AckFrame
    from bp_router import dispatch
    from bp_router.ws_hub import SocketEntry

    state = MagicMock()
    state.correlation.resolve = MagicMock()

    entry = SocketEntry(
        agent_id="agt_attacker",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    # NOTE: inflight_correlations is empty — this socket sent no Ping/Ack.
    assert entry.inflight_correlations == set()

    # Forge an Ack carrying SOME OTHER socket's correlation id.
    forged = AckFrame(
        agent_id="agt_attacker",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="cid_belonging_to_a_different_socket",
        accepted=True,
    )

    asyncio.run(dispatch._handle_ack(state, entry, forged))

    # Resolve was NOT called — the forged Ack got rejected.
    state.correlation.resolve.assert_not_called()


def test_handle_ack_resolves_when_correlation_is_in_socket_inflight() -> None:
    """Sanity: legitimate Acks (correlation IS in this socket's
    inflight set) DO resolve — pin the happy path so the H2
    guard doesn't accidentally break valid Acks."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import AckFrame
    from bp_router import dispatch
    from bp_router.ws_hub import SocketEntry

    state = MagicMock()
    state.correlation.resolve = MagicMock()

    entry = SocketEntry(
        agent_id="agt_alice",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    # This socket DID issue this correlation id.
    entry.inflight_correlations.add("legit_cid")

    legitimate = AckFrame(
        agent_id="agt_alice",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="legit_cid",
        accepted=True,
    )

    asyncio.run(dispatch._handle_ack(state, entry, legitimate))

    state.correlation.resolve.assert_called_once_with("legit_cid", legitimate)


def test_handle_pong_drops_cross_socket_keepalive_attack() -> None:
    """The DoS-by-keepalive scenario: agent A1 sends Pong with
    the router's outstanding Ping correlation id for agent A2.
    A2 is wedged and would normally hit heartbeat-timeout
    eviction. Without the H2 check, A1's forged Pong resolves
    A2's heartbeat future and keeps the wedged socket alive.

    With the check, A1's Pong is dropped because its
    `ref_correlation_id` isn't in A1's inflight set."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import PongFrame
    from bp_router import dispatch
    from bp_router.ws_hub import SocketEntry

    state = MagicMock()
    state.correlation.resolve = MagicMock()

    a1 = SocketEntry(
        agent_id="agt_attacker",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    # A1's inflight is EMPTY — it has no legitimate pending Pings.
    assert a1.inflight_correlations == set()

    forged_pong = PongFrame(
        agent_id="agt_attacker",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="cid_router_sent_to_a2_for_heartbeat",
    )

    asyncio.run(dispatch._handle_pong(state, a1, forged_pong))

    # The keepalive attack is rejected — A2's heartbeat future
    # is left to time out naturally.
    state.correlation.resolve.assert_not_called()


def test_handle_pong_resolves_legitimate_heartbeat_response() -> None:
    """Sanity: when the agent IS the one that received our Ping
    and responds with a matching Pong, resolution proceeds. Pins
    the heartbeat happy path."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import PongFrame
    from bp_router import dispatch
    from bp_router.ws_hub import SocketEntry

    state = MagicMock()
    state.correlation.resolve = MagicMock()

    entry = SocketEntry(
        agent_id="agt_alice",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    # The heartbeat loop added this Ping cid to the socket's
    # inflight set when sending.
    entry.inflight_correlations.add("router_ping_cid")

    response = PongFrame(
        agent_id="agt_alice",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="router_ping_cid",
    )

    asyncio.run(dispatch._handle_pong(state, entry, response))

    state.correlation.resolve.assert_called_once_with(
        "router_ping_cid", response,
    )


def test_heartbeat_loop_adds_ping_cid_to_inflight() -> None:
    """Source pin: the heartbeat loop adds the Ping cid to
    `entry.inflight_correlations` when registering and discards
    it in the finally so it doesn't leak. Without the add, the
    H2 check above would always fail for legitimate Pongs."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._heartbeat_loop)
    # Add when sending Ping.
    assert "entry.inflight_correlations.add(ping.correlation_id)" in src
    # Discard in finally so the membership doesn't outlive the
    # request (resolve doesn't auto-discard the cid).
    assert "entry.inflight_correlations.discard(ping.correlation_id)" in src
    # And the discard is INSIDE a finally, not just on the happy path.
    finally_idx = src.index("finally:")
    discard_idx = src.index(
        "entry.inflight_correlations.discard(ping.correlation_id)",
        finally_idx,
    )
    # The happy-path discard does NOT exist (would be a leak path).
    # The finally discard is the only one inside the heartbeat body.
    assert discard_idx > finally_idx


# ===========================================================================
# C3: _send_loop re-queues frame on send error
# ===========================================================================


def test_send_loop_requeues_frame_on_transport_error() -> None:
    """When `websocket.send_text(...)` raises, the frame the loop
    just popped is re-queued via `put_nowait` BEFORE the exception
    propagates. A subsequent resume connection (which shares the
    outbox by reference, or processes the same queue if attached
    fresh) sees the frame undelivered."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import AckFrame
    from bp_router.ws_hub import SocketEntry, _send_loop

    entry = SocketEntry(
        agent_id="agt_alice",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    # Stub websocket.send_text to raise (transport error).
    entry.websocket.send_text = AsyncMock(
        side_effect=ConnectionResetError("simulated dropped socket")
    )

    # Pre-load one frame.
    frame = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="cid_x",
        accepted=True,
    )

    async def _drive() -> None:
        await entry.outbox.put(frame)
        # Run _send_loop — it'll pop, attempt send, raise.
        with pytest.raises(ConnectionResetError):
            await _send_loop(entry)

    asyncio.run(_drive())

    # The frame must have been put back into the outbox before
    # the exception propagated.
    assert entry.outbox.qsize() == 1
    requeued = entry.outbox.get_nowait()
    assert requeued is frame


def test_send_loop_logs_when_requeue_overflows() -> None:
    """If the outbox is full when we try to re-queue (rare —
    the queue must have grown since we popped this frame), we
    log a warning and drop the frame. Without this branch, the
    `put_nowait` would itself raise QueueFull and mask the
    original transport error."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import AckFrame
    from bp_router.ws_hub import SocketEntry, _send_loop

    # Build a tiny outbox so we can fill it.
    entry = SocketEntry(
        agent_id="agt_alice",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    # Replace the outbox with a 1-capacity queue.
    entry.outbox = asyncio.Queue(maxsize=1)
    entry.websocket.send_text = AsyncMock(
        side_effect=ConnectionResetError("dropped")
    )

    target = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="cid_target",
        accepted=True,
    )
    blocker = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="cid_blocker",
        accepted=True,
    )

    async def _drive() -> None:
        await entry.outbox.put(target)
        # The send loop pops `target`, then before send_text raises
        # we slip another frame in to fill the queue. Use a side
        # effect on send_text to do that.

        async def _raising_send_after_fill(_payload: str) -> None:
            # Fill the (now-empty after pop) queue back to capacity.
            entry.outbox.put_nowait(blocker)
            raise ConnectionResetError("dropped")

        entry.websocket.send_text = _raising_send_after_fill  # type: ignore[assignment]
        with pytest.raises(ConnectionResetError):
            await _send_loop(entry)

    asyncio.run(_drive())

    # The original frame `target` was lost (re-queue failed because
    # `blocker` filled the slot). The queue still contains `blocker`.
    assert entry.outbox.qsize() == 1
    remaining = entry.outbox.get_nowait()
    assert remaining is blocker


def test_send_loop_source_documents_requeue_contract() -> None:
    """Source pin: `_send_loop` has the try/except around
    `send_text` with a `put_nowait` re-queue inside the
    handler, BEFORE the `raise`. Catches a refactor that drops
    the recovery."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._send_loop)
    # try/except wraps the send.
    assert "await entry.websocket.send_text(serialize_frame(frame))" in src
    assert "except (asyncio.CancelledError, Exception):" in src
    # Re-queue + raise.
    assert "entry.outbox.put_nowait(frame)" in src
    # Drop-with-warning fallback for full queue.
    assert "frame_dropped_send_failed_queue_full" in src


def test_send_loop_succeeds_normally_no_requeue() -> None:
    """Sanity: when send_text succeeds, the frame is NOT re-queued.
    Pin the happy path so the C3 fix doesn't accidentally introduce
    duplicate delivery."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import AckFrame
    from bp_router.ws_hub import SocketEntry, _send_loop

    entry = SocketEntry(
        agent_id="agt_alice",
        websocket=MagicMock(),
        session_token="x" * 24,
    )
    entry.websocket.send_text = AsyncMock(return_value=None)

    frame = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="cid_x",
        accepted=True,
    )

    async def _drive() -> None:
        await entry.outbox.put(frame)
        # Set closed to make the loop exit after one iteration.
        # (`while not entry.closed.is_set()` will be True after the
        # one frame, but to avoid infinite loop on subsequent .get()
        # we close BEFORE running.)
        # Trick: run the loop, set closed during the send, so the
        # next iteration's check exits.
        async def _send_then_close(_payload: str) -> None:
            entry.closed.set()

        entry.websocket.send_text = _send_then_close  # type: ignore[assignment]
        await _send_loop(entry)

    asyncio.run(_drive())

    # Queue is empty — frame was sent, not re-queued.
    assert entry.outbox.empty()
