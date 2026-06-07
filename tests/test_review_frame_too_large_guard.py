"""R-MEDIUM #12: oversize inline-payload frames fail loud + actionable.

`image_part()` / `document_part()` base64-inline their bytes (≈+33%)
straight into the spawn/delegate payload. A payload over the
router's `max_payload_bytes` cap (1 MiB default) made the router
close the socket (1009) with no context, and the SDK `_send_pump`
re-queued the same frame → reconnect → 1009 → forever, with the
agent author never told why.

The fix (guidance, NOT auto-rerouting): a pre-send guard in
`WebSocketTransport.send()` scoped to `NewTaskFrame` raises a typed
`FrameTooLargeError` synchronously on the author's
`peers.spawn()`/`delegate()` call — before the frame is queued, so
the reconnect loop can't start — with a message that points inline
media at the out-of-band `ctx.files.put()` attachment path.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest


def _new_task_frame(payload: dict, *, task_id: str | None = None):
    from bp_protocol.frames import NewTaskFrame

    return NewTaskFrame(
        agent_id="agt",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id=task_id,
        destination_agent_id="dest",
        user_id="usr",
        session_id="sess",
        payload=payload,
    )


def _transport(*, cap: int | None):
    """Build a WebSocketTransport without the network handshake.
    `cap` None => leave `_welcome` unset (exercise the default-cap
    fallback); else plant a Welcome carrying that cap."""
    pytest.importorskip("websockets")
    from bp_protocol.frames import WelcomeFrame
    from bp_sdk.transport.ws import WebSocketTransport

    config = MagicMock()
    config.progress_buffer_size = 8
    info = MagicMock()
    info.agent_id = "agt"
    t = WebSocketTransport(config, info=info)
    if cap is not None:
        t._welcome = WelcomeFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
            session_id="sess",
            max_payload_bytes=cap,
        )
    return t


# ===========================================================================
# Module helpers
# ===========================================================================


def test_scan_inline_media_finds_nested_image_and_document() -> None:
    from bp_sdk.transport.ws import _scan_inline_media

    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"text": "look"},
                    {"image": {"mime_type": "image/png", "data": "A" * 40}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"document": {"mime_type": "application/pdf",
                                  "data": "B" * 60}},
                ],
            },
        ]
    }
    count, nbytes = _scan_inline_media(payload)
    assert count == 2
    assert nbytes == 100


def test_scan_inline_media_zero_for_plain_payload() -> None:
    from bp_sdk.transport.ws import _scan_inline_media

    assert _scan_inline_media({"prompt": "just text", "n": 3}) == (0, 0)
    # A non-dict `image`/`document` value isn't a media envelope.
    assert _scan_inline_media({"image": "not-a-dict"}) == (0, 0)


def test_scan_inline_media_depth_bounded() -> None:
    """A pathologically deep structure must not blow the stack on
    the error path — the walk is depth-capped."""
    from bp_sdk.transport.ws import _scan_inline_media

    node: dict = {"image": {"data": "x" * 10}}
    for _ in range(200):
        node = {"nested": node}
    # Returns (not RecursionError); the deep media is past the cap so
    # it simply isn't counted.
    count, _ = _scan_inline_media(node)
    assert count == 0


# ===========================================================================
# send() guard behaviour
# ===========================================================================


def test_oversize_inline_media_raises_with_actionable_message() -> None:
    from bp_sdk.errors import FrameTooLargeError

    async def _run() -> None:
        t = _transport(cap=400)
        frame = _new_task_frame({
            "content": [
                {"image": {"mime_type": "image/png", "data": "Z" * 2000}},
            ]
        })
        with pytest.raises(FrameTooLargeError) as ei:
            await t.send(frame)
        msg = str(ei.value)
        # Size math present.
        assert "over the router's 400-byte frame cap" in msg
        assert "max_payload_bytes" in msg
        # Inline-media-specific guidance + the out-of-band pointer.
        assert "base64 media part(s)" in msg
        assert "ctx.files.put()" in msg
        assert "attachments" in msg
        assert "docs/backplaned/sdk/core.md" in msg
        # Reconnect-loop prevention: never entered the outbox.
        assert t._outbox.qsize() == 0

    asyncio.run(_run())


def test_oversize_without_media_gets_generic_guidance() -> None:
    from bp_sdk.errors import FrameTooLargeError

    async def _run() -> None:
        t = _transport(cap=400)
        frame = _new_task_frame({"blob": "X" * 2000})
        with pytest.raises(FrameTooLargeError) as ei:
            await t.send(frame)
        msg = str(ei.value)
        assert "base64 media part(s)" not in msg
        # Still steers to attachments + the doc.
        assert "ctx.files.put()" in msg
        assert "docs/backplaned/sdk/core.md" in msg
        assert t._outbox.qsize() == 0

    asyncio.run(_run())


def test_under_cap_newtask_passes_through() -> None:
    async def _run() -> None:
        t = _transport(cap=1_048_576)
        frame = _new_task_frame({"prompt": "small"})
        await t.send(frame)
        assert t._outbox.qsize() == 1
        assert await t._outbox.get() is frame

    asyncio.run(_run())


def test_default_cap_used_when_no_welcome_yet() -> None:
    """Pre-handshake (defensive): the guard falls back to the
    protocol default cap, not None/0 — a modest payload still passes
    instead of spuriously raising."""
    async def _run() -> None:
        from bp_sdk.transport.ws import _DEFAULT_MAX_PAYLOAD_BYTES

        assert _DEFAULT_MAX_PAYLOAD_BYTES == 1_048_576
        t = _transport(cap=None)
        assert t._welcome is None
        frame = _new_task_frame({"data": "Q" * 5000})
        await t.send(frame)  # ~5 KB << 1 MiB default
        assert t._outbox.qsize() == 1

    asyncio.run(_run())


def test_guard_scoped_to_newtask_only() -> None:
    """The guard is intentionally NewTaskFrame-only: control frames
    are tiny and the router-side cap still protects integrity.
    A huge ProgressFrame passes the SDK guard (keeps the hot path
    serialize-free)."""
    from bp_protocol.frames import ProgressFrame

    async def _run() -> None:
        t = _transport(cap=200)
        prog = ProgressFrame(
            agent_id="agt",
            trace_id="0" * 32,
            span_id="0" * 16,
            task_id="t1",
            event="chunk",
            content="C" * 5000,  # way over the 200-byte cap
        )
        await t.send(prog)  # not raised — not a NewTaskFrame
        assert t._outbox.qsize() == 1

    asyncio.run(_run())


# ===========================================================================
# Type + structural pins
# ===========================================================================


def test_frame_too_large_is_a_valueerror_not_transporterror() -> None:
    """ValueError so it is loud and is NOT swallowed by the SDK
    loop's `except TransportError` handling — the author must see
    it and fix the payload."""
    from bp_sdk.errors import FrameTooLargeError, TransportError

    assert issubclass(FrameTooLargeError, ValueError)
    assert not issubclass(FrameTooLargeError, TransportError)


def test_guard_lives_in_send_not_send_pump() -> None:
    """Pin the placement: the size check must be in `send()`
    (pre-enqueue, on the caller's stack) — NOT `_send_pump`, where
    it would fire after the frame is already queued and re-queued
    forever."""
    from bp_sdk.transport.ws import WebSocketTransport

    send_src = inspect.getsource(WebSocketTransport.send)
    assert "FrameTooLargeError" in send_src
    assert "NewTaskFrame" in send_src
    pump_src = inspect.getsource(WebSocketTransport._send_pump)
    assert "FrameTooLargeError" not in pump_src
