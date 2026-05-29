"""bp_sdk.peers — How a handler invokes other agents."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel

from bp_protocol.frames import (
    AckFrame,
    NewTaskFrame,
    ProgressFrame,
    ResultFrame,
)
from bp_protocol.types import AgentInfo, TaskPriority

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher

logger = logging.getLogger(__name__)


# Sentinel pushed into the per-stream queue by `aclose()` so any
# consumer parked on `await queue.get()` wakes and exits its iteration.
# Without it, an aclose during a `async for` would just cancel the
# result-future, leaving the consumer hung on the queue until a stray
# frame arrived — which may never happen.
_STREAM_CLOSED: Final = object()


class PeerCallError(Exception):
    """Base class for spawn/delegate failures before terminal Result.

    Subclassed so callers can branch on the specific failure shape
    instead of string-matching `.args[0]`. Catching `PeerCallError`
    still works as the broad backstop.
    """


class SpawnRejected(PeerCallError):
    """Router refused the spawn at admit time (ACL, schema mismatch,
    quota exceeded, unknown destination, etc.)."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class AckTimeout(PeerCallError):
    """No ack within the correlation timeout. Transport- or router-
    level — the destination may not even have seen the request."""


class ResultTimeout(PeerCallError):
    """Spawn was accepted but no Result arrived within timeout_s.
    The child may still be running; the caller has abandoned waiting.
    """

    def __init__(self, message: str, *, task_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id


class UnexpectedResponse(PeerCallError):
    """Protocol surprise from the router (e.g. accepted spawn that
    didn't carry a task_id). Indicates a router-side bug, not a
    caller error."""



class SpawnStream:
    """Handle returned by `peers.spawn(..., stream=True)`.

    Async-iterates the child task's `ProgressFrame`s as they arrive and
    exposes the terminal `ResultFrame` via `result()`. Iteration ends
    when the result lands; calling `result()` afterwards returns the
    cached frame without awaiting again.

    **Cleanup contract**. The dispatcher
    keeps `_progress_subscribers[task_id] -> queue` mapping for the
    lifetime of the stream so it can route incoming `ProgressFrame`s.
    Normal completion (result arrives via `__anext__` or `result()`)
    pops the entry. But three abnormal exits used to leak it:

      - Caller breaks out of `async for ... in stream:` early.
      - `result(timeout_s=N)` times out before the result lands.
      - Caller drops the stream without iterating at all.

    The leak persisted forever — every Progress frame for that
    task_id then queued into a dict entry no consumer would ever
    drain. This class now exposes `aclose()` and an
    `__aenter__/__aexit__` pair so callers (and a `__del__` finalizer
    safety net) always pop the subscription, even on abandonment.
    """

    def __init__(
        self,
        *,
        task_id: str,
        queue: asyncio.Queue,
        result_fut: asyncio.Future,
        dispatcher: Dispatcher,
    ) -> None:
        self.task_id = task_id
        self._queue = queue
        self._result_fut = result_fut
        self._dispatcher = dispatcher
        self._result: ResultFrame | None = None
        self._closed = False

    def __aiter__(self) -> SpawnStream:
        return self

    async def __anext__(self) -> ProgressFrame:
        if self._result is not None:
            # Result already received — clean up subscription on the
            # natural-end iteration path so the dispatcher's
            # `_progress_subscribers[task_id]` doesn't linger.
            self._unsubscribe()
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _STREAM_CLOSED:
            # aclose() pushed the sentinel; exit the iteration.
            raise StopAsyncIteration
        if isinstance(item, ResultFrame):
            self._result = item
            self._unsubscribe()
            raise StopAsyncIteration
        return item

    async def __aenter__(self) -> SpawnStream:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the dispatcher subscription and abort any pending
        result-future await. Idempotent — safe to call multiple times.

        Use `async with stream:` (or call this from a `finally`) when
        you might break out of iteration early or stop awaiting
        `result()` before it lands. Without it, `_progress_subscribers`
        leaks the queue entry forever.
        """
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        # Wake any consumer parked on `await self._queue.get()` (inside
        # `async for ... in stream:`) so it sees end-of-stream instead
        # of hanging forever waiting for a frame that won't arrive.
        # QueueFull is non-fatal — if the queue is already full the
        # consumer will see the next get() return a real item and then
        # the sentinel after.
        try:
            self._queue.put_nowait(_STREAM_CLOSED)
        except asyncio.QueueFull:
            pass
        # Cancel the pending-results future if still awaiting. The
        # dispatcher's resolve path is a no-op on already-completed
        # futures, so a late-arriving Result is safely discarded.
        if not self._result_fut.done():
            self._result_fut.cancel()

    def _unsubscribe(self) -> None:
        """Pop our progress-subscription entry. Idempotent."""
        self._dispatcher.unsubscribe_progress(self.task_id)

    def __del__(self) -> None:
        # Safety net for callers who drop the stream without ever
        # iterating or calling aclose. Pops the dict entry directly
        # — no async work needed because this only mutates a Python
        # dict. The result-future cancel skipped here since we can't
        # await; agents that need that guarantee should use
        # `async with`.
        try:
            self._unsubscribe()
        except Exception:  # noqa: BLE001
            pass

    async def result(self, *, timeout_s: float | None = None) -> ResultFrame:
        """Return the child's `ResultFrame`. Awaits if not yet delivered.

        `timeout_s=None` does NOT mean "wait forever". The streaming
        result future is shielded from the correlation reaper (the
        stream manages its own lifecycle via `aclose`), so an
        unbounded wait here would hang indefinitely on a silent /
        zombie child. `None` therefore falls back to the dispatcher's
        configured `pending_results` timeout — the same bound a
        non-streaming `spawn(wait=True)` gets from the reaper. Pass an
        explicit `timeout_s` to override.
        """
        if self._result is not None:
            return self._result
        effective_timeout = (
            timeout_s
            if timeout_s is not None
            else self._dispatcher.pending_results.default_timeout_s
        )
        try:
            self._result = await asyncio.wait_for(
                asyncio.shield(self._result_fut), timeout=effective_timeout
            )
        except TimeoutError as exc:
            # Timing out means the agent abandoned waiting; release
            # the subscription so the dispatcher doesn't keep
            # queueing into a dead consumer.
            self._unsubscribe()
            raise ResultTimeout(
                f"spawn result timeout for task {self.task_id}",
                task_id=self.task_id,
            ) from exc
        # Natural success — also unsubscribe in case the caller never
        # iterates progress.
        self._unsubscribe()
        return self._result


class PeerClient:
    """Per-task helper bound to one TaskContext."""

    def __init__(self, ctx: TaskContext, dispatcher: Dispatcher) -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------
    # Spawn / delegate
    # ------------------------------------------------------------------

    async def spawn(
        self,
        destination_agent_id: str,
        payload: BaseModel | dict[str, Any],
        *,
        wait: bool = True,
        stream: bool = False,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        mode: str | None = None,
    ) -> ResultFrame | SpawnStream | str:
        """Create a child task.

        Files are NOT passed here. A child reaches files by NAME in
        the shared per-user/per-session file store: the parent
        `ctx.files.store(...)`s them (or they already exist), then
        names them in `payload`; the child reads them with
        `ctx.files.read(name)` / references them with
        `ctx.files.llm_ref(name)`.

        `payload` may be either a Pydantic `BaseModel` instance OR a
        plain `dict[str, Any]`. The two forms are wire-equivalent:

          * BaseModel: `model_dump()` is called and the result becomes
            the frame payload. SDK-side Pydantic validation runs at
            construction time (caller code raises `ValidationError`
            on bad input).
          * dict: passed through as-is. No SDK-side validation. The
            router still validates against the destination's
            `accepts_schema[mode]` at admit time, so a malformed
            payload is rejected before any task row is created.

        `mode` selects which of the destination's registered modes
        this payload targets (the router validates against that
        mode's schema; the consumer dispatches to that handler).
        `None` ⇒ the destination's sole mode — fine for the common
        single-handler agent; REQUIRED for a multi-mode destination
        (omitting it there is rejected as `no_handler`, never
        silently mis-routed). For LLM-driven calls
        `spawn_from_tool_call` fills `mode` from the per-mode tool
        name automatically.

        The dict form exists for the LLM-tool-call dispatch loop
        where the parent agent doesn't have the destination's payload
        class on hand — only the args dict the model returned. See
        also `peers.spawn_from_tool_call`.

        Returns:
          * `wait=True, stream=False` (default): the child's `ResultFrame`.
          * `wait=True, stream=True`: a `SpawnStream` yielding child
            `ProgressFrame`s; await `.result()` for the terminal frame.
          * `wait=False`: the assigned `task_id`, immediately —
            fire-and-forget. The child runs detached: the parent is
            NOT parked or joined against it, and the parent task
            terminates on its own handler return, independent of the
            child. (Only `wait=True` makes the parent "wait": the
            handler blocks on the child's `ResultFrame`, so the parent
            task stays `RUNNING` for the child's duration. No
            `WAITING_CHILDREN` transition occurs — that state is
            reserved and not currently driven by any code path.)

        `priority` controls the admit-queue ordering. Defaults to
        `NORMAL`. Background workloads (memory consolidation,
        summarization, scheduled jobs) should request `LOW` so they
        sit behind interactive traffic; user-facing latency-sensitive
        spawns can request `HIGH`.
        """
        if stream and not wait:
            raise ValueError("stream=True requires wait=True (use SpawnStream.result())")

        payload_dict = (
            payload.model_dump() if isinstance(payload, BaseModel) else payload
        )

        frame = NewTaskFrame(
            agent_id=self._dispatcher.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=None,  # spawn
            parent_task_id=self._ctx.task_id,
            destination_agent_id=destination_agent_id,
            user_id=self._ctx.user_id,
            session_id=self._ctx.session_id,
            priority=priority,
            idempotency_key=idempotency_key,
            input_mode=mode,
            payload=payload_dict,
        )

        # Register a pending ack so we can pick up the assigned
        # task_id. Routed through `register_for_task` so the
        # ack-future is rejected immediately if THIS handler exits
        # before the ack arrives — otherwise the caller would wait
        # out `correlation_timeout`.
        ack_fut = self._dispatcher.register_for_task(
            self._dispatcher.pending_acks,
            frame.correlation_id,
            self._ctx.task_id,
        )
        await self._dispatcher.transport.send(frame)
        try:
            ack = await ack_fut
        except TimeoutError as exc:
            raise AckTimeout("spawn ack timed out") from exc
        if not isinstance(ack, AckFrame) or not ack.accepted:
            reason = ack.reason if isinstance(ack, AckFrame) else "unknown"
            raise SpawnRejected(f"router rejected spawn: {reason}", reason=reason)
        if ack.task_id is None:
            raise UnexpectedResponse(
                "router accepted spawn but did not assign task_id"
            )

        if not wait:
            return ack.task_id

        if stream:
            # Subscribe BEFORE the first ProgressFrame can race in. The
            # dispatcher pushes ProgressFrames here as they arrive and
            # finally pushes the ResultFrame so iteration ends.
            progress_queue = self._dispatcher.subscribe_progress(ack.task_id)
            result_fut = self._dispatcher.register_for_task(
                self._dispatcher.pending_results,
                ack.task_id,
                self._ctx.task_id,
                timeout_s=timeout_s,
            )
            return SpawnStream(
                task_id=ack.task_id,
                queue=progress_queue,
                result_fut=result_fut,
                dispatcher=self._dispatcher,
            )

        # Register a pending Result keyed on the assigned task_id.
        result_fut = self._dispatcher.register_for_task(
            self._dispatcher.pending_results,
            ack.task_id,
            self._ctx.task_id,
            timeout_s=timeout_s,
        )
        try:
            result = await result_fut
        except TimeoutError as exc:
            raise ResultTimeout(
                f"spawn result timeout for task {ack.task_id}",
                task_id=ack.task_id,
            ) from exc
        if not isinstance(result, ResultFrame):
            raise UnexpectedResponse("unexpected response while awaiting Result")
        return result

    async def spawn_from_tool_call(
        self,
        tool_call: Any,
        *,
        wait: bool = True,
        stream: bool = False,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> ResultFrame | SpawnStream | str:
        """Spawn from an LLM-emitted `ToolCall` (`bp_sdk.llm.ToolCall`).

        Resolves `tool_call.name` back to `(agent_id, mode)` via the
        SAME flattening `build_tools` used (`tools.resolve_tool_name`
        against the cached catalog) — so a per-mode tool
        (`call_<agent>_<mode>`) dispatches to the right mode with no
        fragile string parsing. Collapses the agent-loop boilerplate

            for tc in resp.tool_calls:
                result = await ctx.peers.spawn_from_tool_call(tc)

        `tool_call` is typed `Any` to dodge a circular import with
        `bp_sdk.llm.ToolCall` — the helper duck-types `.name` and
        `.args`.

        Raises `ValueError` if the name isn't a `call_…` tool. If the
        name carries the prefix but isn't in the current catalog
        (hidden / uncatalogued / single-mode legacy), it falls back
        to `agent_id = name.removeprefix("call_")`, `mode=None` — the
        router/consumer then resolve the sole mode.
        """
        from bp_sdk.tools import resolve_tool_name  # noqa: PLC0415

        name = getattr(tool_call, "name", None)
        if not isinstance(name, str) or not name.startswith("call_"):
            raise ValueError(
                f"tool_call.name {name!r} does not match the framework "
                "convention 'call_<agent_id>[_<mode>]' — only tools "
                "published by build_tools() are dispatchable via "
                "spawn_from_tool_call"
            )
        resolved = resolve_tool_name(self.visible(), name)
        if resolved is not None:
            agent_id, mode = resolved
        else:
            agent_id, mode = name.removeprefix("call_"), None
        args = getattr(tool_call, "args", {}) or {}
        return await self.spawn(
            agent_id,
            args,
            wait=wait,
            stream=stream,
            timeout_s=timeout_s,
            idempotency_key=idempotency_key,
            priority=priority,
            mode=mode,
        )

    async def delegate(
        self,
        destination_agent_id: str,
        payload: BaseModel | dict[str, Any],
        *,
        priority: TaskPriority = TaskPriority.NORMAL,
        mode: str | None = None,
    ) -> None:
        """Hand off the current task. The current handler should return
        immediately afterwards — the delegated agent will terminate the
        task with the parent's task_id.

        `payload` accepts either a Pydantic `BaseModel` or a plain
        `dict[str, Any]`. See `peers.spawn` for the full validation
        story; the only difference here is that delegations preserve
        the parent's `task_id` instead of getting a fresh one.

        `priority` propagates through the delegation; the router uses
        it for admit-queue ordering at the destination.

        `mode` selects the destination's registered mode (see
        `peers.spawn`). The destination handler reads
        `ctx.delegating_agent_id` if it needs delegation-aware
        behaviour — there is no separate delegation handler.
        """
        payload_dict = (
            payload.model_dump() if isinstance(payload, BaseModel) else dict(payload)
        )
        frame = NewTaskFrame(
            agent_id=self._dispatcher.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=self._ctx.task_id,  # delegate preserves task_id
            parent_task_id=self._ctx.parent_task_id,
            destination_agent_id=destination_agent_id,
            user_id=self._ctx.user_id,
            session_id=self._ctx.session_id,
            priority=priority,
            input_mode=mode,
            payload=payload_dict,
        )
        ack_fut = self._dispatcher.register_for_task(
            self._dispatcher.pending_acks,
            frame.correlation_id,
            self._ctx.task_id,
        )
        await self._dispatcher.transport.send(frame)
        try:
            ack = await ack_fut
        except TimeoutError as exc:
            raise AckTimeout("delegate ack timed out") from exc
        if not isinstance(ack, AckFrame) or not ack.accepted:
            reason = ack.reason if isinstance(ack, AckFrame) else "unknown"
            raise SpawnRejected(
                f"router rejected delegate: {reason}", reason=reason
            )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def visible(self, *, for_user_level: str | None = None) -> dict[str, dict[str, Any]]:
        """Return the catalog from the last Welcome frame, filtered by
        the caller's tier.

        If `for_user_level` is set, that level is used. Otherwise the
        active task's `user_level` is used. When NEITHER resolves to
        a non-empty string, `visible()` is default-CLOSED — returns
        an empty dict. Service-account / system-initiated tasks have
        no user level by construction and previously saw every agent
        in the catalog; that's a privacy + tier-gating leak. Admin
        tooling that legitimately wants the full catalog should pass
        `for_user_level="admin"` (or whatever level genuinely
        satisfies the gates).
        """
        welcome = getattr(self._dispatcher.transport, "welcome", None)
        catalog: dict[str, dict[str, Any]] = (
            welcome.available_destinations if welcome is not None else {}
        )
        level = for_user_level if for_user_level is not None else self._ctx.user_level
        if not level:
            return {}
        return {
            agent_id: entry
            for agent_id, entry in catalog.items()
            if level in entry.get("callable_user_levels", [])
        }

    async def find(self, capability: str) -> list[AgentInfo]:
        """Visible agents that provide `capability` and are callable at
        the active task's user level."""
        out: list[AgentInfo] = []
        for agent_id, entry in self.visible().items():
            if capability in entry.get("capabilities", []):
                out.append(_entry_to_info(agent_id, entry))
        return out

    async def describe(self, agent_id: str) -> AgentInfo:
        """Full AgentInfo for a destination from the cached catalog."""
        entry = self.visible().get(agent_id)
        if entry is None:
            raise PeerCallError(f"agent {agent_id!r} not visible")
        return _entry_to_info(agent_id, entry)

    async def update_agent_info(
        self,
        *,
        description: str | None = None,
        groups: list[str] | None = None,
        capabilities: list[str] | None = None,
        accepts_schema: dict[str, Any] | None = None,
        produces_schema: dict[str, Any] | None = None,
        non_tool_modes: list[str] | None = None,
        hidden: bool | None = None,
        documentation_url: str | None = None,
    ) -> None:
        """Patch-update this agent's published AgentInfo (Phase 10e).

        Thin wrapper over `Agent.update_info` — kept on `PeerClient`
        for ergonomic in-handler use (`await ctx.peers.update_agent_info(...)`).
        The actual frame/ack logic lives on the agent so callers
        outside a task context (e.g. MCP-bridge tools/list_changed
        listener) can use the same machinery.

        PATCH semantics: `None` means "don't touch". Raises
        `PeerCallError` on router rejection, `AckTimeout` on
        timeout, `ValueError` on empty patch.
        """
        await self._dispatcher.agent.update_info(
            description=description,
            groups=groups,
            capabilities=capabilities,
            accepts_schema=accepts_schema,
            produces_schema=produces_schema,
            non_tool_modes=non_tool_modes,
            hidden=hidden,
            documentation_url=documentation_url,
        )


def _entry_to_info(agent_id: str, entry: dict[str, Any]) -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        description=entry.get("description", ""),
        groups=entry.get("groups", []),
        capabilities=entry.get("capabilities", []),
        accepts_schema=entry.get("accepts_schema"),
        non_tool_modes=entry.get("non_tool_modes", []),
        documentation_url=entry.get("documentation_url"),
        hidden=entry.get("hidden", False),
    )
