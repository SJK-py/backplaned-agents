"""bp_sdk.transport.ws — WebSocket transport for external agents.

Maintains one socket to the router; reconnects with jittered exponential
backoff. Hello/Welcome handshake on every (re)connect; offers
resume_token where applicable.

See `docs/backplaned/router/protocol.md` §3 for the lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.frames import (
    ErrorFrame,
    Frame,
    HelloFrame,
    NewTaskFrame,
    WelcomeFrame,
    parse_frame,
    serialize_frame,
)
from bp_sdk.errors import FrameTooLargeError

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)

# Fallback frame-size cap used only before the first Welcome is
# received (spawn/delegate run inside a handler, i.e. post-handshake,
# so this is purely defensive). Mirrors `WelcomeFrame.max_payload_bytes`
# and the router's `Settings.max_payload_bytes` default (1 MiB).
_DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576

# Bound the recursive inline-media walk in the oversize diagnostic.
# Real payloads nest a few levels (messages → content → part); this
# only guards a pathological / adversarial structure from pinning CPU
# on the error path.
_MEDIA_SCAN_MAX_DEPTH = 16

# Bounded window `close()` gives the still-running send pump to
# flush `_outbox` (terminal frames a shutdown-cancelled handler
# just enqueued) before the pump is cancelled. A wedged socket
# can't stall shutdown past this.
_CLOSE_DRAIN_TIMEOUT_S = 5.0


def _scan_inline_media(node: object, depth: int = 0) -> tuple[int, int]:
    """Walk a NewTaskFrame payload for the envelopes `image_part()` /
    `document_part()` produce — `{"image"|"document": {"data": <b64>,
    ...}}` — wherever they're nested (typically inside a
    `Message.content` list). Returns `(count, approx_base64_bytes)`
    for the oversize diagnostic. Best-effort and bounded; never
    raises (it runs only on the already-failing send path)."""
    if depth > _MEDIA_SCAN_MAX_DEPTH:
        return (0, 0)
    count = 0
    nbytes = 0
    if isinstance(node, dict):
        for key in ("image", "document"):
            media = node.get(key)
            if isinstance(media, dict):
                data = media.get("data")
                if isinstance(data, str):
                    count += 1
                    nbytes += len(data)
        for value in node.values():
            if isinstance(value, (dict, list)):
                c, b = _scan_inline_media(value, depth + 1)
                count += c
                nbytes += b
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                c, b = _scan_inline_media(item, depth + 1)
                count += c
                nbytes += b
    return (count, nbytes)


def _oversize_frame_message(
    frame: NewTaskFrame, actual: int, cap: int
) -> str:
    """Actionable error text for an over-cap NewTaskFrame: the size
    math plus, when inline media is the culprit, a pointer to the
    out-of-band attachment path (the #12 fix is guidance, not
    auto-rerouting)."""
    who = frame.task_id or "(spawn — router-assigned id)"
    base = (
        f"NewTaskFrame for task {who} is {actual:,} bytes, over the "
        f"router's {cap:,}-byte frame cap (WelcomeFrame."
        f"max_payload_bytes). The router would close the socket "
        f"(1009) and the SDK would retry the same frame forever."
    )
    media_count, media_bytes = _scan_inline_media(frame.payload)
    if media_count:
        return (
            f"{base} The payload inlines {media_count} base64 media "
            f"part(s) (~{media_bytes:,} bytes of it). image_part() / "
            f"document_part() base64-encode bytes (~33% larger than "
            f"raw) directly into the payload — large media must go "
            f"out-of-band instead: upload with ctx.files.put() and "
            f"pass the reference through the task's attachments. See "
            f"docs/backplaned/sdk/core.md (file attachments)."
        )
    return (
        f"{base} Reduce the payload, or move large binary content to "
        f"file attachments (ctx.files.put()) rather than the inline "
        f"payload. See docs/backplaned/sdk/core.md."
    )


class WebSocketTransport:
    """One-socket-per-agent transport.

    `recv()` blocks until a frame is available; reconnect is transparent.
    `send()` queues into the active socket; on disconnect, frames sit in
    the outbox until the next connection drains them.
    """

    def __init__(self, config: AgentConfig, *, info: AgentInfo) -> None:
        self.config = config
        self.info = info
        self._inbox: asyncio.Queue[Frame] = asyncio.Queue()
        self._outbox: asyncio.Queue[Frame] = asyncio.Queue(
            maxsize=config.progress_buffer_size
        )
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()
        self._welcome: WelcomeFrame | None = None
        self._resume_token: str | None = None
        self._loop_tasks: list[asyncio.Task] = []
        self._ws: object | None = None

    @classmethod
    async def connect(
        cls, config: AgentConfig, *, info: AgentInfo
    ) -> WebSocketTransport:
        t = cls(config, info=info)
        await t._start()
        # Wait for the first successful handshake before returning.
        await t._connected.wait()
        return t

    # ------------------------------------------------------------------
    # Public Transport surface
    # ------------------------------------------------------------------

    async def send(self, frame: Frame) -> None:
        # Pre-send size guard for agent-authored task payloads. Only
        # NewTaskFrame (spawn/delegate) can carry author-controlled
        # bulk (inline image_part/document_part); control frames
        # (Ack/Pong) are tiny, so scoping here keeps the hot path
        # serialize-free while catching the #12 footgun. Raising
        # HERE — synchronously, before _outbox.put — surfaces it on
        # the author's peers.spawn()/delegate() call AND avoids the
        # _send_pump re-queue→reconnect→1009 loop an oversize frame
        # would otherwise wedge the agent in.
        if isinstance(frame, NewTaskFrame):
            cap = (
                self._welcome.max_payload_bytes
                if self._welcome is not None
                else _DEFAULT_MAX_PAYLOAD_BYTES
            )
            actual = len(serialize_frame(frame).encode("utf-8"))
            if actual > cap:
                raise FrameTooLargeError(
                    _oversize_frame_message(frame, actual, cap)
                )
        await self._outbox.put(frame)

    async def recv(self) -> Frame:
        return await self._inbox.get()

    async def close(self) -> None:
        # Best-effort bounded flush BEFORE cancelling the send pump.
        # Shutdown hard-cancels in-flight handlers; their `_run_handler`
        # `except asyncio.CancelledError` enqueues a terminal CANCELLED
        # Result onto `_outbox` (R9). If we cancel the pump first those
        # frames are lost and the calling parent hangs to
        # `correlation_timeout`. Give the still-running pump a brief,
        # bounded window to drain (a wedged socket can't stall shutdown
        # past the timeout). `_closed` is set AFTER the drain so the
        # supervisor doesn't tear the live socket down mid-flush.
        if self._connected.is_set() and not self._closed.is_set():
            try:
                async with asyncio.timeout(_CLOSE_DRAIN_TIMEOUT_S):
                    # `join()` (not an `empty()` poll) so the drain
                    # waits for the frame currently IN `ws.send` too.
                    # The pump does `get()` THEN `await ws.send()`, so
                    # an `empty()` check races the transient window
                    # where a terminal CANCELLED Result is popped but
                    # not yet on the wire — exiting there and
                    # cancelling the pump mid-send loses that frame
                    # and re-strands the parent to correlation_timeout.
                    # `join()` returns only once every popped frame has
                    # its matching `task_done()` (sent or dropped); a
                    # wedged socket is still bounded by the timeout.
                    await self._outbox.join()
            except (TimeoutError, Exception):  # noqa: BLE001
                pass
        self._closed.set()
        for t in self._loop_tasks:
            t.cancel()
        for t in self._loop_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    @property
    def welcome(self) -> WelcomeFrame | None:
        return self._welcome

    def update_catalog(self, catalog: dict) -> None:
        """Replace the cached `available_destinations` snapshot in place.

        Mutates the stored `WelcomeFrame` so callers reading
        `transport.welcome.available_destinations` (e.g. `peers.visible()`)
        see the new payload without rebinding `welcome`. The frame
        model is intentionally not `frozen=True` for this reason.

        No-op when the WS has yet to complete its first handshake; the
        next Welcome will carry the up-to-date catalog anyway.
        """
        if self._welcome is None:
            return
        self._welcome.available_destinations = catalog

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        self._loop_tasks.append(asyncio.create_task(self._connection_supervisor()))

    async def _connection_supervisor(self) -> None:
        backoff = self.config.reconnect_initial_backoff_s
        while not self._closed.is_set():
            try:
                await self._run_one_connection()
                # Clean exit (peer closed) — start a fresh backoff cycle.
                backoff = self.config.reconnect_initial_backoff_s
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws_connection_failed",
                    extra={
                        "event": "ws_connection_failed",
                        "error": repr(exc),
                    },
                )

            if self._closed.is_set():
                return

            jitter = backoff * random.uniform(0.5, 1.5)
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=jitter)
                return
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self.config.reconnect_max_backoff_s)

    async def _run_one_connection(self) -> None:
        """Open the socket, do handshake, run send/recv pumps until close."""
        import websockets  # noqa: PLC0415

        async with websockets.connect(
            self.config.router_url,
            # Hard receive ceiling. MUST sit above the router's
            # negotiated `WelcomeFrame.max_payload_bytes` with
            # envelope headroom — tripping it closes with 1009 (hard
            # socket teardown), which would override the graceful
            # `FrameTooLargeError`/admit-reject path. Default 2 MiB
            # pairs with the router's 1 MiB payload cap default
            # (~2× envelope budget); raise both in lockstep — see
            # `AgentConfig.ws_max_receive_bytes` + `core.md` §7.
            max_size=self.config.ws_max_receive_bytes,
            ping_interval=None,  # we run our own heartbeat at protocol level
        ) as ws:
            self._ws = ws
            try:
                welcome = await self._do_hello(ws)
            except Exception:
                self._ws = None
                raise

            self._welcome = welcome
            self._resume_token = welcome.session_id
            self._connected.set()

            recv_task = asyncio.create_task(self._recv_pump(ws))
            send_task = asyncio.create_task(self._send_pump(ws))
            try:
                done, pending = await asyncio.wait(
                    [recv_task, send_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    exc = t.exception()
                    if exc is not None:
                        raise exc
            finally:
                for t in (recv_task, send_task):
                    t.cancel()
                for t in (recv_task, send_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                self._connected.clear()
                self._ws = None

    async def _do_hello(self, ws) -> WelcomeFrame:  # type: ignore[no-untyped-def]
        if not self.config.auth_token:
            raise RuntimeError(
                "WebSocketTransport.connect(): no auth_token in AgentConfig — "
                "run onboarding first via bp_sdk.onboarding.onboard_or_resume"
            )

        hello = HelloFrame(
            agent_id=self.info.agent_id,
            trace_id="0" * 32,
            span_id="0" * 16,
            auth_token=self.config.auth_token,
            sdk_version="0.1.0",
            agent_info=self.info,
            resume_token=self._resume_token,
        )
        await ws.send(serialize_frame(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        frame = parse_frame(raw)

        if isinstance(frame, ErrorFrame):
            raise RuntimeError(f"router rejected Hello: {frame.code}: {frame.message}")
        if not isinstance(frame, WelcomeFrame):
            raise RuntimeError(f"expected Welcome, got {frame.type}")
        if frame.protocol_version != PROTOCOL_VERSION:
            raise RuntimeError(
                f"router protocol_version {frame.protocol_version!r} mismatch"
            )
        logger.info(
            "ws_connected",
            extra={
                "event": "ws_connected",
                "bp.agent_id": self.info.agent_id,
                "session_id": frame.session_id,
            },
        )
        return frame

    async def _recv_pump(self, ws) -> None:  # type: ignore[no-untyped-def]
        async for raw in ws:
            try:
                frame = parse_frame(raw)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "frame_parse_failed",
                    extra={"event": "frame_parse_failed"},
                )
                continue
            await self._inbox.put(frame)

    async def _send_pump(self, ws) -> None:  # type: ignore[no-untyped-def]
        while True:
            frame = await self._outbox.get()
            try:
                await ws.send(serialize_frame(frame))
                # Balances the get() so `_outbox.join()` (close()'s
                # shutdown drain) can tell this frame is fully on the
                # wire — not merely popped.
                self._outbox.task_done()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # Re-queue the un-sent frame so the supervisor's
                # subsequent reconnect can drain it. Mirrors the
                # router-side fix in
                # `bp_router/ws_hub.py:_send_loop`. Without the
                # re-queue, a frame already popped from `_outbox`
                # before `ws.send` raised is silently lost — Result
                # / Ack / Progress frames the router was expecting
                # vanish. asyncio.Queue doesn't support push-front,
                # so re-queue introduces slight reordering vs. other
                # late frames; acceptable per the protocol (most
                # frames are independent; ordered streams like
                # LlmDelta are scoped per correlation_id where this
                # frame's order vs. others doesn't change).
                try:
                    self._outbox.put_nowait(frame)
                except asyncio.QueueFull:
                    # Outbox is full → we can't preserve the frame.
                    # Logged so operators can correlate with
                    # downstream "missing terminal" reports.
                    logger.warning(
                        "frame_dropped_send_failed_queue_full",
                        extra={
                            "event": "frame_dropped_send_failed_queue_full",
                            "bp.agent_id": self.info.agent_id,
                            "frame_type": frame.type,
                        },
                    )
                # Balance the get() for THIS pop before propagating
                # (whether re-queued as a fresh unfinished item or
                # dropped on QueueFull) — otherwise the unfinished
                # count never returns to zero and close()'s
                # `_outbox.join()` blocks until its timeout on every
                # send failure.
                self._outbox.task_done()
                raise
