"""bp_sdk.history — agent-side client for the router-managed session store.

`ctx.history`, shaped like `ctx.files`. Implements the agent half of
`docs/design/router-managed-session-store.md`.

Read the write signatures before the read ones — the asymmetry is the whole
design. Writes take **no owner parameter at any level**: an agent writes its
own threads, and the router stamps the owner from the task's active executor,
so there is no field in which to name another agent. Reads DO take `owner`,
because reads are session-scoped: a summarizer must read the thread it
summarizes, and reading cannot fabricate an utterance.

To put something in another agent's context, `hand_over` it. The owner
drains its queue at turn start and materialises what it chooses under its
own authorship.

Two helpers carry the ergonomics:

  * `batch()` — an ordered op list applied in ONE router-side transaction,
    with reads observing the writes before them. A turn is two round trips.
  * `turn()` — the default path, and it is ORDERED: it takes the session's
    FIFO lease, renews it for the length of the turn, releases it at the
    end, and asserts the thread has not moved before writing. Going
    lock-free is `turn(ordered=False)` and an explicit decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from bp_protocol.frames import (
    AcquireLeaseOp,
    AppendOp,
    AssertThreadOp,
    ConsumeHandoversOp,
    GetStateOp,
    HandoverItem,
    HandOverOp,
    ListThreadsOp,
    ReadOp,
    RedactOp,
    ReleaseLeaseOp,
    RenewLeaseOp,
    SessionMessage,
    SessionOpFrame,
    SessionOpResult,
    SessionResultFrame,
    SetFloorOp,
    SetStateOp,
    StateValue,
    StatThreadOp,
    ThreadStat,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bp_sdk.context import TaskContext

logger = logging.getLogger(__name__)


class SessionStoreError(RuntimeError):
    """A refused batch. `code` is the wire error; `op_index` says which op
    caused it when the router could attribute it.

    The codes a caller is expected to HANDLE rather than log:
    `thread_conflict` (someone appended under you — re-read and retry),
    `version_conflict` (state CAS lost — recompute from the fresh value),
    `lease_lost` (your turn lease expired mid-turn).
    """

    def __init__(self, code: str, op_index: int | None = None) -> None:
        super().__init__(code if op_index is None else f"{code} (op {op_index})")
        self.code = code
        self.op_index = op_index


class _Handle:
    """A placeholder for one op's result, resolved when the batch commits.

    Lets a batch read like straight-line code — `turns = b.read(...)` — while
    still being a single round trip. Touching a handle before the batch has
    been sent is a programming error, not a silent empty result."""

    __slots__ = ("_result", "_index", "_resolved")

    def __init__(self, index: int) -> None:
        self._index = index
        self._result: SessionOpResult | None = None
        self._resolved = False

    def _resolve(self, result: SessionOpResult) -> None:
        self._result = result
        self._resolved = True

    @property
    def result(self) -> SessionOpResult:
        if not self._resolved or self._result is None:
            raise RuntimeError(
                "batch result read before the batch was sent — "
                "exit the `async with ctx.history.batch()` block first"
            )
        return self._result

    @property
    def messages(self) -> list[SessionMessage]:
        return self.result.messages or []

    @property
    def items(self) -> list[HandoverItem]:
        return self.result.items or []

    @property
    def state(self) -> dict[str, StateValue]:
        return {v.key: v for v in (self.result.state or [])}

    @property
    def stat(self) -> ThreadStat | None:
        return self.result.stat

    @property
    def threads(self) -> list[ThreadStat]:
        return self.result.threads or []

    @property
    def message_id(self) -> int | None:
        return self.result.message_id

    @property
    def last_message_id(self) -> int:
        return self.result.last_message_id or 0

    @property
    def truncated_before_id(self) -> int | None:
        return self.result.truncated_before_id


class SessionBatch:
    """Builder for one transactional op list. Ops apply in the order added;
    reads see the writes before them. Any refusal rolls the WHOLE batch back
    — which is what makes "write the summary and move the floor" atomic
    without the router knowing what a summary is."""

    def __init__(self, history: SessionHistory) -> None:
        self._history = history
        self._ops: list[Any] = []
        self._handles: list[_Handle] = []
        self._sent = False

    def _add(self, op: Any) -> _Handle:
        if self._sent:
            raise RuntimeError("batch already sent")
        handle = _Handle(len(self._ops))
        self._ops.append(op)
        self._handles.append(handle)
        return handle

    # -- writes: no owner parameter exists ------------------------------

    def append(
        self,
        role: str,
        content: str,
        *,
        thread: str = "",
        hidden: bool = False,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> _Handle:
        return self._add(
            AppendOp(
                thread_key=thread,
                role=role,
                content=content,
                hidden=hidden,
                metadata=metadata or {},
                idempotency_key=idempotency_key,
            )
        )

    def set_floor(self, floor_id: int, *, thread: str = "") -> _Handle:
        return self._add(SetFloorOp(thread_key=thread, floor_id=floor_id))

    def set_state(
        self,
        key: str,
        value: str | None,
        *,
        thread: str = "",
        session_scoped: bool = False,
        expected_version: int | None = None,
    ) -> _Handle:
        return self._add(
            SetStateOp(
                session_scoped=session_scoped,
                thread_key=thread,
                key=key,
                value=value,
                expected_version=expected_version,
            )
        )

    def redact(self, *message_ids: int) -> _Handle:
        return self._add(RedactOp(message_ids=list(message_ids)))

    def hand_over(
        self,
        target: str,
        item_kind: str,
        payload: dict[str, Any] | None = None,
        *,
        thread: str = "",
    ) -> _Handle:
        return self._add(
            HandOverOp(
                target_agent_id=target,
                thread_key=thread,
                item_kind=item_kind,
                payload=payload or {},
            )
        )

    def consume_handovers(
        self, *, item_kinds: list[str] | None = None, limit: int = 32
    ) -> _Handle:
        return self._add(ConsumeHandoversOp(item_kinds=item_kinds, limit=limit))

    def assert_thread(
        self, last_message_id: int, *, owner: str | None = None, thread: str = ""
    ) -> _Handle:
        """Refuse the batch unless the thread is still where the caller last
        saw it. The guard that turns an interleaved append into a detected
        `thread_conflict` instead of a garbled transcript."""
        return self._add(
            AssertThreadOp(
                owner_agent_id=owner, thread_key=thread, last_message_id=last_message_id
            )
        )

    # -- reads: owner IS a parameter ------------------------------------

    def read(
        self,
        *,
        owner: str | None = None,
        thread: str = "",
        roles: list[str] | None = None,
        include_retired: bool = False,
        include_hidden: bool = True,
        since_id: int | None = None,
        before_id: int | None = None,
        limit: int = 500,
        max_bytes: int | None = None,
    ) -> _Handle:
        return self._add(
            ReadOp(
                owner_agent_id=owner,
                thread_key=thread,
                roles=roles,
                include_retired=include_retired,
                include_hidden=include_hidden,
                since_id=since_id,
                before_id=before_id,
                limit=limit,
                max_bytes=max_bytes,
            )
        )

    def get_state(
        self,
        *keys: str,
        owner: str | None = None,
        thread: str = "",
        session_scoped: bool = False,
    ) -> _Handle:
        return self._add(
            GetStateOp(
                session_scoped=session_scoped,
                owner_agent_id=owner,
                thread_key=thread,
                keys=list(keys) or None,
            )
        )

    def stat(self, *, owner: str | None = None, thread: str = "") -> _Handle:
        return self._add(StatThreadOp(owner_agent_id=owner, thread_key=thread))

    def list_threads(self, *, owner: str | None = None) -> _Handle:
        return self._add(ListThreadsOp(owner_agent_id=owner))

    # -- lease ----------------------------------------------------------

    def acquire_lease(self, holder_id: str, *, ttl_ms: int = 300_000) -> _Handle:
        return self._add(AcquireLeaseOp(holder_id=holder_id, ttl_ms=ttl_ms))

    def renew_lease(self, holder_id: str, *, ttl_ms: int = 300_000) -> _Handle:
        return self._add(RenewLeaseOp(holder_id=holder_id, ttl_ms=ttl_ms))

    def release_lease(self, holder_id: str) -> _Handle:
        return self._add(ReleaseLeaseOp(holder_id=holder_id))

    async def send(self) -> list[SessionOpResult]:
        if self._sent:
            raise RuntimeError("batch already sent")
        self._sent = True
        if not self._ops:
            return []
        results = await self._history._round_trip(self._ops)
        if len(results) != len(self._ops):
            # Results are positional; a short list means this SDK and the
            # router disagree about the op set (an older router silently
            # skipping an op it doesn't know). Fail loudly here rather than
            # let a caller read a handle that never resolved.
            raise SessionStoreError("result_count_mismatch")
        for handle, result in zip(self._handles, results, strict=True):
            handle._resolve(result)
        return results

    async def __aenter__(self) -> SessionBatch:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            await self.send()


class Turn:
    """A lease-ordered turn (§6.4). The default posture: the suite gets
    strict turn ordering without designing for concurrency.

    `ordered=False` skips the lease entirely — legitimate for a shared
    session or genuinely independent parallel work, but then interleaved
    appends and stale context snapshots are the caller's problem. The
    store stays SAFE either way (assertions, CAS, monotonic floors); only
    coherence is at stake.
    """

    def __init__(
        self,
        history: SessionHistory,
        *,
        holder_id: str,
        ordered: bool,
        ttl_ms: int,
        wait_timeout_s: float,
    ) -> None:
        self._history = history
        self._holder_id = holder_id
        self._ordered = ordered
        self._ttl_ms = ttl_ms
        self._wait_timeout_s = wait_timeout_s
        self._renewer: asyncio.Task | None = None
        self.ticket: int | None = None
        self.lease_lost = False

    def batch(self) -> SessionBatch:
        return self._history.batch()

    async def _acquire(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._wait_timeout_s
        while True:
            batch = self._history.batch()
            handle = batch.acquire_lease(self._holder_id, ttl_ms=self._ttl_ms)
            await batch.send()
            lease = handle.result.lease
            if lease is None or lease.granted:
                self.ticket = lease.ticket if lease else None
                return
            self.ticket = lease.ticket
            # Wait for the promotion push, but never ONLY for it: a holder
            # that dies without releasing leaves nobody to promote, so fall
            # back on its TTL deadline and re-acquire. Ordering is preserved
            # regardless — the ticket, not the retry timing, decides.
            fallback = 1.0
            if lease.holder_expires_at is not None:
                from datetime import UTC, datetime  # noqa: PLC0415

                delta = (lease.holder_expires_at - datetime.now(UTC)).total_seconds()
                fallback = max(0.5, min(delta + 0.25, 60.0))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SessionStoreError("session_busy")
            await self._history._await_lease(
                self.ticket, timeout_s=min(fallback, remaining)
            )

    async def _renew_loop(self) -> None:
        interval = max(1.0, (self._ttl_ms / 1000.0) / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                batch = self._history.batch()
                batch.renew_lease(self._holder_id, ttl_ms=self._ttl_ms)
                await batch.send()
            except SessionStoreError as exc:
                if exc.code == "lease_lost":
                    # Don't tear the turn down: the writes are still guarded
                    # by their thread assertions. Record it so the caller can
                    # decide, and stop renewing a lease we no longer hold.
                    self.lease_lost = True
                    logger.warning(
                        "session_lease_lost",
                        extra={"event": "session_lease_lost", "holder": self._holder_id},
                    )
                    return
                raise
            except Exception:  # noqa: BLE001 - renewal is best-effort
                logger.debug("session_lease_renew_failed", exc_info=True)
                return

    async def __aenter__(self) -> Turn:
        if self._ordered:
            await self._acquire()
            self._renewer = asyncio.create_task(self._renew_loop())
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renewer
        if self._ordered and not self.lease_lost:
            # Release even on failure: holding a lease through a crashed turn
            # would wedge the session until its TTL, and the next waiter is
            # already queued behind us.
            with contextlib.suppress(Exception):
                batch = self._history.batch()
                batch.release_lease(self._holder_id)
                await batch.send()


class SessionHistory:
    """Per-task handle on the router-managed session store.

    Convenience methods below are one-op batches; anything that needs two
    or more writes to be atomic — or wants a turn in one round trip —
    should use `batch()`.
    """

    def __init__(
        self,
        ctx: TaskContext,
        *,
        dispatcher: Any | None = None,
        scope: str = "session",
    ) -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher
        self._scope = scope
        self._lease_waiters: dict[int, asyncio.Future] = {}

    @property
    def user_scope(self) -> SessionHistory:
        """The cross-session, user-wide namespace — an agent's standing
        context with this user, the conversational analogue of the file
        store's `persist/`. Survives session close and purge."""
        return SessionHistory(self._ctx, dispatcher=self._dispatcher, scope="user")

    # -- transport ------------------------------------------------------

    async def _round_trip(self, ops: list[Any]) -> list[SessionOpResult]:
        d = self._dispatcher
        if d is None:
            raise RuntimeError(
                "SessionHistory requires a dispatcher (external agent context)"
            )
        frame = SessionOpFrame(
            agent_id=d.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=self._ctx.task_id,
            scope=self._scope,
            ops=ops,
        )
        fut = d.register_for_task(
            d.pending_acks, frame.correlation_id, self._ctx.task_id
        )
        await d.transport.send(frame)
        try:
            res = await fut
        except TimeoutError as exc:
            raise SessionStoreError("timeout") from exc
        if not isinstance(res, SessionResultFrame):
            raise SessionStoreError("unexpected_response")
        if res.error is not None:
            raise SessionStoreError(res.error, res.error_index)
        return res.results

    def _on_lease_frame(self, ticket: int, granted: bool) -> None:
        """Called by the SDK dispatcher when a `SessionLease` push arrives."""
        fut = self._lease_waiters.pop(ticket, None)
        if fut is not None and not fut.done():
            fut.set_result(granted)

    async def _await_lease(self, ticket: int, *, timeout_s: float) -> bool:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._lease_waiters[ticket] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            return False
        finally:
            self._lease_waiters.pop(ticket, None)

    # -- entry points ---------------------------------------------------

    def batch(self) -> SessionBatch:
        return SessionBatch(self)

    def turn(
        self,
        *,
        ordered: bool = True,
        holder_id: str | None = None,
        ttl_ms: int = 300_000,
        wait_timeout_s: float = 300.0,
    ) -> Turn:
        return Turn(
            self,
            holder_id=holder_id or (self._ctx.task_id or "turn"),
            ordered=ordered,
            ttl_ms=ttl_ms,
            wait_timeout_s=wait_timeout_s,
        )

    # -- one-op conveniences --------------------------------------------

    async def append(
        self,
        role: str,
        content: str,
        *,
        thread: str = "",
        hidden: bool = False,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        batch = self.batch()
        handle = batch.append(
            role,
            content,
            thread=thread,
            hidden=hidden,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        await batch.send()
        return handle.message_id or 0

    async def read(
        self,
        *,
        owner: str | None = None,
        thread: str = "",
        roles: list[str] | None = None,
        include_retired: bool = False,
        include_hidden: bool = True,
        since_id: int | None = None,
        limit: int = 500,
    ) -> list[SessionMessage]:
        """Read a thread's active window, paging transparently when the
        router truncates against its byte budget (§9.2) — the caller sees
        one list, oldest first."""
        collected: list[SessionMessage] = []
        before_id: int | None = None
        while True:
            batch = self.batch()
            handle = batch.read(
                owner=owner,
                thread=thread,
                roles=roles,
                include_retired=include_retired,
                include_hidden=include_hidden,
                since_id=since_id,
                before_id=before_id,
                limit=limit,
            )
            await batch.send()
            page = handle.messages
            collected = page + collected
            before_id = handle.truncated_before_id
            if before_id is None or not page:
                return collected

    async def recall(
        self,
        *,
        roles: list[str],
        count: int,
        before_id: int | None = None,
        owner: str | None = None,
        thread: str = "",
    ) -> list[SessionMessage]:
        """A bounded page of older rows — the read side of a recall tool.
        Same query as `read`, different roles and cursor."""
        batch = self.batch()
        handle = batch.read(
            owner=owner,
            thread=thread,
            roles=roles,
            include_retired=True,
            before_id=before_id,
            limit=count,
        )
        await batch.send()
        return handle.messages

    async def state(
        self,
        *keys: str,
        owner: str | None = None,
        thread: str = "",
        session_scoped: bool = False,
    ) -> dict[str, StateValue]:
        batch = self.batch()
        handle = batch.get_state(
            *keys, owner=owner, thread=thread, session_scoped=session_scoped
        )
        await batch.send()
        return handle.state

    async def set_state(
        self,
        key: str,
        value: str | None,
        *,
        thread: str = "",
        session_scoped: bool = False,
        expected_version: int | None = None,
    ) -> None:
        batch = self.batch()
        batch.set_state(
            key,
            value,
            thread=thread,
            session_scoped=session_scoped,
            expected_version=expected_version,
        )
        await batch.send()

    async def stat(self, *, owner: str | None = None, thread: str = "") -> ThreadStat:
        batch = self.batch()
        handle = batch.stat(owner=owner, thread=thread)
        await batch.send()
        return handle.stat or ThreadStat(owner_agent_id=owner or "", thread_key=thread)

    async def hand_over(
        self,
        target: str,
        item_kind: str,
        payload: dict[str, Any] | None = None,
        *,
        thread: str = "",
    ) -> None:
        batch = self.batch()
        batch.hand_over(target, item_kind, payload, thread=thread)
        await batch.send()

    async def consume_handovers(
        self, *, item_kinds: list[str] | None = None, limit: int = 32
    ) -> list[HandoverItem]:
        batch = self.batch()
        handle = batch.consume_handovers(item_kinds=item_kinds, limit=limit)
        await batch.send()
        return handle.items

    async def redact(self, *message_ids: int) -> int:
        batch = self.batch()
        handle = batch.redact(*message_ids)
        await batch.send()
        return handle.result.affected or 0
