"""bp_sdk.agent — The Agent class that agent authors instantiate."""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import (
    Any,
    TypeVar,
)

from pydantic import BaseModel

from bp_protocol.types import AgentInfo
from bp_sdk.context import TaskContext
from bp_sdk.errors import HandlerError, TransportPermanentlyFailed
from bp_sdk.settings import AgentConfig, load_agent_config

logger = logging.getLogger(__name__)


T_in = TypeVar("T_in", bound=BaseModel)
T_out = TypeVar("T_out", bound=BaseModel)
HandlerFn = Callable[[TaskContext, Any], Awaitable[Any]]


@dataclass
class _RegisteredHandler:
    fn: HandlerFn
    mode: str
    input_model: Any  # type[BaseModel] | the bare `dict` escape hatch
    output_model: type[BaseModel] | None = None
    tool: bool = True  # False => excluded from build_tools (control-plane)
    description: str | None = None  # per-mode tool description (build_tools)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Top-level entry point for an agent process.

    Usage:

        agent = Agent(info=AgentInfo(...))

        @agent.handler
        async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput: ...

        agent.run()                # external (blocks)
        await agent.run_async()    # embedded (non-blocking)
    """

    def __init__(
        self,
        info: AgentInfo,
        *,
        config: AgentConfig | None = None,
    ) -> None:
        self.info = info
        self.config = config or load_agent_config()
        # Snapshot fields the operator explicitly set on AgentInfo at
        # construction time, BEFORE any decorator runs. Pydantic v2's
        # `info.model_fields_set` is live — direct `setattr` calls
        # (including our own auto-publish step) mutate it — so we
        # can't use it as a "did the operator pin this?" oracle
        # after the fact. Freezing the set here keeps the operator's
        # intent stable across the decorator passes.
        self._operator_pinned_schema_fields: frozenset[str] = frozenset(
            info.model_fields_set
        )

        # ONE unified registry, keyed by explicit mode name. Routing
        # is an O(1) mode lookup — no structural first-match, no
        # registration-order dependence. Control-plane handlers live
        # here too (registered with `tool=False`, which lists their
        # mode in `AgentInfo.non_tool_modes`); delegation is not a
        # separate path — a handler reads `ctx.delegating_agent_id`
        # if it cares. Insertion-ordered for stable schema output.
        self._handlers_by_mode: dict[str, _RegisteredHandler] = {}
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._shutdown_hooks: list[Callable[[], Awaitable[None]]] = []

        # Filled on connect by the dispatch module.
        self._dispatcher: Any | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _make_registered(
        self, fn: HandlerFn, *, mode: str | None, tool: bool,
        description: str | None = None,
    ) -> _RegisteredHandler:
        """Introspect a handler and build the `_RegisteredHandler` record.

        The `mode` is the explicit routing key. Default (when the
        decorator didn't pass one): the payload model's class name —
        unambiguous for the common single-handler agent, and stable
        as long as the model class isn't renamed. A `dict`-input
        handler has no model name, so `mode=` is REQUIRED for it.

        Resolves PEP 563 string annotations via `typing.get_type_hints`
        so handler modules can use `from __future__ import annotations`
        (the project's house style). Without that resolution,
        `inspect.signature(fn).parameters[1].annotation` returns the
        BARE STRING `"LLMData"` and the BaseModel subclass check
        fails — every external agent module that follows the project
        style guide would crash at import time.
        """
        import typing  # noqa: PLC0415

        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"{fn.__qualname__} must be async — embedded agents in particular "
                "rely on this for event-loop fairness."
            )
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if len(params) < 2:
            raise TypeError("handler must take (ctx, payload) — saw fewer params")

        try:
            hints = typing.get_type_hints(fn, include_extras=False)
        except NameError as exc:
            raise TypeError(
                f"{fn.__qualname__}: cannot resolve type annotation "
                f"({exc}). Forward references must be importable from "
                "the handler module's namespace at decoration time."
            ) from exc

        input_param_name = params[1].name
        input_model = hints.get(input_param_name, params[1].annotation)
        # `payload: dict` is allowed as a special escape hatch for
        # forwarding agents (MCP bridge, generic relay shims) that
        # don't have a typed payload model at decoration time.
        # The router still validates the inbound payload against
        # `accepts_schema` at admit, so dict-input doesn't relax
        # the security boundary — it just turns off SDK-side
        # Pydantic re-validation. Parameterised forms like
        # `dict[str, Any]` are NOT accepted; use the bare `dict`
        # type to opt in deliberately.
        if input_model is not dict and not (
            isinstance(input_model, type) and issubclass(input_model, BaseModel)
        ):
            raise TypeError(
                f"{fn.__qualname__} payload type must be `dict` or a Pydantic "
                f"BaseModel subclass; got {input_model!r}"
            )

        output_model = hints.get("return", sig.return_annotation)
        if output_model is sig.empty:
            output_model = None
        resolved_output = (
            output_model
            if isinstance(output_model, type) and issubclass(output_model, BaseModel)
            else None
        )

        if mode is None:
            if input_model is dict:
                raise TypeError(
                    f"{fn.__qualname__}: a `dict`-input handler has no "
                    "payload model to derive a mode name from — pass an "
                    "explicit @handler(mode=...)"
                )
            mode = input_model.__name__
        if not isinstance(mode, str) or not mode:
            raise TypeError(
                f"{fn.__qualname__}: handler mode must be a non-empty str"
            )
        return _RegisteredHandler(
            fn=fn,
            mode=mode,
            input_model=input_model,
            output_model=resolved_output,
            tool=tool,
            description=description,
        )

    @staticmethod
    def _strict_schema(model: type[BaseModel]) -> dict[str, Any]:
        """Published per-mode schema, with unknown top-level keys
        forbidden. The router validates `payload` against this; with
        explicit mode routing there is no `oneOf`, so tightening
        `additionalProperties` makes a typo'd / mis-shaped payload a
        clean `schema_mismatch` at admit instead of silently
        validating. Pydantic `model_validate` on the consumer stays
        lenient — this hardens only the PUBLISHED contract, not
        runtime model parsing."""
        s = model.model_json_schema()
        if s.get("type") == "object" and "additionalProperties" not in s:
            s["additionalProperties"] = False
        return s

    def _republish_schemas(self) -> None:
        """Recompute the auto-published `accepts_schema` (per-mode
        map), `non_tool_modes`, and `produces_schema` from the
        unified registry. Operator-pinned fields (set on
        `AgentInfo(...)` at construction, captured in
        `_operator_pinned_schema_fields`) are never overwritten.
        """
        regs = list(self._handlers_by_mode.values())

        if "accepts_schema" not in self._operator_pinned_schema_fields:
            # {mode: schema|None}. None == dict-input mode: the router
            # admits without payload validation (explicit, not a
            # silent gap). Empty registry → None (clear).
            if regs:
                self.info.accepts_schema = {
                    r.mode: (
                        None
                        if r.input_model is dict
                        else self._strict_schema(r.input_model)
                    )
                    for r in regs
                }
            else:
                self.info.accepts_schema = None

        if "non_tool_modes" not in self._operator_pinned_schema_fields:
            # Insertion order preserved (registry is ordered).
            self.info.non_tool_modes = [
                r.mode for r in regs if not r.tool
            ]

        if "mode_descriptions" not in self._operator_pinned_schema_fields:
            # Per-mode tool descriptions (build_tools prefers these over the
            # agent-level description). None when no handler supplied one.
            descs = {r.mode: r.description for r in regs if r.description}
            self.info.mode_descriptions = descs or None

        if "produces_schema" not in self._operator_pinned_schema_fields:
            outs = list(dict.fromkeys(
                r.output_model for r in regs if r.output_model is not None
            ))
            if not outs:
                self.info.produces_schema = None
            elif len(outs) == 1:
                self.info.produces_schema = outs[0].model_json_schema()
            else:
                self.info.produces_schema = {
                    "oneOf": [m.model_json_schema() for m in outs]
                }

    def handler(
        self,
        fn: HandlerFn | None = None,
        *,
        mode: str | None = None,
        tool: bool = True,
        description: str | None = None,
    ) -> HandlerFn | Callable[[HandlerFn], HandlerFn]:
        """Register `fn` as a handler. Usable bare (`@agent.handler`)
        or parameterised (`@agent.handler(mode="…", tool=False)`).

        Routing is by explicit MODE, not payload shape. `mode`
        defaults to the payload model's class name — unambiguous for
        the common single-handler agent and stable unless the class
        is renamed. Pass `mode=` to decouple the wire/tool name from
        the Python identifier, or when two handlers would otherwise
        derive the same default (a duplicate mode raises at
        registration — fail fast, not silent shadowing).

        `tool=False` registers a control-plane handler: still a
        normal mode the router validates and dispatches, but its
        mode is listed in `AgentInfo.non_tool_modes` so
        `build_tools` never advertises it to tool-using models.
        There is NO separate control/delegation registry — a handler
        reads `ctx.delegating_agent_id` if it needs delegation-aware
        behaviour.

        `description` (optional) is published as this mode's per-mode
        tool description (`AgentInfo.mode_descriptions[mode]`); the
        calling LLM sees it on `call_<agent>[_<mode>]` instead of the
        agent-level `description`. Use it on multi-mode agents to
        distinguish each tool.

        Auto-publishes `accepts_schema` (`{mode: schema|null}`),
        `non_tool_modes`, `mode_descriptions`, and `produces_schema`
        unless the operator pinned them on `AgentInfo(...)`.
        """
        def _register(f: HandlerFn) -> HandlerFn:
            registered = self._make_registered(
                f, mode=mode, tool=tool, description=description
            )
            if registered.mode in self._handlers_by_mode:
                raise TypeError(
                    f"duplicate handler mode {registered.mode!r} "
                    f"({f.__qualname__}); pass an explicit "
                    "@handler(mode=...) to disambiguate"
                )
            self._handlers_by_mode[registered.mode] = registered
            self._republish_schemas()
            return f

        return _register if fn is None else _register(fn)

    async def set_modes(
        self,
        modes: Mapping[str, tuple[HandlerFn, dict[str, Any] | None]],
        *,
        non_tool_modes: Sequence[str] | None = None,
    ) -> None:
        """Atomically REPLACE the agent's mode set and push the new
        `accepts_schema` / `non_tool_modes` to the router.

        For runtime consumers that need to add / remove / update
        modes after `run_async()` is in flight — the MCP bridge is
        the canonical case (`tools/list_changed` reshapes the per-
        server agent's mode set). Typical authors use `@agent.handler`
        at module load.

        `modes[name] = (handler_fn, accepts_schema_or_None)`:
          * `handler_fn` is `async def(ctx, payload: dict) -> ...`.
            Payload is `dict` here — the router validates it against
            the per-mode schema before dispatch, so the SDK-side
            Pydantic re-validation is unnecessary.
          * `accepts_schema_or_None` is the JSON schema published for
            this mode, or `None` to mean "no payload validation at
            the router" (matches the dict-input semantics of
            `_republish_schemas`).

        Replacement is wholesale: any previously-registered modes
        absent from `modes` are dropped. A frame for a dropped mode
        lands as `no_handler` at the dispatcher — the documented
        "tool went away" behaviour.

        Pre-connect, only the in-process `AgentInfo` is mutated;
        `run_async()` publishes the up-to-date snapshot on its
        initial handshake. Post-connect, an `AgentInfoUpdate` is
        broadcast for the new `accepts_schema` + `non_tool_modes`
        (capabilities / description / other fields are independent —
        the bridge calls `update_info(...)` separately if they
        change in lockstep with the tool set).

        Operator-pinned `accepts_schema` and `non_tool_modes` set
        on `AgentInfo(...)` at construction are OVERWRITTEN here —
        `set_modes` IS the pin mutation for runtime callers. The
        operator pin protects against `@agent.handler`'s
        auto-publish; explicit `set_modes` is consent.

        Pin stickiness: once `set_modes` has run, both
        `accepts_schema` and `non_tool_modes` are pinned for the
        lifetime of this Agent instance — a later `@agent.handler`
        registers the new mode in `_handlers_by_mode` but does NOT
        re-derive `accepts_schema`, so the new mode won't be
        published. Mix `set_modes` and `@agent.handler` carefully:
        either decorate all handlers at module load and never call
        `set_modes`, or use `set_modes` exclusively (the MCP-bridge
        pattern).
        """
        new_handlers: dict[str, _RegisteredHandler] = {}
        nt_set = set(non_tool_modes or ())
        for mode_name, (fn, _schema) in modes.items():
            if not isinstance(mode_name, str) or not mode_name:
                raise TypeError(
                    "set_modes mode names must be non-empty strings"
                )
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"set_modes handler for mode {mode_name!r} must be "
                    "async — embedded agents in particular rely on this "
                    "for event-loop fairness."
                )
            sig = inspect.signature(fn)
            if len(sig.parameters) < 2:
                raise TypeError(
                    f"set_modes handler for mode {mode_name!r} must take "
                    "(ctx, payload)"
                )
            new_handlers[mode_name] = _RegisteredHandler(
                fn=fn,
                mode=mode_name,
                # Runtime callers always use `dict`-input handlers —
                # the schema came from outside Python (MCP tool spec,
                # plugin manifest, etc.) and `_make_registered`'s
                # BaseModel-subclass derivation doesn't apply.
                input_model=dict,
                output_model=None,
                tool=mode_name not in nt_set,
            )

        new_accepts: dict[str, dict[str, Any] | None] = {
            name: schema for name, (_fn, schema) in modes.items()
        }
        new_non_tool: list[str] = list(non_tool_modes or ())

        # Atomic swap of the registry + in-memory info. Done before
        # the wire broadcast so a frame that arrives mid-call routes
        # consistently regardless of which side of the ack we're on.
        # Pin the operator-pinned set so the next handler-decorator
        # call doesn't re-derive on top of these explicit values.
        self._handlers_by_mode = new_handlers
        self.info.accepts_schema = new_accepts
        self.info.non_tool_modes = new_non_tool
        self._operator_pinned_schema_fields = (
            self._operator_pinned_schema_fields
            | frozenset({"accepts_schema", "non_tool_modes"})
        )

        if self._dispatcher is None:
            return
        # `update_info` rejects an all-None patch — accepts_schema is
        # never None here (we just built a dict), so it always passes.
        await self.update_info(
            accepts_schema=new_accepts,
            non_tool_modes=new_non_tool,
        )

    def on_startup(self, fn: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(fn)

    def on_shutdown(self, fn: Callable[[], Awaitable[None]]) -> None:
        self._shutdown_hooks.append(fn)

    # ------------------------------------------------------------------
    # Internal: dispatch resolution
    # ------------------------------------------------------------------

    def resolve_handler(
        self, *, mode: str | None
    ) -> _RegisteredHandler | None:
        """Resolve by explicit mode — O(1), order-independent.

        `mode is None` → the agent's sole handler when there is
        exactly one (the common single-handler / mode-agnostic
        producer case); ambiguous (>1 registered) → None, surfaced
        as `no_handler` so the caller must name the mode. An
        unknown mode → None likewise.
        """
        reg = self._handlers_by_mode
        if mode is None:
            return next(iter(reg.values())) if len(reg) == 1 else None
        return reg.get(mode)

    @property
    def registered_handlers(self) -> dict[str, _RegisteredHandler]:
        """The unified mode → handler registry (insertion-ordered)."""
        return self._handlers_by_mode

    # ------------------------------------------------------------------
    # Run loops
    # ------------------------------------------------------------------

    async def update_info(
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
        """Patch-update this agent's published `AgentInfo` via
        `AgentInfoUpdateFrame` (Phase 10e).

        Same logic as `PeerClient.update_agent_info` but callable
        from OUTSIDE a task context — useful for agents that
        update their surface in response to external events:
          * MCP bridge handling `tools/list_changed` notifications.
          * Plugin-host agents loading/unloading plugins via
            SIGHUP / file watcher / admin signal.
          * Any background task running alongside the agent's
            run loop.

        PATCH semantics: `None` means "don't touch". Raises
        `ValueError` on empty patches, `RuntimeError` if the
        agent isn't connected yet, `PeerCallError` on router
        rejection (rate limit / invalid value / not found),
        `AckTimeout` if the router doesn't reply.

        On success, mutates the in-process `self.info` so
        subsequent calls (peers.find / build_tools / etc.) see
        the new values immediately — the router's `CatalogUpdate`
        broadcast tells OTHER agents.
        """
        # Deferred imports to dodge the AgentInfoUpdateFrame ↔ Agent
        # symbol cycle through the SDK's __init__.
        from bp_protocol.frames import AckFrame, AgentInfoUpdateFrame  # noqa: PLC0415
        from bp_sdk.peers import AckTimeout, PeerCallError  # noqa: PLC0415

        if self._dispatcher is None:
            raise RuntimeError(
                "agent.update_info called before run_async() — "
                "agent is not connected to the router yet"
            )
        if all(
            v is None
            for v in (
                description, groups, capabilities, accepts_schema,
                produces_schema, non_tool_modes, hidden,
                documentation_url,
            )
        ):
            raise ValueError(
                "update_info requires at least one field; "
                "an empty patch is a no-op and rejected at the wire"
            )

        frame = AgentInfoUpdateFrame(
            agent_id=self.info.agent_id,
            # No surrounding task context — synthetic trace/span ids.
            # Observability hint only; doesn't affect routing.
            trace_id="0" * 32,
            span_id="0" * 16,
            description=description,
            groups=groups,
            capabilities=capabilities,
            accepts_schema=accepts_schema,
            produces_schema=produces_schema,
            non_tool_modes=non_tool_modes,
            hidden=hidden,
            documentation_url=documentation_url,
        )
        # task_id=None — this isn't inside a task context; falls
        # back to the PendingMap's timeout-reaper path.
        ack_fut = self._dispatcher.register_for_task(
            self._dispatcher.pending_acks,
            frame.correlation_id,
            task_id=None,
        )
        await self._dispatcher.transport.send(frame)
        try:
            ack = await asyncio.wait_for(
                ack_fut, timeout=self.config.pending_acks_timeout_s,
            )
        except TimeoutError as exc:
            raise AckTimeout(
                "router did not ack AgentInfoUpdate within timeout",
            ) from exc
        if not isinstance(ack, AckFrame) or not ack.accepted:
            reason = ack.reason if isinstance(ack, AckFrame) else "unknown"
            raise PeerCallError(
                f"router rejected AgentInfoUpdate: {reason}",
            )

        # Mirror the change on the local in-process AgentInfo.
        for field, value in (
            ("description", description),
            ("groups", groups),
            ("capabilities", capabilities),
            ("accepts_schema", accepts_schema),
            ("produces_schema", produces_schema),
            ("non_tool_modes", non_tool_modes),
            ("hidden", hidden),
            ("documentation_url", documentation_url),
        ):
            if value is not None:
                setattr(self.info, field, value)

    async def spawn_root_for_user(
        self,
        destination_agent_id: str,
        payload: BaseModel | dict[str, Any],
        *,
        user_id: str,
        session_id: str,
        mode: str | None = None,
        priority: Any | None = None,
        idempotency_key: str | None = None,
        deadline: Any | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        ack_timeout_s: float | None = None,
    ) -> str:
        """Admit a *parentless* root task on behalf of an end user (B1).

        A gateway / channel agent uses this to inject a user turn as a
        task carrying the END USER's `(user_id, session_id)` — not its
        own identity. `peers.spawn` can't do this: it is handler-bound
        and always inherits `parent_task_id = ctx.task_id` from the
        running task. Here there is no surrounding task, so the frame
        is parentless and the router admits it on the root-task path
        (lineage + spawn-depth checks are skipped when
        `parent_task_id is None`; the ACL is evaluated at the *session
        principal's* level, derived from `user_id`, not the gateway's
        `service` level).

        Returns the assigned `task_id` *before* the (possibly
        minutes-long) result — the admit/await split so the caller can
        record the id (e.g. to cancel it later) and then call
        `await_root_result(task_id, ...)`. The asserted `user_id` is a
        data-integrity check only; the router re-validates that
        `(user_id, session_id)` is a real, open, owned session (which it
        is when the gateway minted it).

        Raises `AckTimeout` if the router never acks, `SpawnRejected`
        if admit is refused (ACL / schema / quota / unknown session or
        destination), `UnexpectedResponse` if the ack carries no
        `task_id`, and `RuntimeError` if the agent isn't connected yet.
        """
        import secrets  # noqa: PLC0415

        from bp_protocol.frames import AckFrame, NewTaskFrame  # noqa: PLC0415
        from bp_protocol.types import TaskPriority  # noqa: PLC0415
        from bp_sdk.peers import (  # noqa: PLC0415
            AckTimeout,
            SpawnRejected,
            UnexpectedResponse,
        )

        if self._dispatcher is None:
            raise RuntimeError(
                "agent.spawn_root_for_user called before run_async() — "
                "agent is not connected to the router yet"
            )

        payload_dict = (
            payload.model_dump()
            if isinstance(payload, BaseModel)
            else dict(payload)
        )
        frame = NewTaskFrame(
            agent_id=self.info.agent_id,
            # Root of a fresh trace — no surrounding span to inherit.
            # 128-bit trace / 64-bit span, W3C-shaped; caller may pass
            # explicit ids when it already holds an active span.
            trace_id=trace_id or secrets.token_hex(16),
            span_id=span_id or secrets.token_hex(8),
            task_id=None,  # spawn — router assigns + acks the id
            parent_task_id=None,  # ROOT — admit skips lineage/depth checks
            destination_agent_id=destination_agent_id,
            user_id=user_id,
            session_id=session_id,
            priority=priority or TaskPriority.NORMAL,
            deadline=deadline,
            idempotency_key=idempotency_key,
            input_mode=mode,
            payload=payload_dict,
        )
        # No owning handler context (task_id=None) → the ack future
        # falls back to the correlation reaper's timeout path rather
        # than the per-task drain. Mirrors `peers.spawn`'s ack round-trip.
        ack_fut = self._dispatcher.register_for_task(
            self._dispatcher.pending_acks,
            frame.correlation_id,
            None,
            timeout_s=ack_timeout_s,
        )
        await self._dispatcher.transport.send(frame)
        try:
            ack = await ack_fut
        except TimeoutError as exc:
            raise AckTimeout("root-task admit ack timed out") from exc
        if not isinstance(ack, AckFrame) or not ack.accepted:
            reason = ack.reason if isinstance(ack, AckFrame) else "unknown"
            raise SpawnRejected(
                f"router rejected root task: {reason}", reason=reason
            )
        if ack.task_id is None:
            raise UnexpectedResponse(
                "router accepted root task but did not assign task_id"
            )
        return ack.task_id

    async def await_root_result(
        self,
        task_id: str,
        *,
        timeout_s: float | None = None,
        on_progress: Callable[[Any], Any] | None = None,
    ) -> Any:
        """Await the terminal `ResultFrame` of a root task admitted via
        `spawn_root_for_user`, optionally invoking `on_progress` per
        `ProgressFrame` (sync or async callback).

        The router fans Progress + Result frames for the task back to
        this agent (the admitting caller); this drives them through the
        dispatcher's `open_spawn_stream` — the supported out-of-context
        entry point, with the same cancellation-safe subscription
        cleanup the in-handler `peers.spawn(stream=True)` path uses.

        Raises `ResultTimeout` if no terminal frame arrives within
        `timeout_s` (falls back to the dispatcher's configured
        `pending_results` timeout when `None`), and `RuntimeError` if
        the agent isn't connected yet.
        """
        if self._dispatcher is None:
            raise RuntimeError(
                "agent.await_root_result called before run_async() — "
                "agent is not connected to the router yet"
            )

        stream = self._dispatcher.open_spawn_stream(task_id, timeout_s=timeout_s)
        async with stream:
            if on_progress is None:
                # No subscriber callback — don't iterate the queue (a
                # result that raced ahead of our subscribe lives in the
                # result future, not the queue); just await the result.
                return await stream.result(timeout_s=timeout_s)
            # Verbose path: drain Progress frames as they arrive,
            # terminating when the terminal Result lands. The documented
            # streaming idiom — see docs/sdk/core.md §7.
            async for pf in stream:
                res = on_progress(pf)
                if inspect.isawaitable(res):
                    await res
            return await stream.result(timeout_s=timeout_s)

    def run(self) -> None:
        """Blocking run loop for external agents. Installs SIGINT/SIGTERM.

        Exits NON-ZERO (`SystemExit(1)`) when the transport is
        permanently dead (recv loop exhausted): a fleet supervisor
        (`systemd Restart=on-failure`, k8s, etc.) must see the
        failure to restart the agent. The pre-fix path returned
        normally → exit 0 → a permanently-broken agent was silently
        never restarted. Embedded agents call `run_async()` directly
        and get the raised `TransportPermanentlyFailed` to handle
        within their host process instead."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            pass
        except TransportPermanentlyFailed as exc:
            logger.error(
                "agent_transport_permanently_failed",
                extra={
                    "event": "agent_transport_permanently_failed",
                    "error": str(exc),
                },
            )
            raise SystemExit(1) from exc

    async def run_async(self) -> None:
        """Async run loop. Suitable for embedded agents (router lifespan)."""
        from bp_sdk.dispatch import build_dispatcher  # noqa: PLC0415
        from bp_sdk.transport import build_transport  # noqa: PLC0415

        # Onboard if needed
        if not self.config.embedded and not self.config.auth_token:
            from bp_sdk.onboarding import onboard_or_resume  # noqa: PLC0415

            await onboard_or_resume(self.info, self.config)

        transport = await build_transport(self.config, info=self.info)
        self._dispatcher = build_dispatcher(self, transport)

        for hook in self._startup_hooks:
            await hook()

        # Install signal handlers (best-effort; not available on Windows main loop)
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

        # Proactive token refresh — only meaningful for external agents
        # (embedded agents inherit the router's trust boundary and don't
        # carry their own JWT).
        refresh_task: asyncio.Task | None = None
        if not self.config.embedded:
            refresh_task = asyncio.create_task(self._token_refresh_loop())

        try:
            await self._dispatcher.run_until(self._stop_event)  # type: ignore[union-attr]
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                try:
                    await refresh_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for hook in self._shutdown_hooks:
                try:
                    await hook()
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown_hook_failed")
            await transport.close()

    async def _token_refresh_loop(self) -> None:
        """Refresh the agent JWT proactively before expiry.

        Wakes at `schedule_seconds_until_refresh(token)`, rotates via
        `bp_sdk.onboarding.refresh_token`, then loops on the new token.
        On failure, retries with exponential backoff capped at 5 min.
        Cancellation (agent shutdown) propagates as asyncio.CancelledError.
        """
        from bp_sdk.onboarding import (  # noqa: PLC0415
            refresh_token,
            schedule_seconds_until_refresh,
        )

        backoff_s = 30.0
        while not self._stop_event.is_set():
            token = self.config.auth_token
            if not token:
                # Should not happen post-onboard, but be defensive.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
                except TimeoutError:
                    continue
                return

            sleep_s = schedule_seconds_until_refresh(token)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
                return  # stop_event fired
            except TimeoutError:
                pass

            try:
                new_exp = await refresh_token(self.config)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "token_refresh_unexpected", extra={"event": "token_refresh_unexpected"}
                )
                new_exp = None

            if new_exp is None:
                # Transient — back off and retry. Capped so we don't sleep
                # past the token's actual expiry without trying again.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff_s)
                    return
                except TimeoutError:
                    pass
                backoff_s = min(backoff_s * 2, 300.0)
            else:
                backoff_s = 30.0  # success — reset

    async def aclose(self) -> None:
        self._stop_event.set()


# Re-exported for convenience
__all__ = ["Agent", "HandlerError"]
