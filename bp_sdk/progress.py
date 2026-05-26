"""bp_sdk.progress — ProgressEmitter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bp_protocol.frames import ProgressFrame

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Emit:
    event: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ProgressEmitter:
    """Best-effort progress emitter — never blocks the handler.

    The sync convenience methods (`chunk` / `status` / `tool_call` /
    `tool_result`) enqueue onto a bounded FIFO `asyncio.Queue`; a
    SINGLE consumer task (`_drain`) awaits `transport.send` for each
    item IN ORDER.

    Ordering is the point. The previous design scheduled an
    independent `asyncio.create_task(self.emit(...))` per call. Those
    tasks completed in whatever order the event loop happened to run
    them — under a busy loop a streamed LLM response emitted
    `chunk("Hel")`, `chunk("lo")` and the consumer could observe
    `"lo"` then `"Hel"`. A single FIFO consumer makes the on-the-wire
    order exactly the handler's call order.

    Bound: the queue is capped at `_PENDING_EMITS_SOFT_CAP`. A wedged
    transport (synchronous hang, not an exception) stalls the single
    drain; the queue then fills and further emits are dropped with a
    warn log rather than growing without bound — an LLM streaming 10K
    tokens would otherwise OOM the process. (The old per-call design
    had the same OOM risk via an unbounded strong-ref task set; this
    keeps the cap, now expressed as the queue's maxsize.)

    Lifecycle: `aclose()` stops the drain — best-effort flush of what
    is already queued, then a bounded cancel — and reaps the task.
    The dispatcher calls it in the handler-teardown `finally`,
    symmetric with `FileStash.cleanup()`. Without it the drain
    task outlived the handler and queued frames were silently lost.
    """

    # Bounded depth of the FIFO emit queue. The drain normally keeps
    # this near-empty (one transport.send per item); the cap only
    # engages when the transport is wedged. Class attribute so a
    # deployment can tune it via subclass / monkeypatch before the
    # emitter is constructed.
    _PENDING_EMITS_SOFT_CAP = 1000

    # aclose(): how long to let the drain flush already-queued items
    # before force-cancelling it. Bounds handler teardown when the
    # transport is wedged.
    _ACLOSE_DRAIN_TIMEOUT_S = 2.0

    # The drain polls `queue.get()` with this timeout so a stop
    # request is observed even when the queue is idle. A queue
    # sentinel would NOT work here: a drain wedged inside
    # `transport.send` never reads the queue, so the only thing that
    # can preempt it is cancellation — the stop path is
    # cancellation-based and this poll just bounds the idle-wakeup
    # latency.
    _DRAIN_GET_POLL_S = 0.2

    def __init__(self, ctx: TaskContext, dispatcher: Dispatcher) -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher
        self._queue: asyncio.Queue[_Emit] = asyncio.Queue(
            maxsize=self._PENDING_EMITS_SOFT_CAP
        )
        # Created lazily on first emit so a handler that never emits
        # progress doesn't spawn an idle drain task.
        self._drain_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._closed = False

    async def emit(
        self,
        event: str,
        content: str = "",
        **metadata: Any,
    ) -> None:
        frame = ProgressFrame(
            agent_id=self._dispatcher.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=self._ctx.task_id,
            event=event,
            content=content,
            metadata=metadata,
        )
        try:
            await self._dispatcher.transport.send(frame)
        except asyncio.CancelledError:
            # aclose() cancels a drain wedged in a hung transport.send.
            # Never swallow a CancelledError — re-raise so the drain
            # task actually unwinds.
            raise
        except Exception:  # noqa: BLE001
            # Best-effort: one transport hiccup must not kill the
            # drain (that would strand every SUBSEQUENT progress frame
            # for this task). Observable now (was a silent `pass`) but
            # at debug — progress is non-critical and a disconnect
            # storm would otherwise warn once per queued chunk.
            logger.debug(
                "progress_emit_failed",
                extra={
                    "event": "progress_emit_failed",
                    "dropped_event": event,
                    "bp.task_id": self._ctx.task_id,
                },
                exc_info=True,
            )

    async def _drain(self) -> None:
        while True:
            if self._stop.is_set() and self._queue.empty():
                return
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), self._DRAIN_GET_POLL_S
                )
            except TimeoutError:
                continue
            try:
                await self.emit(item.event, item.content, **item.metadata)
            finally:
                self._queue.task_done()

    def _ensure_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    def _enqueue(self, event: str, content: str = "", **metadata: Any) -> None:
        if self._closed:
            return
        self._ensure_drain()
        try:
            self._queue.put_nowait(_Emit(event, content, dict(metadata)))
        except asyncio.QueueFull:
            logger.warning(
                "progress_emit_dropped_queue_full",
                extra={
                    "event": "progress_emit_dropped_queue_full",
                    "dropped_event": event,
                    "cap": self._PENDING_EMITS_SOFT_CAP,
                    "bp.task_id": self._ctx.task_id,
                },
            )

    async def aclose(self) -> None:
        """Stop the drain and reap the task. Idempotent. Bounded so a
        wedged transport cannot stall handler teardown."""
        self._closed = True
        task = self._drain_task
        if task is None:
            return
        # Ask the drain to finish: flush what is already queued, then
        # exit (the top-of-loop `_stop and empty` check).
        self._stop.set()
        try:
            await asyncio.wait_for(task, self._ACLOSE_DRAIN_TIMEOUT_S)
        except TimeoutError:
            # Drain wedged inside transport.send — `wait_for` already
            # cancelled it on timeout; reap so it isn't orphaned.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except asyncio.CancelledError:
            # aclose() itself was cancelled (loop teardown). `wait_for`
            # cancelled the drain too; reap it, then honour our own
            # cancellation (asyncio contract: never swallow it).
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise
        except Exception:  # noqa: BLE001
            # The drain finished with an unexpected error; it is
            # already done, nothing to reap.
            pass

    # Convenience wrappers (sync, fire-and-forget — enqueue + return)
    def chunk(self, text: str) -> None:
        self._enqueue("chunk", text)

    def status(self, status: str) -> None:
        self._enqueue("status", status)

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._enqueue("tool_call", name, args=args)

    def tool_result(self, name: str, result: Any) -> None:
        self._enqueue("tool_result", name, result=result)
