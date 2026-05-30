"""bp_sdk.dispatch — Receive loop, send queue drain, heartbeat.

Runs the receive coroutine that classifies inbound frames:
  - NewTask → TaskContext build + handler invocation + Result emission
  - Result → resolve correlated peer-call Future
  - Cancel → trip cancel token on the matching task
  - Progress → forward to subscriber
  - Ack → resolve send-side Future
  - Ping → respond Pong
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from bp_protocol.errors import safe_validator_message
from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    CatalogUpdateFrame,
    ErrorFrame,
    FileResultFrame,
    FileUploadGrantFrame,
    Frame,
    LlmDeltaFrame,
    LlmResultFrame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
)
from bp_protocol.types import AgentOutput, TaskStatus
from bp_sdk.context import CancelToken, TaskContext
from bp_sdk.correlation import PendingMap
from bp_sdk.errors import (
    CancellationError,
    HandlerError,
    TransportError,
    TransportPermanentlyFailed,
)

if TYPE_CHECKING:
    from bp_sdk.agent import Agent
    from bp_sdk.transport.base import Transport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class _ActiveTask:
    task_id: str
    cancel_token: CancelToken
    handler_task: asyncio.Task | None = None


class HandlerExited(Exception):
    """Raised on still-pending peer-call / LLM futures when the
    handler that registered them terminates.

    The dispatcher's `_run_handler` finally drains
    `_task_correlations[task_id]` with this exception so callers
    awaiting those futures fail fast instead of timing out at the
    `correlation_timeout` boundary (default 30 s).

    Carries the originating task_id for log diagnostics. Distinct
    from `CancellationError` (cooperative cancel via cancel_token)
    and `TimeoutError` (correlation_timeout reaper).
    """

    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"handler for task {task_id!r} exited with this future "
            "still pending"
        )
        self.task_id = task_id


class Dispatcher:
    """The runtime that ties Agent + Transport together."""

    def __init__(self, agent: Agent, transport: Transport) -> None:
        self.agent = agent
        self.transport = transport
        # Apply per-deployment overrides for the early-resolve buffer
        # tunables. Class-attribute mutation is fine because
        # PendingMap reads them on every call — there's no
        # captured-at-init copy.
        cfg = agent.config
        PendingMap.BUFFER_RESOLVES_S = cfg.pending_buffer_window_s
        PendingMap.BUFFER_MAX_SIZE = cfg.pending_buffer_max_size
        self.pending_acks = PendingMap(
            default_timeout_s=cfg.pending_acks_timeout_s,
        )
        self.pending_results = PendingMap(
            default_timeout_s=cfg.pending_results_timeout_s,
        )
        self._active: dict[str, _ActiveTask] = {}
        self._loops: list[asyncio.Task] = []
        # Set during graceful shutdown: stop ADMITTING new tasks, but keep
        # the recv loop running so in-flight handlers still receive the
        # Ack/Result/LlmResult frames they're awaiting and can finish.
        self._draining: bool = False
        # correlation_id → asyncio.Queue used by streaming LLM calls.
        # Keyed on the LlmRequest.correlation_id; LlmDelta frames are
        # pushed in arrival order and the terminal LlmResult is pushed
        # last, ending the SDK-side iterator.
        self._llm_streams: dict[str, asyncio.Queue] = {}
        # child task_id → asyncio.Queue. Populated by SpawnStream when an
        # agent calls peers.spawn(..., stream=True). _handle_progress
        # pushes ProgressFrames; the matching ResultFrame (handled in
        # _dispatch) is pushed last to terminate iteration.
        self._progress_subscribers: dict[str, asyncio.Queue] = {}
        # task_id → list of ProgressFrames received BEFORE the
        # corresponding `subscribe_progress` call. `peers.spawn(..,
        # stream=True)` subscribes only AFTER the spawn Ack lands;
        # the router can begin streaming Progress frames immediately
        # after admit, so a small race window exists where
        # `_handle_progress` finds no subscriber and would drop the
        # frame on the floor — the SpawnStream consumer would
        # silently miss content.
        #
        # Buffer is bounded: at most _PROGRESS_BUFFER_PER_TASK frames
        # per task_id, at most _PROGRESS_BUFFER_MAX_TASKS distinct
        # task_ids. A frame arriving when either cap is hit is
        # dropped with a warning — same fail-mode as a slow consumer.
        # Drained into the subscriber queue on `subscribe_progress`.
        self._pending_progress_buffer: dict[
            str, list[ProgressFrame]
        ] = {}
        # task_id → set of (PendingMap, correlation_id) registered
        # while the handler for that task is running. Used to reject
        # in-flight peer-call / LLM futures on handler termination
        # rather than waiting them out at `correlation_timeout`.
        # `register_for_task` enrolls; the done-callback wired on
        # the future untracks on resolve;
        # `_drain_task_correlations` rejects whatever's left when
        # the handler exits.
        self._task_correlations: dict[str, set[tuple[PendingMap, str]]] = {}

    # Class-level buffer caps for the pre-subscribe progress race.
    # Class-level so deployments with unusual workloads can tune via
    # subclass / monkeypatch.
    _PROGRESS_BUFFER_PER_TASK = 16
    _PROGRESS_BUFFER_MAX_TASKS = 256

    # ------------------------------------------------------------------
    # Run / shutdown
    # ------------------------------------------------------------------

    async def run_until(self, stop_event: asyncio.Event) -> None:
        self.pending_acks.start_reaper()
        self.pending_results.start_reaper()

        recv_loop = asyncio.create_task(self._recv_loop())
        self._loops = [recv_loop]

        # Wait for either external stop or the recv loop dying.
        stop_task = asyncio.create_task(stop_event.wait())
        recv_death: BaseException | None = None
        try:
            done, _pending = await asyncio.wait(
                [recv_loop, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            # Stop admitting NEW tasks for the rest of shutdown, but DON'T
            # cancel the recv loop yet (the finally does that). Keeping it
            # alive through the drain is the whole point: in-flight handlers
            # blocked on `await ctx.llm.generate(...)` / `ctx.peers.spawn(...)`
            # still need the recv loop to dispatch the Ack/Result/LlmResult
            # frames that unblock them. Cancelling it before the drain (the
            # old behaviour) starved every mid-call handler, so all of them
            # hit the hard-cancel deadline instead of finishing cooperatively.
            self._draining = True
            stop_task.cancel()  # done waiting on the stop signal either way
            recv_alive = recv_loop not in done
            # asyncio.wait swallows a completed task's exception — capture the
            # recv loop's so a permanently-dead transport surfaces as a
            # non-zero exit (HIGH-1) instead of an indistinguishable clean
            # return. recv_loop is in `done` only if it finished on its own.
            if not recv_alive and not recv_loop.cancelled():
                recv_death = recv_loop.exception()
            # A dead transport can deliver none of the awaited frames, so
            # don't burn the full grace window waiting for them — trip + reap
            # promptly. A live transport gets the full cooperative grace.
            await self._drain_in_flight(grace_s=30.0 if recv_alive else 0.0)
            # The transport can also die DURING the drain (recv_alive was True
            # at the top but the loop has since finished on its own). Capture
            # that exception too — otherwise the `finally` cancel below masks
            # it and run_until returns cleanly despite a permanently-failed
            # transport. (No-op on a clean stop: recv_loop is still running.)
            if (
                recv_death is None
                and recv_loop.done()
                and not recv_loop.cancelled()
            ):
                recv_death = recv_loop.exception()
        finally:
            # Unconditional teardown — runs on clean stop AND on recv
            # death. Pre-fix only `self._loops` (recv loop) was
            # cancelled: the two correlation reapers were orphaned
            # ("Task was destroyed but it is pending") and the
            # pending maps were never rejected, so any peer/LLM/ack
            # future not covered by the in-flight drain hung to its
            # full correlation timeout (HIGH-2).
            for t in self._loops:
                t.cancel()
            await self.pending_acks.stop_reaper()
            await self.pending_results.stop_reaper()
            self.pending_acks.reject_all(
                TransportError("agent shutting down: correlation abandoned")
            )
            self.pending_results.reject_all(
                TransportError("agent shutting down: correlation abandoned")
            )

        if recv_death is not None:
            raise recv_death

    async def _drain_in_flight(
        self, *, grace_s: float, hard_drain_timeout_s: float = 5.0
    ) -> None:
        deadline = asyncio.get_running_loop().time() + grace_s
        while self._active:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                cancelled: list[asyncio.Task] = []
                for entry in list(self._active.values()):
                    # Cooperative handlers had the full `grace_s` to
                    # finish naturally. At the deadline, trip the
                    # token (a last "please stop" for any handler
                    # still polling it) AND hard-cancel the handler
                    # task — an uncooperative handler that ignores
                    # `ctx.cancel_token` would otherwise run unbounded
                    # past shutdown, leaking the task. The hard cancel
                    # delivers `asyncio.CancelledError` into
                    # `_run_handler`, which (R9) still emits a
                    # terminal CANCELLED Result before propagating.
                    entry.cancel_token.trip("shutdown")
                    # `handler_task` is briefly None between the
                    # pre-ack registration (cancel-race fix) and
                    # create_task. Tripping the token above covers
                    # that window; only hard-cancel + await a task
                    # that actually exists.
                    if entry.handler_task is not None:
                        entry.handler_task.cancel()
                        cancelled.append(entry.handler_task)
                # CRIT: actually AWAIT the hard-cancelled handlers.
                # Without this the loop `break`s immediately and
                # `run_until` tears the transport down before
                # `_run_handler`'s `except asyncio.CancelledError`
                # ever runs — so the terminal CANCELLED Result is
                # never emitted and the calling parent's spawn future
                # hangs to `correlation_timeout` (defeating the R9
                # fix). Bounded so a handler wedged in an
                # uncancellable call can't stall shutdown forever.
                if cancelled:
                    await asyncio.wait(
                        cancelled, timeout=hard_drain_timeout_s
                    )
                break
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        # Hard cap on consecutive recv failures before bailing out.
        # Without it, a synchronous bug in `transport.recv()`
        # (programming error, decoding bug, dead supervisor)
        # busy-loops at 100% CPU spamming `recv_failed` logs forever
        # — `run_until` never sees the dispatcher die.
        # Sixteen consecutive failures with bounded backoff means
        # we tolerate ~10 s of transient transport flakiness, then
        # surface the failure so the agent can shut down or
        # reconnect. The threshold is configurable via
        # `AgentConfig.recv_consecutive_failures_max` — tune for
        # noisy networks or in tests that want faster failure
        # surfacing.
        MAX_CONSECUTIVE_FAILURES = (
            self.agent.config.recv_consecutive_failures_max
        )
        BACKOFF_INITIAL_S = 0.1
        BACKOFF_MAX_S = 5.0
        consecutive_failures = 0

        while True:
            try:
                frame = await self.transport.recv()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.exception(
                    "recv_failed",
                    extra={
                        "event": "recv_failed",
                        "consecutive": consecutive_failures,
                    },
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "recv_loop_giving_up",
                        extra={
                            "event": "recv_loop_giving_up",
                            "consecutive": consecutive_failures,
                        },
                    )
                    # RAISE (not `return`): a bare return is
                    # indistinguishable from a clean stop in
                    # `run_until`'s asyncio.wait, so the process
                    # exited 0 on permanent transport death and a
                    # fleet on `Restart=on-failure` never restarted
                    # the dead agent. run_until re-raises this so
                    # `Agent.run()` exits non-zero.
                    raise TransportPermanentlyFailed(
                        f"recv loop gave up after {consecutive_failures} "
                        "consecutive failures (transport permanently "
                        "unrecoverable)"
                    ) from exc
                # Exponential backoff with cap. Don't burn CPU
                # while transport recovers / reconnects.
                wait = min(
                    BACKOFF_INITIAL_S * (2 ** (consecutive_failures - 1)),
                    BACKOFF_MAX_S,
                )
                await asyncio.sleep(wait)
                continue

            # Reset on every successful recv — only consecutive
            # failures count toward the bail threshold.
            consecutive_failures = 0
            try:
                await self._dispatch(frame)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "dispatch_failed",
                    extra={"event": "dispatch_failed", "type": frame.type},
                )

    async def _dispatch(self, frame: Frame) -> None:
        if isinstance(frame, NewTaskFrame):
            await self._handle_new_task(frame)
        elif isinstance(frame, ResultFrame):
            # Peer-call results are correlated by task_id (assigned during
            # the spawn ack). PendingMap accepts arbitrary keys.
            self.pending_results.resolve(frame.task_id, frame)
            # If a SpawnStream is iterating progress for this child task,
            # push the Result onto its queue so iteration ends cleanly.
            sub = self._progress_subscribers.pop(frame.task_id, None)
            if sub is not None:
                try:
                    sub.put_nowait(frame)
                except asyncio.QueueFull:
                    pass
            # Terminal frame — drop any pre-subscribe progress buffer
            # for this task. The spawner is either subscribed (above)
            # or is never going to subscribe (non-stream spawn path),
            # so the buffered frames are no longer reachable.
            self._pending_progress_buffer.pop(frame.task_id, None)
        elif isinstance(frame, ProgressFrame):
            await self._handle_progress(frame)
        elif isinstance(frame, CancelFrame):
            await self._handle_cancel(frame)
        elif isinstance(frame, AckFrame):
            self.pending_acks.resolve(frame.ref_correlation_id, frame)
        elif isinstance(frame, PingFrame):
            pong = PongFrame(
                agent_id=self.agent.info.agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
            )
            await self.transport.send(pong)
        elif isinstance(frame, PongFrame):
            self.pending_acks.resolve(frame.ref_correlation_id, frame)
        elif isinstance(frame, FileUploadGrantFrame):
            # Correlated response to a FileStash upload
            # negotiation — same pending_acks map peers.spawn /
            # Ack / Pong resolve on, keyed by ref_correlation_id.
            self.pending_acks.resolve(frame.ref_correlation_id, frame)
        elif isinstance(frame, FileResultFrame):
            # Correlated response to a FileStash store / fetch /
            # manage round-trip — same pending_acks map.
            self.pending_acks.resolve(frame.ref_correlation_id, frame)
        elif isinstance(frame, LlmDeltaFrame):
            await self._handle_llm_delta(frame)
        elif isinstance(frame, LlmResultFrame):
            await self._handle_llm_result(frame)
        elif isinstance(frame, CatalogUpdateFrame):
            self.transport.update_catalog(frame.available_destinations)
        elif isinstance(frame, ErrorFrame):
            logger.warning(
                "router_error_frame",
                extra={
                    "event": "router_error_frame",
                    "code": frame.code,
                    "message": frame.message,
                },
            )
        else:
            logger.warning(
                "unexpected_frame",
                extra={"event": "unexpected_frame", "type": frame.type},
            )

    # ------------------------------------------------------------------
    # NewTask → handler invocation
    # ------------------------------------------------------------------

    async def _handle_new_task(self, frame: NewTaskFrame) -> None:
        # During graceful shutdown we keep dispatching frames (so in-flight
        # handlers can finish) but refuse to ADMIT new work — otherwise a
        # task admitted at the drain boundary would be hard-cancelled at the
        # deadline before it could make progress. Reject so the caller
        # re-routes immediately instead of waiting on its spawn timeout.
        if self._draining:
            await self.transport.send(AckFrame(
                agent_id=self.agent.info.agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
                accepted=False,
                reason="agent_shutting_down",
            ))
            return

        # Acknowledge admission immediately; the handler runs in the
        # background and emits a Result frame on completion.
        ack = AckFrame(
            agent_id=self.agent.info.agent_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=True,
            task_id=frame.task_id,
        )

        # Find a handler. If we can't, reject before acking.
        handler = self._resolve_handler_for(frame)
        if handler is None:
            ack = AckFrame(
                agent_id=self.agent.info.agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
                accepted=False,
                reason="no_handler",
            )
            await self.transport.send(ack)
            return

        # Validate input. `dict`-input handlers receive the frame's
        # payload dict verbatim — the router already enforced the
        # destination's `accepts_schema` at admit time, so re-running
        # Pydantic validation here would just rebuild a copy of the
        # same dict.
        if handler.input_model is dict:
            payload = frame.payload
        else:
            # Broad except: Pydantic v2 wraps ValueError raised inside
            # `@field_validator` into a ValidationError, but a
            # misbehaving validator (programming bug, forward-ref
            # resolution failure, AttributeError on a None) raises a
            # `TypeError` / `RuntimeError` / `KeyError` straight
            # through, leaving no Ack sent. The caller's spawn future
            # then hangs to deadline. Catch everything and surface as
            # a normal Ack rejection.
            try:
                payload = handler.input_model.model_validate(frame.payload)
            except ValidationError as exc:
                reason = f"validation_error: {safe_validator_message(exc)}"
                await self._reject_new_task(frame, reason)
                return
            except Exception as exc:  # noqa: BLE001
                # Non-Pydantic surface (bug in a validator, malformed
                # input_model). Log with stack so the SDK author can
                # debug; surface as a bounded validation_error so the
                # caller's correlation future resolves promptly.
                logger.exception(
                    "input_model_unexpected_exception",
                    extra={
                        "event": "input_model_unexpected_exception",
                        "bp.task_id": frame.task_id,
                        "bp.agent_id": self.agent.info.agent_id,
                        "exc_type": type(exc).__name__,
                    },
                )
                await self._reject_new_task(
                    frame, f"validation_error: {type(exc).__name__}"
                )
                return

        # Build TaskContext + register the active-task entry BEFORE
        # acking. The ack tells the router the task is live, so the
        # router may route a Cancel straight back; if `_active` were
        # not populated until after `create_task`, a Cancel arriving
        # during the `send(ack)` await would find nothing and be
        # silently dropped (the handler would then run uncancellable).
        # Registering the cancel_token first closes that race — a
        # Cancel in the window still trips the token, which the
        # handler observes as soon as it starts.
        cancel_token = CancelToken()
        ctx = self._build_context(frame, cancel_token)
        # NewTaskFrame.task_id is None only on agent → router spawn
        # frames (router assigns the id and acks). Frames the router
        # DELIVERS to an agent always carry the assigned task_id; if
        # we somehow see one without, skip the active-tasks
        # bookkeeping rather than relying on `assert`.
        if frame.task_id is not None:
            self._active[frame.task_id] = _ActiveTask(
                task_id=frame.task_id,
                cancel_token=cancel_token,
            )

        await self.transport.send(ack)

        handler_task = asyncio.create_task(
            self._run_handler(handler, ctx, payload, frame)
        )
        if frame.task_id is None:
            logger.warning(
                "newtask_without_task_id",
                extra={"event": "newtask_without_task_id"},
            )
            return
        active = self._active.get(frame.task_id)
        if active is not None:
            active.handler_task = handler_task

    def _resolve_handler_for(self, frame: NewTaskFrame):  # type: ignore[no-untyped-def]
        """Resolve by explicit mode — `frame.input_mode`. O(1),
        order-independent, no structural payload probing.

        There are no disjoint registries any more: a control-plane
        handler is just a mode registered with `tool=False` (hidden
        from `build_tools` via `AgentInfo.non_tool_modes`, still
        routed/validated normally). Delegation is not a routing axis
        — a handler reads `ctx.delegating_agent_id` if it needs to
        branch. `input_mode is None` resolves to the sole handler
        when there's exactly one; ambiguous/unknown → None →
        `no_handler` ack.
        """
        return self.agent.resolve_handler(mode=frame.input_mode)

    async def _reject_new_task(
        self, frame: NewTaskFrame, reason: str
    ) -> None:
        """Send a `accepted=False` Ack for a NewTaskFrame and return.

        Folds the AckFrame construction shared by the handler-not-
        found path and the input-model validation path (both
        `ValidationError` and the broad-except). Centralising means
        future ack-shape changes (e.g. a structured `reason_code`
        field) update once."""
        await self.transport.send(
            AckFrame(
                agent_id=self.agent.info.agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
                accepted=False,
                reason=reason,
            )
        )


    # ------------------------------------------------------------------
    # Progress subscription accessors
    # ------------------------------------------------------------------
    #
    # SpawnStream and `peers.spawn` previously poked
    # `self._progress_subscribers` directly. Promoting the access to
    # typed methods lets the storage shape evolve (e.g. multi-consumer
    # fan-out) without rippling through every caller.

    def subscribe_progress(
        self, task_id: str, *, maxsize: int = 0
    ) -> asyncio.Queue:
        """Allocate a queue for ProgressFrames bound to `task_id` and
        register it on the dispatcher. Returns the queue the caller
        will drain via `__anext__` / `await queue.get()`.

        Drains any frames that arrived BEFORE this subscribe was
        registered (the spawn-Ack-but-not-yet-subscribed window) into
        the new queue, in arrival order. Subject to the queue's
        `maxsize` — drops with a warning past the cap, same shape as
        `_handle_progress`."""
        prior = self._progress_subscribers.get(task_id)
        if prior is not None:
            # A second subscribe for the same task_id displaces the
            # first. Pre-R9 the old queue was silently orphaned:
            # `_handle_progress` and the terminal-ResultFrame path
            # both key off the LATEST subscriber, so the first
            # `SpawnStream` would block on `queue.get()` until its
            # `result_fut` timed out (and its terminal Result would
            # be delivered to the WRONG, second consumer). Push the
            # `_STREAM_CLOSED` sentinel into the displaced queue so
            # the orphaned stream ends cleanly via
            # `StopAsyncIteration` instead of hanging.
            from bp_sdk.peers import _STREAM_CLOSED  # noqa: PLC0415

            try:
                prior.put_nowait(_STREAM_CLOSED)
            except asyncio.QueueFull:
                pass
            logger.warning(
                "progress_subscriber_displaced",
                extra={
                    "event": "progress_subscriber_displaced",
                    "bp.task_id": task_id,
                },
            )
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._progress_subscribers[task_id] = queue
        buffered = self._pending_progress_buffer.pop(task_id, None)
        if buffered:
            for frame in buffered:
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    logger.warning(
                        "progress_buffer_drain_subscriber_full",
                        extra={
                            "event": "progress_buffer_drain_subscriber_full",
                            "bp.task_id": task_id,
                        },
                    )
                    break
        return queue

    def unsubscribe_progress(self, task_id: str) -> None:
        """Pop the subscription. Idempotent."""
        self._progress_subscribers.pop(task_id, None)

    def open_spawn_stream(
        self,
        task_id: str,
        *,
        timeout_s: float | None = None,
        maxsize: int = 0,
    ) -> Any:
        """Build a `SpawnStream` for a task admitted out of band.

        Used by callers OUTSIDE any TaskContext (channel agents,
        daemons) that admit a task via an HTTP path and then want
        progress+result delivery with the usual cancellation-safe
        cleanup. The intra-handler path goes through
        `PeerClient.spawn(stream=True)` instead, which threads the
        active task_id automatically.
        """
        from bp_sdk.peers import SpawnStream  # noqa: PLC0415

        queue = self.subscribe_progress(task_id, maxsize=maxsize)
        result_fut = self.register_for_task(
            self.pending_results, task_id, None, timeout_s=timeout_s
        )
        return SpawnStream(
            task_id=task_id,
            queue=queue,
            result_fut=result_fut,
            dispatcher=self,
        )

    # ------------------------------------------------------------------
    # Per-task pending-future tracking
    # ------------------------------------------------------------------

    def register_for_task(
        self,
        pmap: PendingMap,
        correlation_id: str,
        task_id: str | None,
        *,
        timeout_s: float | None = None,
    ) -> asyncio.Future:
        """Wrap `PendingMap.register` with task ownership tracking.

        When a handler for `task_id` raises (or returns) without
        awaiting the future the SDK is about to register on its
        behalf, `_drain_task_correlations` will reject this future
        immediately rather than letting the caller wait out the
        `correlation_timeout`.

        `task_id=None` (e.g. `<spawn>` placeholder for handler-bootstrap
        paths, or LLM calls outside a handler context) bypasses the
        tracker — the future falls back to the timeout-reaper path
        like before.

        **Race window with the early-resolve buffer.** `pmap.register`
        can return an ALREADY-RESOLVED future when an early-arriving
        Result was buffered. In that path the `_untrack` callback
        scheduled below fires on `call_soon`, briefly placing the
        key in `_task_correlations[task_id]` before it's discarded
        on the next loop tick. If the handler raises during this
        tiny window, `_drain_task_correlations` would try to reject
        an already-resolved future — `pmap.reject` checks
        `cid in self._pending` and short-circuits, so the reject is
        a no-op. The race is benign by virtue of that guard; future
        refactors removing the guard would expose it. R6 third-pass
        review.
        """
        fut = pmap.register(correlation_id, timeout_s=timeout_s)
        if task_id is None or task_id == "<spawn>":
            return fut
        key = (pmap, correlation_id)
        self._task_correlations.setdefault(task_id, set()).add(key)

        # Untrack on natural resolution. `add_done_callback` runs the
        # callback on the next loop iteration if the future is already
        # done, otherwise when it's resolved/rejected. Either way the
        # tracker is cleaned promptly without polluting the resolve /
        # reject hot paths.
        def _untrack(_fut: asyncio.Future, _key: tuple = key,
                     _tid: str = task_id) -> None:
            s = self._task_correlations.get(_tid)
            if s is not None:
                s.discard(_key)
                if not s:
                    self._task_correlations.pop(_tid, None)
        fut.add_done_callback(_untrack)
        return fut

    def _drain_task_correlations(
        self, task_id: str, exc: BaseException
    ) -> int:
        """Reject every still-pending future this task registered.

        Called from `_run_handler`'s finally so a raised handler
        doesn't leave child spawns / LLM calls hanging until their
        correlation_timeout fires.

        Returns the number of futures actually rejected (those still
        pending; already-resolved ones are skipped to avoid the
        PendingMap `_buffered` stale-entry footgun).
        """
        keys = self._task_correlations.pop(task_id, None)
        if not keys:
            return 0
        rejected = 0
        for pmap, cid in keys:
            # Only reject if the entry is still pending. If `resolve`
            # already popped it, calling `reject` would push the
            # exception into `_buffered` — a stale entry no register
            # would ever consume. Check `_pending` membership first.
            if cid in pmap._pending:
                if pmap.reject(cid, exc):
                    rejected += 1
        return rejected

    def _build_context(
        self, frame: NewTaskFrame, cancel_token: CancelToken
    ) -> TaskContext:
        from bp_sdk.files import FileStash  # noqa: PLC0415
        from bp_sdk.llm import LlmServiceClient  # noqa: PLC0415
        from bp_sdk.peers import PeerClient  # noqa: PLC0415
        from bp_sdk.progress import ProgressEmitter  # noqa: PLC0415

        bound_log = logger.getChild(self.agent.info.agent_id)
        # Construct service handles. None of them block on construction;
        # actual network/IO happens on first use.
        ctx = TaskContext(
            task_id=frame.task_id or "<spawn>",
            parent_task_id=frame.parent_task_id,
            user_id=frame.user_id,
            user_level=frame.user_level or "",
            session_id=frame.session_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            deadline=frame.deadline,
            cancel_token=cancel_token,
            log=bound_log,
            progress=None,  # filled below
            files=None,     # filled below
            llm=None,       # filled below
            peers=None,     # filled below
            delegating_agent_id=frame.delegating_agent_id,
        )
        ctx.progress = ProgressEmitter(ctx, self)
        ctx.peers = PeerClient(ctx, self)
        ctx.llm = LlmServiceClient(ctx, self)
        ctx.files = FileStash(
            ctx,
            inbox_dir=Path(self.agent.config.state_dir) / "inbox" / (frame.task_id or "spawn"),
            router_url=self._http_router_url(),
            dispatcher=self,
        )
        return ctx

    def _http_router_url(self) -> str:
        # Derive http(s) base from ws(s) router_url for FileStash HTTP upload/download.
        url = self.agent.config.router_url
        if url.startswith("wss://"):
            return "https://" + url[len("wss://") :].split("/v1/")[0]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://") :].split("/v1/")[0]
        return url

    async def _run_handler(
        self,
        handler,  # type: ignore[no-untyped-def]
        ctx: TaskContext,
        payload: BaseModel,
        frame: NewTaskFrame,
    ) -> None:
        status = TaskStatus.SUCCEEDED
        status_code = 200
        output: AgentOutput | None = None
        error: dict[str, Any] | None = None
        cancelled_exc: asyncio.CancelledError | None = None

        try:
            result = await handler.fn(ctx, payload)
            # Strict mode: when the handler declared a return type via
            # type annotation, fail loudly if the actual return doesn't
            # match. The old coerce-cascade silently rewrote a wrong-
            # typed return into AgentOutput(content=str(...)), turning
            # bugs into test-time surprises. Agents that legitimately
            # rely on the cascade can either declare AgentOutput as
            # the return type or annotate -> Any.
            if (
                handler.output_model is not None
                and result is not None
                and not isinstance(result, handler.output_model)
            ):
                raise HandlerError(
                    f"handler returned {type(result).__name__}; "
                    f"declared return type {handler.output_model.__name__}"
                )
            if isinstance(result, AgentOutput):
                output = result
            elif isinstance(result, BaseModel):
                # Coerce arbitrary BaseModel returns into AgentOutput.metadata
                output = AgentOutput(metadata=result.model_dump())
            elif result is None:
                output = AgentOutput()
            else:
                output = AgentOutput(content=str(result))
        except CancellationError as exc:
            status = TaskStatus.CANCELLED
            status_code = exc.status_code
            error = {"code": "cancelled", "message": str(exc)}
        except asyncio.CancelledError as exc:
            # asyncio cancelled this handler TASK (not the typed,
            # cooperative `CancellationError` an agent raises off
            # `ctx.cancel_token`). Sources: the shutdown-drain
            # escalation in `_drain_in_flight`, a parent TaskGroup,
            # or loop teardown. `CancelledError` is a
            # `BaseException`, so the broad `except Exception` below
            # never caught it — pre-R9 the cancellation propagated
            # straight past the ResultFrame send, so the calling
            # parent's spawn future hung to `correlation_timeout`
            # with no terminal frame. Emit a terminal CANCELLED
            # Result so the parent resolves immediately, THEN
            # re-raise (the asyncio contract: never swallow a
            # CancelledError).
            status = TaskStatus.CANCELLED
            status_code = 499
            error = {"code": "cancelled", "message": "handler task cancelled"}
            cancelled_exc = exc
        except HandlerError as exc:
            status = TaskStatus.FAILED
            status_code = exc.status_code
            error = {"code": type(exc).__name__, "message": str(exc)}
        except Exception:  # noqa: BLE001
            # Catch-all for unclassified handler failures. Emit a
            # FIXED message — never `str(exc)` — because the result
            # frame flows back through the router to the calling
            # parent agent (and from there to the admin Test Task
            # UI / programmatic consumers). Exception strings often
            # leak host names, file paths, env-variable hints,
            # internal SQL fragments, etc. The full traceback stays
            # in `logger.exception` for ops investigation. Mirrors
            # the router-side fix; the symmetric SDK-side path was
            # previously missed.
            logger.exception(
                "handler_unhandled_exception",
                extra={
                    "event": "handler_unhandled_exception",
                    "bp.task_id": frame.task_id,
                },
            )
            status = TaskStatus.FAILED
            status_code = 500
            error = {"code": "InternalError", "message": "internal_error"}
        finally:
            if frame.task_id is not None:
                self._active.pop(frame.task_id, None)
                # Reject every still-pending peer-call / LLM future
                # registered while this handler ran. Without this,
                # a handler that raised before awaiting a child
                # spawn would leave the spawn's ack / result
                # futures hanging until `correlation_timeout`
                # (default 30 s). The drain short-circuits that
                # wait so callers see the failure immediately.
                # `HandlerExited` is the sentinel exception used to
                # distinguish drained futures from genuine timeouts
                # in caller logs.
                self._drain_task_correlations(
                    frame.task_id, HandlerExited(frame.task_id)
                )
            # Tear down the per-task FileStash inbox dir.
            # Without this, every task leaks its inbox tree
            # into `state_dir/inbox/<task_id>` until process exit.
            # `cleanup` is best-effort and swallows its own errors,
            # but wrap defensively so the result-frame send below
            # still happens if some future cleanup path raises.
            files = ctx.files
            if files is not None:
                try:
                    await files.cleanup()
                except asyncio.CancelledError as exc:
                    # A SECOND cancellation (loop teardown during
                    # `asyncio.run` finalization, common in the
                    # shutdown-drain path) hitting this cleanup
                    # `await` must NOT propagate out of the
                    # `finally` — that would skip the terminal
                    # CANCELLED Result send below and re-strand the
                    # parent. Capture it; the post-send `finally`
                    # re-raises so the asyncio contract is still
                    # honoured. (cleanup is best-effort anyway.)
                    if cancelled_exc is None:
                        cancelled_exc = exc
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "files_cleanup_failed",
                        extra={
                            "event": "files_cleanup_failed",
                            "bp.task_id": frame.task_id,
                        },
                        exc_info=True,
                    )

            # Stop the progress drain, symmetric with the files
            # cleanup above. Without this the single FIFO drain task
            # outlives the handler: queued-but-unsent Progress frames
            # are lost and the task lingers until loop teardown.
            # `aclose()` is bounded (best-effort flush, then cancel)
            # so a wedged transport can't stall the terminal Result
            # send below. Same CancelledError discipline as files
            # cleanup — capture, don't propagate out of the `finally`.
            progress = ctx.progress
            if progress is not None:
                try:
                    await progress.aclose()
                except asyncio.CancelledError as exc:
                    if cancelled_exc is None:
                        cancelled_exc = exc
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "progress_aclose_failed",
                        extra={
                            "event": "progress_aclose_failed",
                            "bp.task_id": frame.task_id,
                        },
                        exc_info=True,
                    )

        # `output.files` is a list of file-store NAMES that rides
        # inside the wire `output` (just strings) — no out-of-band
        # lift. The names point at the shared per-user/per-session
        # stash; the caller resolves them by name (the LLM parent via
        # `tool_response_from_result`, others via `ctx.files.read`).
        result_frame = ResultFrame(
            agent_id=self.agent.info.agent_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            task_id=frame.task_id or "",
            parent_task_id=frame.parent_task_id,
            status=status,
            status_code=status_code,
            output=output,
            error=error,
        )
        try:
            await self.transport.send(result_frame)
        finally:
            # If the handler task was asyncio-cancelled, honour the
            # cancellation AFTER best-effort delivery of the terminal
            # CANCELLED Result. The `finally` guarantees we re-raise
            # even if `transport.send` itself fails during teardown.
            if cancelled_exc is not None:
                raise cancelled_exc

    async def _handle_progress(self, frame: ProgressFrame) -> None:
        # Forward to a SpawnStream iterating this child task, if any.
        # Best-effort: a slow consumer drops progress rather than stalling
        # the recv loop.
        queue = self._progress_subscribers.get(frame.task_id)
        if queue is None:
            # No progress subscriber. If we're already awaiting this task's
            # RESULT, this is a wait-only `spawn(stream=False)` (e.g. a
            # subagent tool call): the caller explicitly opted out of
            # progress, so DROP it rather than buffer frames nobody will
            # drain. A chatty subagent (research / web-search) otherwise
            # floods `progress_buffer_per_task_cap`. The pre-subscribe
            # buffer below stays for the streamed-spawn race, where
            # `subscribe_progress` lands just after the ack.
            if frame.task_id in self.pending_results:
                return
            # Else the spawn-Ack-but-not-yet-subscribed window (or a
            # truly-orphan task_id) — buffer up to a per-task cap so the
            # eventual `subscribe_progress` can drain, bounded so an orphan
            # can't grow forever.
            self._buffer_pending_progress(frame)
            return
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning(
                "progress_subscriber_full",
                extra={
                    "event": "progress_subscriber_full",
                    "bp.task_id": frame.task_id,
                },
            )

    def _buffer_pending_progress(self, frame: ProgressFrame) -> None:
        """Buffer a ProgressFrame whose subscriber isn't registered yet.

        Bounded: per-task cap + total-task cap. Caps are class-level
        so deployments with unusual workloads can subclass / monkey-
        patch without re-wiring `_handle_progress`."""
        task_id = frame.task_id
        existing = self._pending_progress_buffer.get(task_id)
        if existing is None:
            if (
                len(self._pending_progress_buffer)
                >= self._PROGRESS_BUFFER_MAX_TASKS
            ):
                # Pre-R9 a new task_id was DROPPED here, so a burst
                # of distinct never-subscribed task_ids (many
                # fire-and-forget spawns, or a misbehaving/adversarial
                # router emitting Progress for task_ids this agent
                # never spawned) pinned all 256 slots until each
                # task's unrelated Result happened to land — starving
                # every legitimate to-be-subscribed task in the
                # meantime. Evict the OLDEST buffered task_id instead
                # (dict insertion order == first-buffered order, so
                # this is FIFO with zero extra bookkeeping). An
                # orphan can now occupy a slot only until
                # `_PROGRESS_BUFFER_MAX_TASKS` newer task_ids appear,
                # and the most-recent task_ids (likeliest to be about
                # to `subscribe_progress`) keep priority.
                oldest_tid = next(iter(self._pending_progress_buffer))
                self._pending_progress_buffer.pop(oldest_tid, None)
                logger.warning(
                    "progress_buffer_task_cap_evicted_oldest",
                    extra={
                        "event": "progress_buffer_task_cap_evicted_oldest",
                        "bp.task_id": task_id,
                        "evicted_task_id": oldest_tid,
                        "cap": self._PROGRESS_BUFFER_MAX_TASKS,
                    },
                )
            self._pending_progress_buffer[task_id] = [frame]
            return
        if len(existing) >= self._PROGRESS_BUFFER_PER_TASK:
            logger.warning(
                "progress_buffer_per_task_cap",
                extra={
                    "event": "progress_buffer_per_task_cap",
                    "bp.task_id": task_id,
                    "cap": self._PROGRESS_BUFFER_PER_TASK,
                },
            )
            return
        existing.append(frame)

    async def _handle_cancel(self, frame: CancelFrame) -> None:
        # CancelFrame.task_id is Optional — the LLM-call abort variant
        # carries ref_correlation_id instead, but the router cancels
        # those router-side and does not forward a frame to the agent.
        # Guard explicitly so the dict.get(None) lookup is intentional.
        if frame.task_id is None:
            logger.debug(
                "cancel_without_task_id",
                extra={"event": "cancel_without_task_id"},
            )
            return
        active = self._active.get(frame.task_id)
        if active is not None:
            active.cancel_token.trip(frame.reason)

    # ------------------------------------------------------------------
    # LLM responses
    # ------------------------------------------------------------------

    async def _handle_llm_delta(self, frame: LlmDeltaFrame) -> None:
        from bp_sdk.llm import _frame_delta_to_delta  # noqa: PLC0415

        queue = self._llm_streams.get(frame.ref_correlation_id)
        if queue is None:
            return  # late delta after the iterator was abandoned
        # Non-blocking: a full queue means the consumer isn't
        # draining (handler broke out of the `async for` but the
        # stream generator's `finally` hasn't popped this entry
        # yet). Pre-R9 this was `await queue.put(...)` on an
        # UNBOUNDED queue, so an abandoned stream grew memory one
        # delta per inbound frame for the rest of the recv loop.
        # Drop + warn — same best-effort tradeoff the Progress path
        # makes. The stream generator's `finally` will send the
        # abort CancelFrame so the router stops producing.
        try:
            queue.put_nowait(_frame_delta_to_delta(frame))
        except asyncio.QueueFull:
            logger.warning(
                "llm_stream_consumer_full",
                extra={
                    "event": "llm_stream_consumer_full",
                    "bp.correlation_id": frame.ref_correlation_id,
                },
            )

    async def _handle_llm_result(self, frame: LlmResultFrame) -> None:
        # Streaming case: terminate the iterator queue. The TERMINAL
        # frame must NEVER be drop-on-full (unlike a delta): the
        # consumer-side get (`LlmService._queue_get_or_cancel`) has
        # no timeout and the router sends exactly ONE terminator, so
        # a dropped terminator strands a slow-but-ALIVE consumer
        # forever (it drains its backlog, then blocks on an empty
        # queue with no end-of-stream signal). Never block the recv
        # loop either (an abandoned stream's queue stays full — its
        # generator `finally` owns teardown + the abort CancelFrame).
        # Resolution: evict oldest items until the terminator fits.
        # A live consumer loses at most a little trailing *content*;
        # it always gets the end-of-stream. Single-producer (the
        # serialized recv loop) so this terminates after ≤ maxsize
        # evictions.
        queue = self._llm_streams.get(frame.ref_correlation_id)
        if queue is not None:
            evicted = 0
            while True:
                try:
                    queue.put_nowait(frame)
                    break
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                        evicted += 1
                    except asyncio.QueueEmpty:
                        # Consumer drained concurrently; retry fits.
                        continue
            if evicted:
                logger.warning(
                    "llm_stream_terminator_evicted_deltas",
                    extra={
                        "event": "llm_stream_terminator_evicted_deltas",
                        "bp.correlation_id": frame.ref_correlation_id,
                        "evicted": evicted,
                    },
                )
            return
        # Non-streaming case: resolve the pending future.
        self.pending_results.resolve(frame.ref_correlation_id, frame)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_dispatcher(agent: Agent, transport: Transport) -> Dispatcher:
    return Dispatcher(agent, transport)
