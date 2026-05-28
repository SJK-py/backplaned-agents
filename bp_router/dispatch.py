"""bp_router.dispatch — Frame-type → action dispatch.

The receive loop in `ws_hub` decodes one frame at a time and calls
`dispatch_frame`. Side effects are pushed to the right subsystem
(tasks, correlation, observability).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from bp_protocol.errors import safe_validator_message
from bp_protocol.frames import (
    AckFrame,
    AgentInfoUpdateFrame,
    CancelFrame,
    CopyFileRequest,
    DeleteFileRequest,
    ErrorCode,
    FileFetchFrame,
    FileManageFrame,
    FileResultFrame,
    FileStoreFrame,
    FileUploadGrantFrame,
    FileUploadRequestFrame,
    Frame,
    ListFileRequest,
    LlmDeltaFrame,
    LlmRequestFrame,
    LlmResultFrame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
    WriteFileRequest,
)
from bp_protocol.types import TaskState
from bp_router.attachments import (
    AttachmentResolutionError,
    derive_task_file_scope as _derive_task_scope,
)
from bp_router.delivery import fanout_frame
from bp_router.file_store import (
    _PERSIST_PREFIX,
    _allocate_name,
    _display_name,
    _quota_ok,
    _scope_for,
    _split_stash_name,
    _valid_bare_filename,
)
from bp_router.observability import metrics

if TYPE_CHECKING:
    from bp_router.app import AppState
    from bp_router.llm.service import TokenUsage, ToolCall
    from bp_router.ws_hub import SocketEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire-frame error message scrubbing (R4 second-pass review)
# ---------------------------------------------------------------------------
#
# Provider SDK exception messages reach `_err_result` verbatim
# (typed `LlmUpstreamError` and `StreamInterrupted` branches both
# pass `str(exc)`). Those messages can contain:
#   - bearer tokens / api keys (the SDK occasionally formats the
#     Authorization header into the exception)
#   - request bodies (some SDKs serialize the failed request as
#     part of the error string)
#   - upstream endpoint hostnames / request IDs (internal infra)
#
# The catch-all branch at `_handle_llm_request` redacts to a
# generic "internal_error" already; the typed branches should
# too. This scrubber:
#   - truncates at 256 chars (bound the audit-log + on-wire bytes)
#   - strips obvious bearer tokens (`Bearer xyz...`)
#   - strips obvious api-key params (`api_key=xyz...`)
#   - strips OpenAI-style `sk-xxxx` keys
# It does NOT attempt to be a complete PII / secret scrubber —
# operators relying on this for compliance should also gate
# provider keys via the secrets backend (the keys never leave
# the router's memory in normal operation; only error paths
# can leak them).

_SCRUB_MAX_LEN = 256
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.+/=]+")
_API_KEY_RE = re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*[A-Za-z0-9_\-.+/=]+")
_OAI_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")


def _scrub_upstream_message(msg: str) -> str:
    """Bound + redact a provider SDK exception message for the wire."""
    if not msg:
        return msg
    msg = _BEARER_RE.sub("Bearer ***", msg)
    msg = _API_KEY_RE.sub("api_key=***", msg)
    msg = _OAI_SK_RE.sub("sk-***", msg)
    if len(msg) > _SCRUB_MAX_LEN:
        msg = msg[: _SCRUB_MAX_LEN - 1] + "…"
    return msg


def _serialize_tool_calls(
    calls: list[ToolCall | None],
) -> list[dict[str, Any]]:
    """Flatten neutral ToolCall dataclasses into wire-frame dicts.

    Each entry carries `id`, `name`, `args`, and an optional
    `thought_signature`. Gemini 3 returns a signature on the FIRST
    function call of any function-calling response and requires it
    round-tripped on the next turn — preserving it on the dict is the
    point of this serializer.
    """
    out: list[dict[str, Any]] = []
    for tc in calls:
        if tc is None:
            continue
        d: dict[str, Any] = {"id": tc.id, "name": tc.name, "args": tc.args}
        if tc.thought_signature is not None:
            d["thought_signature"] = tc.thought_signature
        out.append(d)
    return out


def _serialize_usage(usage: TokenUsage) -> dict[str, int]:
    """TokenUsage → wire-frame usage dict, omitting zero counts."""
    out: dict[str, int] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    if usage.thoughts_tokens:
        out["thoughts_tokens"] = usage.thoughts_tokens
    if usage.cache_read_tokens:
        out["cache_read_tokens"] = usage.cache_read_tokens
    if usage.cache_write_tokens:
        out["cache_write_tokens"] = usage.cache_write_tokens
    return out


async def dispatch_frame(
    state: AppState,
    entry: SocketEntry,
    frame: Frame,
) -> None:
    """Route an inbound frame to the right handler."""
    # Module-level metrics import (R8 perf): this is the hottest
    # inbound path — every frame from every socket. The previous
    # function-local `from ... import frames_total` ran an import
    # statement (sys.modules dict lookup + the surrounding
    # try/except frame setup) on every single frame. Hoisted to a
    # module-level `from bp_router.observability import metrics`
    # (the pattern tasks.py already uses); the attribute access
    # `metrics.frames_total` is a plain lookup.
    try:
        metrics.frames_total.labels(direction="recv", type=frame.type).inc()
    except Exception:  # noqa: BLE001
        pass

    if isinstance(frame, NewTaskFrame):
        await _handle_new_task(state, entry, frame)
    elif isinstance(frame, ResultFrame):
        await _handle_result(state, entry, frame)
    elif isinstance(frame, ProgressFrame):
        await _handle_progress(state, entry, frame)
    elif isinstance(frame, CancelFrame):
        await _handle_cancel(state, entry, frame)
    elif isinstance(frame, AckFrame):
        await _handle_ack(state, entry, frame)
    elif isinstance(frame, PingFrame):
        await _handle_ping(state, entry, frame)
    elif isinstance(frame, PongFrame):
        await _handle_pong(state, entry, frame)
    elif isinstance(frame, LlmRequestFrame):
        await _handle_llm_request(state, entry, frame)
    elif isinstance(frame, AgentInfoUpdateFrame):
        await _handle_agent_info_update(state, entry, frame)
    elif isinstance(frame, FileUploadRequestFrame):
        await _handle_file_upload_request(state, entry, frame)
    elif isinstance(frame, FileStoreFrame):
        await _handle_file_store(state, entry, frame)
    elif isinstance(frame, FileFetchFrame):
        await _handle_file_fetch(state, entry, frame)
    elif isinstance(frame, FileManageFrame):
        await _handle_file_manage(state, entry, frame)
    else:
        logger.warning(
            "unexpected_frame_in_dispatch",
            extra={"event": "unexpected_frame_in_dispatch", "type": frame.type},
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_new_task(
    state: AppState, entry: SocketEntry, frame: NewTaskFrame
) -> None:
    """Validate, ACL-check, admit, dispatch."""
    from bp_router.tasks import AdmitError, admit_task  # noqa: PLC0415

    try:
        result = await admit_task(state, frame, caller_agent_id=entry.agent_id)
    except AdmitError as exc:
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason=exc.message,
        )
        await entry.outbox.put(ack)
        return
    except Exception:  # noqa: BLE001
        logger.exception(
            "admit_task_failed",
            extra={"event": "admit_task_failed"},
        )
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason="internal_error",
        )
        await entry.outbox.put(ack)
        return

    # Acknowledge the spawn with the assigned task_id.
    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=True,
        task_id=result.task_id,
    )
    await entry.outbox.put(ack)

    # Idempotency replay: when the hit was an ALREADY-TERMINAL task,
    # its terminal Result was fanned out exactly once (to the
    # original request) and will never be emitted again — without
    # this re-emit the retrying caller hangs to its spawn timeout
    # awaiting a frame that can't come. Enqueue the reconstructed
    # terminal Result AFTER the ack on this same socket's outbox:
    # the caller IS `entry` (it sent the spawn), so FIFO ordering
    # means the SDK processes the ack (learns task_id, registers
    # `pending_results[task_id]`) before this Result. The SDK's
    # `PendingMap` early-resolve buffer also covers the reverse
    # order defensively. None on every non-idempotent path and on
    # an in-flight idempotency hit (the retry joins the live task).
    #
    # Double-deliver window (benign, do NOT add router-side dedup):
    # `find_idempotent` is a non-locking read, so in the rare race
    # where the original task's one-shot fan-out lands on this same
    # agent socket around the same time as this replay, the SDK can
    # see TWO terminal frames for one task_id. That is absorbed by
    # construction: `PendingMap.resolve` pops the task_id's pending
    # future on the FIRST frame; the SECOND finds no pending entry
    # and goes to `_buffer_late_value`, which is a bounded
    # (FIFO-evicted) + time-expired buffer keyed by that unique,
    # now-dead task_id — so it is never mis-delivered to another
    # waiter and self-evicts. Net cost is one transient buffered
    # frame, never a crash, mis-resolve, or unbounded growth. The
    # same buffer is what makes the ack→register→result ordering
    # safe; it covers this duplicate for free.
    if result.replay_result is not None:
        await entry.outbox.put(result.replay_result)


async def _handle_result(
    state: AppState, entry: SocketEntry, frame: ResultFrame
) -> None:
    """Persist + propagate to parent. Send Ack to the reporting agent."""
    from bp_router.tasks import complete_task  # noqa: PLC0415

    try:
        await complete_task(state, frame, reporting_agent_id=entry.agent_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "complete_task_failed",
            extra={"event": "complete_task_failed", "bp.task_id": frame.task_id},
        )
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason="internal_error",
        )
        await entry.outbox.put(ack)
        return

    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=True,
    )
    await entry.outbox.put(ack)


async def _handle_progress(
    state: AppState, entry: SocketEntry, frame: ProgressFrame
) -> None:
    """Fan-out to the caller agent's socket. No persistence; best-effort.

    Consults `state.caller_agent_cache` first — at line rate (a
    chatty agent emitting 100 Progress/s) the per-frame SQL lookup
    this used to do saturated the default 10-conn pool. The cache
    is populated at admit time, so for any task admitted on this
    worker the lookup is O(1) and we skip the DB round-trip.
    Cache miss falls through to a single-table SELECT; the result
    is back-filled so subsequent Progress frames hit cache — UNLESS
    the task is already in a terminal state, in which case we do
    NOT back-fill (a stale or adversarial Progress frame would
    otherwise re-insert the entry after `_notify_task_terminal`
    evicted it).
    """
    cache = getattr(state, "caller_agent_cache", None)
    caller_agent_id: str | None
    active_agent_id: str | None
    if cache is not None and frame.task_id in cache:
        caller_agent_id, active_agent_id = cache[frame.task_id]
    else:
        pool = state.db_pool  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, caller_agent_id, active_agent_id "
                "FROM tasks WHERE task_id = $1",
                frame.task_id,
            )
        if row is None:
            return
        caller_agent_id = row["caller_agent_id"]
        active_agent_id = row["active_agent_id"]
        if cache is not None and not TaskState(row["state"]).is_terminal:
            cache[frame.task_id] = (caller_agent_id, active_agent_id)
    # Authz: only the task's CURRENT executor may emit Progress for
    # it — the same rule `complete_task` enforces for Results.
    # Without this, any connected agent could spoof
    # `Progress{task_id=X}` and inject attacker-controlled content
    # into another tenant's caller stream. Best-effort: drop
    # silently (no error path back to a Progress sender).
    if active_agent_id is None or entry.agent_id != active_agent_id:
        return
    if caller_agent_id is None:
        return
    fanout_frame(state, [caller_agent_id], frame)


async def _handle_cancel(
    state: AppState, entry: SocketEntry, frame: CancelFrame
) -> None:
    """Cancel an in-flight LLM call or recursively cancel a task."""
    # LLM-call abort: cancel just the matching router-side asyncio.Task.
    if frame.ref_correlation_id is not None:
        task = entry.llm_tasks.pop(frame.ref_correlation_id, None)
        if task is not None and not task.done():
            task.cancel()
        return

    if frame.task_id is None:
        return  # malformed; nothing to do

    from bp_router.db import queries  # noqa: PLC0415
    from bp_router.tasks import cancel_task  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM tasks WHERE task_id = $1",
            frame.task_id,
        )
        if row is None:
            return

        # Authorise: the calling agent must be the assignee of the
        # task itself or any of its ancestors WITHIN the same user's
        # tree. Otherwise an agent A1 belonging to user U1 could
        # send Cancel{task_id=X} where X is owned by U2 and the
        # router would happily cancel U2's tree using U2's user_id.
        # The `user_id=row["user_id"]` scoping
        # also stops a cross-user `parent_task_id` chain from
        # bridging the boundary if the path-traversal guard ever regresses.
        authorised = await queries.task_has_ancestor_with_agent(
            conn,
            task_id=frame.task_id,
            agent_id=entry.agent_id,
            user_id=row["user_id"],
        )
    if not authorised:
        logger.warning(
            "cancel_unauthorised",
            extra={
                "event": "cancel_unauthorised",
                "agent_id": entry.agent_id,
                "task_id": frame.task_id,
                "task_user_id": row["user_id"],
            },
        )
        # Drop silently — emitting an explicit error here would let a
        # malicious agent enumerate task IDs (probing for "exists" vs
        # "not authorised" via response timing / shape). Audit the
        # attempt instead.
        return

    await cancel_task(
        state,
        frame.task_id,
        user_id=row["user_id"],
        reason=frame.reason,
        initiator=entry.agent_id,
    )


async def _handle_ack(
    state: AppState, entry: SocketEntry, frame: AckFrame
) -> None:
    """Resolve any pending ack for `frame.ref_correlation_id`.

    Scoped to this socket's `inflight_correlations`. Without the membership
    check, agent A1 could craft `Ack{ref_correlation_id=<router→A2
    correlation>}` and resolve A2's pending future — letting a
    malicious agent unblock or short-circuit another agent's
    pending operations. Out-of-band acks are silently dropped
    rather than logged loudly: a chatty agent could otherwise
    log-spam by sending random correlation_ids. The drop is
    counted in `ws_unknown_correlation_total{frame_type="Ack"}`
    so the rate is visible on dashboards.
    """
    if frame.ref_correlation_id not in entry.inflight_correlations:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            ws_unknown_correlation_total,
        )
        ws_unknown_correlation_total.labels(frame_type="Ack").inc()
        return
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]


async def _handle_ping(
    state: AppState, entry: SocketEntry, frame: PingFrame
) -> None:
    pong = PongFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
    )
    await entry.outbox.put(pong)


async def _handle_pong(
    state: AppState, entry: SocketEntry, frame: PongFrame
) -> None:
    """Resolve heartbeat Ping for this socket only.

    Same shape as `_handle_ack`: the `ref_correlation_id` MUST be a
    Ping THIS socket sent. Without the check, agent A1 sending
    `Pong{ref_correlation_id=<router→A2 ping>}` keeps A2's wedged
    socket alive past its heartbeat-timeout eviction (DoS-by-keepalive).
    The drop is counted in
    `ws_unknown_correlation_total{frame_type="Pong"}`
    so the rate is visible.
    """
    if frame.ref_correlation_id not in entry.inflight_correlations:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            ws_unknown_correlation_total,
        )
        ws_unknown_correlation_total.labels(frame_type="Pong").inc()
        return
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LLM request handler
# ---------------------------------------------------------------------------


async def _handle_llm_request(
    state: AppState, entry: SocketEntry, frame: LlmRequestFrame
) -> None:
    """Run an LLM call against `state.llm_service` and stream/return the result.

    The router-side asyncio.Task is tracked on the SocketEntry so the
    disconnect handler can cancel in-flight LLM work and stop wasting
    provider tokens on a dead client.

    Rejects duplicate `correlation_id`s.
    Without the dedup check, a programming error or adversarial agent
    sending two LlmRequests with the same correlation_id would
    silently orphan the first router-side asyncio.Task: it stops
    being tracked in `entry.llm_tasks`, isn't cancelled on
    `_on_disconnect`, and keeps consuming provider tokens until the
    upstream completes naturally. The CancelFrame's
    `ref_correlation_id` abort path also can't reach it. We refuse
    the second request with a typed LlmResult error and leave the
    first request's task running unchanged — the original work
    isn't disrupted and the buggy agent gets a clean signal.
    """
    import asyncio  # noqa: PLC0415

    if frame.correlation_id in entry.llm_tasks:
        existing = entry.llm_tasks[frame.correlation_id]
        if not existing.done():
            logger.warning(
                "llm_request_duplicate_correlation_id",
                extra={
                    "event": "llm_request_duplicate_correlation_id",
                    "bp.agent_id": entry.agent_id,
                    "correlation_id": frame.correlation_id,
                },
            )
            await entry.outbox.put(LlmResultFrame(
                agent_id="router",
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
                error={
                    "code": ErrorCode.FRAME_INVALID,
                    "message": (
                        "duplicate correlation_id — another LlmRequest "
                        "with this correlation_id is still in flight"
                    ),
                },
            ))
            return
        # Done-but-not-yet-cleaned-up entry: cleanup callback hasn't
        # fired (task can finish before add_done_callback runs). The
        # `_cleanup` below uses identity (`is _t`) when popping, so
        # the prior task's pending callback won't accidentally remove
        # the new task we're about to register.

    task = asyncio.create_task(_run_llm_call(state, entry, frame))
    # R8 (HIGH): stamp the originating task_id on the Task so
    # `cancel_task(task_id=X)` can later find + abort every LLM
    # call that was spawned on behalf of task X. Pre-R8 the
    # router-side LLM Tasks kept streaming after a task-level
    # cancel — the agent received the CancelFrame but the
    # router-side asyncio.Task running the provider call wasn't
    # aborted, so provider tokens kept burning until the call
    # naturally finished.
    setattr(task, "_bp_task_id", frame.task_id)
    entry.llm_tasks[frame.correlation_id] = task

    # R8 perf: also index by task_id so `cancel_task` can find the
    # router-side LLM Tasks for a task in O(1) instead of scanning
    # every live socket × every in-flight LLM task. Only index when
    # there's a real task_id (None would be a useless key the
    # scanner already skips).
    tid = frame.task_id
    idx = getattr(state, "llm_tasks_by_task_id", None)
    if tid is not None and idx is not None:
        idx.setdefault(tid, set()).add(task)

    def _cleanup(_t: asyncio.Task) -> None:
        # Identity check. The prior fix's
        # `pop(..., None)` was idempotent for a MISSING key but
        # destructive when the key was live and pointed at a
        # DIFFERENT task — exactly the case in the done-but-not-
        # yet-cleaned-up branch above, where the OLD task's queued
        # cleanup would otherwise remove the NEW task from the
        # tracking dict, orphaning it (no _on_disconnect cancel,
        # no Cancel{ref_correlation_id} reachability).
        current = entry.llm_tasks.get(frame.correlation_id)
        if current is _t:
            entry.llm_tasks.pop(frame.correlation_id, None)
        # Prune the task_id index so it stays self-bounded. Discard
        # by identity; drop the key when its set empties.
        if tid is not None and idx is not None:
            bucket = idx.get(tid)
            if bucket is not None:
                bucket.discard(_t)
                if not bucket:
                    idx.pop(tid, None)

    task.add_done_callback(_cleanup)


async def _run_llm_call(
    state: AppState, entry: SocketEntry, frame: LlmRequestFrame
) -> None:
    from bp_router.llm.presets import (  # noqa: PLC0415
        PresetNotAllowedError,
        PresetUnknownError,
    )
    from bp_router.llm.retry_classification import (  # noqa: PLC0415
        LlmUpstreamError,
        StreamInterrupted,
    )
    from bp_router.llm.service import Message, ToolSpec  # noqa: PLC0415

    correlation = frame.correlation_id
    # `preset` wins when both are set; otherwise fall back to legacy
    # `model` (which resolves through the same default-preset names).
    preset_name = frame.preset or frame.model

    def _send(out_frame: Frame) -> asyncio.Future:  # type: ignore[no-untyped-def]
        return entry.outbox.put(out_frame)

    def _err_result(
        message: str,
        *,
        code: str = "internal_error",
        retry_after_seconds: float | None = None,
        upstream_class: str | None = None,
    ) -> LlmResultFrame:
        # Pydantic v2 coerces the dict to `LlmResultError`; `retriable`
        # auto-derives from `code` via `RETRIABLE_LLM_CODES`.
        # Scrub the message before surfacing — provider SDK exception
        # messages can include endpoint hostnames, raw request bodies,
        # auth-header fragments, and similar internal infrastructure
        # data that the calling agent shouldn't see (or that we don't
        # want bouncing off audit logs / forwarded by the agent's
        # error handler).
        error_payload: dict[str, Any] = {
            "code": code,
            "message": _scrub_upstream_message(message),
        }
        if retry_after_seconds is not None:
            error_payload["retry_after_seconds"] = retry_after_seconds
        if upstream_class:
            error_payload["upstream_class"] = upstream_class
        return LlmResultFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=correlation,
            error=error_payload,
        )

    # Look up the caller's user level for the tier gate.
    #
    # Failure handling: if the DB is unreachable, we *fail-closed for
    # tier-gated presets* but proceed for `*` presets (those don't
    # consult user_level anyway). The original code silently swallowed
    # the exception and continued with `user_level=None`, which made
    # tier-pinned routing collapse into "everyone gets `*` only" — a
    # quiet, hard-to-debug degradation. Now we surface a clean
    # `auth_lookup_failed` error code when the requested preset needs
    # the gate, so admins notice the DB outage immediately.
    preset_obj = None
    try:
        preset_obj = state.llm_service.get_preset(preset_name)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        # `get_preset` shouldn't raise, but defensive — treat as unknown.
        preset_obj = None
    preset_needs_tier = (
        preset_obj is not None and preset_obj.min_user_level != "*"
    )

    user_level: str | None = None
    if frame.user_id:
        # Peek the cache BEFORE acquiring a DB connection.
        # Most calls hit the warm cache; without
        # the peek, every LLM request paid the pool-acquire cost
        # even when no DB query was needed.
        user_level = state.llm_service.peek_user_level_cached(  # type: ignore[attr-defined]
            frame.user_id
        )
        if user_level is None:
            try:
                async with state.db_pool.acquire() as conn:
                    user_level = await state.llm_service.resolve_user_level(  # type: ignore[attr-defined]
                        conn, frame.user_id
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "llm_user_level_lookup_failed",
                    extra={
                        "event": "llm_user_level_lookup_failed",
                        "bp.user_id": frame.user_id,
                        "bp.agent_id": entry.agent_id,
                        "preset_needs_tier": preset_needs_tier,
                    },
                    exc_info=True,
                )
                if preset_needs_tier:
                    await _send(_err_result(
                        "user-level lookup unavailable; tier gate cannot be "
                        "evaluated for this preset",
                        code=ErrorCode.LLM_AUTH_LOOKUP_FAILED,
                    ))
                    return

    try:
        # Resolve any `file_ref` parts (router-proxy / http /
        # localfile, via the shared multi-protocol resolver) into
        # inline image/document bytes BEFORE the provider adapter
        # sees the messages. No-op when there are none; embed has no
        # messages so it's untouched.
        from bp_router.llm.attachments import (  # noqa: PLC0415
            resolve_request_file_refs,
        )

        await resolve_request_file_refs(
            state,
            messages=frame.messages,
            user_id=frame.user_id,
            caller_agent_id=entry.agent_id,
            # Name file_refs derive their (user_id, session_id) scope
            # from this task (active-executor verified); proxy refs
            # ignore it. None is fine when there are no name refs.
            task_id=frame.task_id,
        )

        if frame.kind == "embed":
            text = frame.text or []
            vectors = await state.llm_service.embed(  # type: ignore[attr-defined]
                text,
                preset=preset_name,
                user_id=frame.user_id,
                user_level=user_level,
            )
            await _send(
                LlmResultFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    vectors=vectors,
                )
            )
            return

        if frame.kind == "count_tokens":
            messages = [
                Message(
                    role=m["role"],
                    content=m["content"],
                    name=m.get("name"),
                    tool_call_id=m.get("tool_call_id"),
                )
                for m in frame.messages
            ]
            total = await state.llm_service.count_tokens(  # type: ignore[attr-defined]
                messages,
                preset=preset_name,
                user_level=user_level,
            )
            await _send(
                LlmResultFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    total_tokens=total,
                )
            )
            return

        # Default: generate
        messages = [
            Message(
                role=m["role"],
                content=m["content"],
                name=m.get("name"),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in frame.messages
        ]
        tools = (
            [
                ToolSpec(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=t.get("parameters") or t.get("input_schema") or {},
                )
                for t in frame.tools
            ]
            if frame.tools
            else None
        )

        if not frame.stream:
            resp = await state.llm_service.generate(  # type: ignore[attr-defined]
                messages,
                preset=preset_name,
                tools=tools,
                tool_choice=frame.tool_choice,
                temperature=frame.temperature,
                max_tokens=frame.max_tokens,
                stream=False,
                provider_options=frame.provider_options,
                user_id=frame.user_id,
                user_level=user_level,
                task_id=frame.task_id,
            )
            await _send(
                LlmResultFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    text=resp.text,
                    tool_calls=_serialize_tool_calls(resp.tool_calls),
                    finish_reason=resp.finish_reason,
                    usage=_serialize_usage(resp.usage),
                    thought_summary=resp.thought_summary,
                    thought_signature=resp.thought_signature,
                    reasoning_blocks=resp.reasoning_blocks,
                )
            )
            return

        # Streaming
        iterator = await state.llm_service.generate(  # type: ignore[attr-defined]
            messages,
            preset=preset_name,
            tools=tools,
            tool_choice=frame.tool_choice,
            temperature=frame.temperature,
            max_tokens=frame.max_tokens,
            stream=True,
            provider_options=frame.provider_options,
            user_id=frame.user_id,
            user_level=user_level,
            task_id=frame.task_id,
        )

        # Aggregators below mirror EVERY field on TokenUsage so the
        # final LlmResultFrame.usage and the post-stream `_record`
        # call land complete data — not just the input/output/
        # thoughts subset. Without
        # cache_read / cache_write / cost aggregation, billing and
        # quota telemetry silently dropped those signals on every
        # streaming call (which is the dominant call shape for
        # chat-style agents). Anthropic emits cumulative usage in
        # message_delta — `max()` absorbs that without re-summing
        # partials. cost_microusd is a derived field some adapters
        # populate per-delta; max() is the right operator there too.
        final_finish = "stop"
        agg_in = agg_out = agg_thoughts = 0
        agg_cache_read = agg_cache_write = agg_cost = 0
        agg_reasoning: list[dict[str, Any]] = []
        async for delta in iterator:  # type: ignore[union-attr]
            # Meta deltas are status hints (e.g. "retry pending"
            # during streaming setup-retry backoff). Mutex with the
            # content fields per LlmDeltaFrame's validator — emit
            # alone, never alongside text / tool_call / etc.
            if delta.meta is not None:
                await _send(
                    LlmDeltaFrame(
                        agent_id="router",
                        trace_id=frame.trace_id,
                        span_id=frame.span_id,
                        ref_correlation_id=correlation,
                        meta=delta.meta,
                    )
                )
                continue
            await _send(
                LlmDeltaFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    text=delta.text,
                    tool_call=(
                        _serialize_tool_calls([delta.tool_call])[0]
                        if delta.tool_call
                        else None
                    ),
                    finish_reason=delta.finish_reason,
                    usage=_serialize_usage(delta.usage) if delta.usage else None,
                    thought=delta.thought,
                    thought_signature=delta.thought_signature,
                    reasoning_block=delta.reasoning_block,
                )
            )
            if delta.finish_reason:
                final_finish = delta.finish_reason
            if delta.usage:
                # Anthropic's `message_delta` usage is cumulative; max()
                # absorbs that without re-accumulating partials.
                agg_in = max(agg_in, delta.usage.input_tokens)
                agg_out = max(agg_out, delta.usage.output_tokens)
                agg_thoughts = max(agg_thoughts, delta.usage.thoughts_tokens)
                agg_cache_read = max(
                    agg_cache_read, delta.usage.cache_read_tokens
                )
                agg_cache_write = max(
                    agg_cache_write, delta.usage.cache_write_tokens
                )
                agg_cost = max(agg_cost, delta.usage.cost_microusd)
            if delta.reasoning_block:
                agg_reasoning.append(delta.reasoning_block)

        # Build a single TokenUsage from the aggregators and route it
        # through _serialize_usage so the wire shape exactly matches
        # the unary path. Hand-
        # building the dict here was how cache_read / cache_write /
        # cost slipped past for so long.
        from bp_router.llm.service import TokenUsage  # noqa: PLC0415

        final_usage = TokenUsage(
            input_tokens=agg_in,
            output_tokens=agg_out,
            thoughts_tokens=agg_thoughts,
            cache_read_tokens=agg_cache_read,
            cache_write_tokens=agg_cache_write,
            cost_microusd=agg_cost,
        )
        await _send(
            LlmResultFrame(
                agent_id="router",
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=correlation,
                finish_reason=final_finish,
                usage=_serialize_usage(final_usage),
                reasoning_blocks=agg_reasoning,
            )
        )
        # Land the same Prometheus counters the unary path lands.
        # Best-effort; the helper
        # internally swallows lookup / build failures so a telemetry
        # blip never disrupts agent-visible behaviour.
        state.llm_service.record_streaming_outcome(  # type: ignore[attr-defined]
            preset_name=preset_name,
            usage=final_usage,
            finish_reason=final_finish,
            user_id=frame.user_id,
            task_id=frame.task_id,
        )
    except asyncio.CancelledError:
        # Disconnect or supersede; don't send a result frame, the socket is gone.
        raise
    except PresetUnknownError as exc:
        logger.info(
            "llm_preset_unknown",
            extra={
                "event": "llm_preset_unknown",
                "bp.agent_id": entry.agent_id,
                "bp.preset": str(exc),
            },
        )
        try:
            await entry.outbox.put(
                _err_result(f"unknown preset: {exc}", code=ErrorCode.LLM_PRESET_UNKNOWN)
            )
        except Exception:  # noqa: BLE001
            pass
    except PresetNotAllowedError as exc:
        logger.info(
            "llm_preset_not_allowed",
            extra={
                "event": "llm_preset_not_allowed",
                "bp.agent_id": entry.agent_id,
                "bp.user_level": exc.user_level,
                "bp.preset": exc.preset_name,
                "bp.required_level": exc.required,
            },
        )
        try:
            await entry.outbox.put(
                _err_result(str(exc), code=ErrorCode.LLM_PRESET_NOT_ALLOWED)
            )
        except Exception:  # noqa: BLE001
            pass
    except StreamInterrupted as exc:
        # Mid-stream connection drop after deltas had been delivered.
        # Agent already has partial output, so this is NOT retriable.
        # Surface as the typed `stream_interrupted` code so SDK
        # clients distinguish it from "request never started" failures.
        logger.warning(
            "llm_stream_interrupted",
            extra={
                "event": "llm_stream_interrupted",
                "bp.agent_id": entry.agent_id,
                "after_n_deltas": exc.after_n_deltas,
                "upstream_class": exc.upstream_class,
            },
        )
        try:
            await entry.outbox.put(_err_result(
                exc.message,
                code=ErrorCode.LLM_STREAM_INTERRUPTED,
                upstream_class=exc.upstream_class or None,
            ))
        except Exception:  # noqa: BLE001
            pass
    except LlmUpstreamError as exc:
        # Classified provider failure on chain exhaustion. Emit the
        # typed code (and `retry_after_seconds` hint when present)
        # rather than the previous generic `internal_error`.
        logger.warning(
            "llm_upstream_error",
            extra={
                "event": "llm_upstream_error",
                "bp.agent_id": entry.agent_id,
                "code": exc.code,
                "upstream_class": exc.upstream_class,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
        try:
            await entry.outbox.put(_err_result(
                exc.message,
                code=exc.code,
                retry_after_seconds=exc.retry_after_seconds,
                upstream_class=exc.upstream_class or None,
            ))
        except Exception:  # noqa: BLE001
            pass
    except AttachmentResolutionError as exc:
        # A `file_ref` couldn't be resolved (bad/expired key, SSRF
        # refusal, gate disabled, over the inline cap, not found).
        # The message is intentionally agent-safe (the SSRF path is
        # opaque), so surface it + the code — same contract the task
        # path gives via AdmitError. Never silently drop: a missing
        # image changes the model's answer.
        try:
            await entry.outbox.put(
                _err_result(exc.message, code=exc.code)
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        # Catch-all for unclassified failures (programming errors,
        # provider SDK bugs, etc.). The `LlmUpstreamError` branch
        # above is the *expected* provider-failure path; reaching
        # here means something went wrong inside the router. Emit a
        # FIXED message — never `str(exc)` — because the result
        # frame flows back to the calling agent (in production: to
        # user-controlled code via `ctx.llm.generate(...)`), and
        # exception strings often leak host names, file paths, env
        # variable hints, internal SQL fragments, etc. The full
        # traceback stays in `logger.exception` for ops investigation.
        logger.exception(
            "llm_call_failed",
            extra={
                "event": "llm_call_failed",
                "bp.agent_id": entry.agent_id,
            },
        )
        try:
            await entry.outbox.put(_err_result("internal_error"))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# AgentInfoUpdate (Phase 10e)
# ---------------------------------------------------------------------------


_AGENT_INFO_MUTABLE_FIELDS = (
    "description",
    "groups",
    "capabilities",
    "accepts_schema",
    "produces_schema",
    "produces_files",
    "non_tool_modes",
    "mode_descriptions",
    "hidden",
    "documentation_url",
)


async def _handle_agent_info_update(
    state: AppState, entry: SocketEntry, frame: AgentInfoUpdateFrame
) -> None:
    """Agent sent a patch-update of its AgentInfo. Merge, validate,
    persist, broadcast CatalogUpdate.

    Rate-limited per-agent (settings.agent_info_update_rate_limit_*)
    to bound CatalogUpdate broadcast frequency — each accepted
    update triggers an O(agents²) push.
    """
    from bp_protocol.types import AgentInfo  # noqa: PLC0415
    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415
    from bp_router.db import queries  # noqa: PLC0415
    from bp_router.quota import BUCKET_AGENT_INFO_UPDATE  # noqa: PLC0415

    settings = state.settings  # type: ignore[attr-defined]
    bucket_key = f"{BUCKET_AGENT_INFO_UPDATE}:{entry.agent_id}"
    decision = await state.login_quota.try_consume(  # type: ignore[attr-defined]
        bucket_key,
        rate_per_s=settings.agent_info_update_rate_limit_per_agent_per_s,
        burst=settings.agent_info_update_rate_limit_per_agent_burst,
    )
    if not decision.allowed:
        await _ack(entry, frame, accepted=False, reason="rate_limited")
        _agent_info_update_metric("rate_limited")
        return

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        # SELECT FOR UPDATE + merge + UPDATE share one transaction so
        # two concurrent AgentInfoUpdate frames serialise — without
        # the lock both read the same baseline and the second
        # UPDATE silently clobbers the first.
        async with conn.transaction():
            row = await queries.get_agent_for_update(conn, entry.agent_id)
            if row is None:
                await _ack(
                    entry, frame, accepted=False, reason="agent_not_found"
                )
                _agent_info_update_metric("rejected")
                return

            # Patch-merge: only non-None fields on the frame are applied.
            existing = dict(row.agent_info or {})
            patch: dict[str, Any] = {}
            for field in _AGENT_INFO_MUTABLE_FIELDS:
                value = getattr(frame, field)
                if value is not None:
                    patch[field] = value
            merged = {**existing, **patch}
            # agent_id is locked — keep the existing value even if
            # something upstream tried to slip it in. The new-frame
            # grammar doesn't expose agent_id but defence in depth.
            merged["agent_id"] = entry.agent_id

            # Re-validate the full shape via AgentInfo. This catches
            # malformed group / capability / documentation_url values
            # using the same validators Hello did.
            try:
                validated_info = AgentInfo.model_validate(merged)
            except ValidationError as exc:
                await _ack(
                    entry,
                    frame,
                    accepted=False,
                    reason=f"invalid_agent_info: {safe_validator_message(exc)}",
                )
                _agent_info_update_metric("rejected")
                return

            # Persist. Update the JSONB blob + the denormalised
            # `groups` / `capabilities` columns the ACL evaluator reads
            # from directly (kept in sync with the JSONB).
            await queries.update_agent_info(
                conn,
                entry.agent_id,
                agent_info=validated_info.model_dump(),
                groups=list(validated_info.groups),
                capabilities=list(validated_info.capabilities),
            )
            await queries.append_audit_event(
                conn,
                actor_kind="agent",
                actor_id=entry.agent_id,
                event="agent.info_updated",
                target_kind="agent",
                target_id=entry.agent_id,
                payload={"fields_changed": sorted(patch.keys())},
            )

    # Increment the metric AFTER the DB transaction committed but
    # BEFORE the catalog broadcast / Ack send. Semantically the
    # update IS the commit — once the row + audit are persisted
    # the "AgentInfoUpdate accepted" event has happened. Ordering
    # this AFTER `_ack` (the original shape) means a transient
    # transport failure between commit and ack lost the counter
    # increment despite the DB change being durable.
    _agent_info_update_metric("accepted")

    # Broadcast updated catalog to everyone — including the agent
    # itself, so its `peers.visible()` reflects any group/cap
    # changes immediately. Phase 4 helper.
    await push_catalog_update_to_all(state)

    await _ack(entry, frame, accepted=True)


def _agent_info_update_metric(outcome: str) -> None:
    """Increment the agent_info_update counter. Wrapped so a metric
    import failure / registry hiccup never breaks the handler path."""
    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            agent_info_update_total,
        )
        agent_info_update_total.labels(outcome=outcome).inc()
    except Exception:  # noqa: BLE001
        pass


async def _ack(
    entry: SocketEntry,
    frame: AgentInfoUpdateFrame,
    *,
    accepted: bool,
    reason: str | None = None,
) -> None:
    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=accepted,
        reason=reason,
    )
    await entry.outbox.put(ack)


async def _handle_file_upload_request(
    state: AppState, entry: SocketEntry, frame: FileUploadRequestFrame
) -> None:
    """Agent negotiating a scoped one-shot file-upload credential.

    Security: the owning `user_id` is derived from the task row
    AFTER verifying the connection's authenticated `agent_id`
    (`entry.agent_id`, bound at Hello — never self-asserted) is
    that task's current `active_agent_id` — the same authz rule
    `complete_task` uses to decide who may emit a task's terminal
    Result (a write capability of comparable weight). Any `user_id`
    an agent might try to smuggle in is irrelevant; it's never
    read. Failures reply a SINGLE opaque `"denied"` (unknown-task
    and not-your-task are indistinguishable) so an agent can't
    enumerate task ids — mirrors the `_handle_cancel` silent-drop
    rationale, but as a correlated reply so the SDK fails fast
    instead of hanging to its negotiation timeout.
    """
    from bp_router.quota import BUCKET_FILE_UPLOAD_REQUEST  # noqa: PLC0415
    from bp_router.security.jwt import (  # noqa: PLC0415
        issue_file_upload_token,
    )

    settings = state.settings  # type: ignore[attr-defined]

    def _grant(**kw: Any) -> FileUploadGrantFrame:
        return FileUploadGrantFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            **kw,
        )

    bucket_key = f"{BUCKET_FILE_UPLOAD_REQUEST}:{entry.agent_id}"
    decision = await state.login_quota.try_consume(  # type: ignore[attr-defined]
        bucket_key,
        rate_per_s=settings.file_upload_request_rate_limit_per_agent_per_s,
        burst=settings.file_upload_request_rate_limit_per_agent_burst,
    )
    if not decision.allowed:
        await entry.outbox.put(_grant(error="rate_limited"))
        return

    # Defence in depth: the upload endpoint re-caps at
    # min(max_upload_bytes, grant.byte_size), but refuse here too so
    # a grant never promises more than will be accepted.
    if frame.byte_size <= 0 or frame.byte_size > settings.max_upload_bytes:
        await entry.outbox.put(_grant(error="denied"))
        return

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, active_agent_id FROM tasks WHERE task_id = $1",
            frame.task_id,
        )
    if row is None or row["active_agent_id"] != entry.agent_id:
        # Operator visibility for the otherwise-opaque refusal
        # (the wire stays non-enumerable).
        logger.warning(
            "file_upload_request_denied",
            extra={
                "event": "file_upload_request_denied",
                "bp.agent_id": entry.agent_id,
                "bp.task_id": frame.task_id,
                "reason": "unknown_task" if row is None else "not_active_agent",
            },
        )
        await entry.outbox.put(_grant(error="denied"))
        return

    token, exp, _jti = issue_file_upload_token(
        user_id=row["user_id"],
        sha256=frame.sha256,
        byte_size=frame.byte_size,
        mime_type=frame.mime_type,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.file_upload_token_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )
    await entry.outbox.put(
        _grant(
            upload_url="/v1/files/upload",
            upload_token=token,
            expires_at=exp,
        )
    )


# ---------------------------------------------------------------------------
# Router-managed named file store (docs/design/router-managed-file-store.md)
#
# Name parsing, scope keys, the dedup allocation policy, and the per-user
# quota gate are shared with the session-authed HTTP endpoints, so they
# live in `bp_router.file_store` (imported at the top of this module).
# The frame handlers below compose them under a transaction.
# ---------------------------------------------------------------------------


def _file_result(frame: Frame, **kw: Any) -> FileResultFrame:
    return FileResultFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        **kw,
    )


def _log_file_denied(agent_id: str, task_id: str, op: str) -> None:
    logger.warning(
        "file_op_denied",
        extra={
            "event": "file_op_denied",
            "bp.agent_id": agent_id,
            "bp.task_id": task_id,
            "op": op,
        },
    )


async def _handle_file_store(
    state: AppState, entry: SocketEntry, frame: FileStoreFrame
) -> None:
    """Bind an uploaded blob to a name. Identity derived from the
    task row (never asserted); quota gated; dedup-allocated; audited.
    Replies the ACTUAL saved name."""
    from bp_router.db import queries  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        scope_t = await _derive_task_scope(conn, frame.task_id, entry.agent_id)
        if scope_t is None:
            _log_file_denied(entry.agent_id, frame.task_id, "store")
            await entry.outbox.put(_file_result(frame, error="denied"))
            return
        user_id, session_id = scope_t
        sq = queries.Scope.user(conn, user_id)

        # The blob must already exist (uploaded via upload-with-grant).
        blob = await sq.get_file_by_sha256(frame.sha256)
        if blob is None:
            await entry.outbox.put(_file_result(frame, error="not_found"))
            return

        filename = frame.filename or blob.original_filename or blob.file_id
        if not _valid_bare_filename(filename):
            await entry.outbox.put(
                _file_result(frame, error="invalid_filename")
            )
            return
        scope = _scope_for(frame.persistent, session_id)

        # Quota gate BEFORE allocation (worst case = full byte_size;
        # an idempotent / shrinking outcome simply won't add).
        # Overwrite of a smaller blob can only shrink, so gating on
        # the full size is conservative-safe.
        existing = await sq.resolve_file_name(scope, filename)
        is_idempotent = existing is not None and existing.file_id == blob.file_id
        worst_add = 0 if is_idempotent else blob.byte_size
        if not await _quota_ok(state, sq, user_id, worst_add):
            await entry.outbox.put(
                _file_result(frame, error="quota_exceeded")
            )
            return

        async with conn.transaction():
            saved, err, _added = await _allocate_name(
                sq, scope=scope, filename=filename, file_id=blob.file_id,
                byte_size=blob.byte_size, dedup=frame.dedup,
            )
            if err is not None:
                await entry.outbox.put(_file_result(frame, error=err))
                return
            await queries.append_audit_event(
                conn,
                actor_kind="agent",
                actor_id=entry.agent_id,
                event="file.store",
                target_kind="file",
                target_id=f"{scope}/{saved}",
                payload={"byte_size": blob.byte_size, "dedup": frame.dedup},
            )
        await entry.outbox.put(
            _file_result(frame, saved_name=_display_name(frame.persistent, saved))
        )


async def _handle_file_fetch(
    state: AppState, entry: SocketEntry, frame: FileFetchFrame
) -> None:
    """Resolve a stash name to an ephemeral signed download URL.
    Read-only — no audit, no quota."""
    from bp_router.db import queries  # noqa: PLC0415
    from bp_router.security.jwt import issue_file_fetch_token  # noqa: PLC0415

    settings = state.settings  # type: ignore[attr-defined]
    parsed = _split_stash_name(frame.name)
    if parsed is None:
        await entry.outbox.put(_file_result(frame, error="invalid_filename"))
        return
    persistent, bare = parsed
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        scope_t = await _derive_task_scope(conn, frame.task_id, entry.agent_id)
        if scope_t is None:
            _log_file_denied(entry.agent_id, frame.task_id, "fetch")
            await entry.outbox.put(_file_result(frame, error="denied"))
            return
        user_id, session_id = scope_t
        sq = queries.Scope.user(conn, user_id)
        row = await sq.resolve_file_name(_scope_for(persistent, session_id), bare)
        if row is None:
            await entry.outbox.put(_file_result(frame, error="not_found"))
            return
        token, exp, _jti = issue_file_fetch_token(
            file_id=row.file_id,
            user_id=user_id,
            secret=settings.jwt_secret.get_secret_value(),
            ttl_s=settings.file_fetch_token_ttl_s,
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    await entry.outbox.put(
        _file_result(
            frame,
            fetch_url=f"/v1/files/{row.file_id}",
            fetch_token=token,
            fetch_expires_at=exp,
        )
    )


async def _handle_file_manage(
    state: AppState, entry: SocketEntry, frame: FileManageFrame
) -> None:
    """Dispatch a typed file-management command (list / delete / copy
    / write) under the task-derived scope."""
    from bp_router.db import queries  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        scope_t = await _derive_task_scope(conn, frame.task_id, entry.agent_id)
        if scope_t is None:
            _log_file_denied(entry.agent_id, frame.task_id, "manage")
            await entry.outbox.put(_file_result(frame, error="denied"))
            return
        user_id, session_id = scope_t
        sq = queries.Scope.user(conn, user_id)
        cmd = frame.command

        if isinstance(cmd, ListFileRequest):
            rows = await sq.list_file_names(
                _scope_for(cmd.persistent, session_id),
                query=cmd.query,
                stored_after=cmd.stored_after,
            )
            names = [_display_name(cmd.persistent, r.filename) for r in rows]
            await entry.outbox.put(_file_result(frame, names=names))
            return

        if isinstance(cmd, DeleteFileRequest):
            # Delete accepts a `*`-glob, so we can't reuse
            # `_split_stash_name` (it rejects nothing but '/'); parse
            # the reserved prefix + reject '/' ourselves, keeping '*'.
            persistent = cmd.name.startswith(_PERSIST_PREFIX)
            bare = cmd.name[len(_PERSIST_PREFIX):] if persistent else cmd.name
            if not bare or "/" in bare:
                await entry.outbox.put(
                    _file_result(frame, error="invalid_filename")
                )
                return
            scope = _scope_for(persistent, session_id)
            async with conn.transaction():
                if "*" in bare:
                    deleted = await sq.delete_file_names_glob(scope, bare)
                else:
                    deleted = await sq.delete_file_name(scope, bare)
                await queries.append_audit_event(
                    conn,
                    actor_kind="agent",
                    actor_id=entry.agent_id,
                    event="file.delete",
                    target_kind="file",
                    target_id=f"{scope}/{bare}",
                    payload={"deleted_count": deleted},
                )
            await entry.outbox.put(_file_result(frame, deleted_count=deleted))
            return

        if isinstance(cmd, CopyFileRequest):
            await _file_copy(state, entry, frame, conn, sq, session_id, cmd)
            return

        if isinstance(cmd, WriteFileRequest):
            await _file_write(state, entry, frame, conn, sq, user_id, session_id, cmd)
            return


async def _file_copy(
    state: AppState,
    entry: SocketEntry,
    frame: FileManageFrame,
    conn: Any,
    sq: Any,
    session_id: str,
    cmd: CopyFileRequest,
) -> None:
    from bp_router.db import queries  # noqa: PLC0415

    src = _split_stash_name(cmd.src)
    dst = _split_stash_name(cmd.dst)
    if src is None or dst is None:
        await entry.outbox.put(_file_result(frame, error="invalid_filename"))
        return
    src_persist, src_name = src
    dst_persist, dst_name = dst
    src_scope = _scope_for(src_persist, session_id)
    dst_scope = _scope_for(dst_persist, session_id)

    src_row = await sq.resolve_file_name(src_scope, src_name)
    if src_row is None:
        await entry.outbox.put(_file_result(frame, error="not_found"))
        return
    # Copy/move onto the SAME name is a no-op — short-circuit BEFORE the
    # delete_original branch. Otherwise `move x→x` allocates idempotently
    # (the name already points at this blob) and then deletes the source,
    # destroying the only name pointing at the file.
    if src_scope == dst_scope and src_name == dst_name:
        await entry.outbox.put(
            _file_result(frame, saved_name=_display_name(dst_persist, dst_name))
        )
        return
    # A move (delete_original) is net-zero bytes; a copy adds the blob
    # size at the destination unless it lands idempotently.
    if not cmd.delete_original:
        dst_existing = await sq.resolve_file_name(dst_scope, dst_name)
        idem = dst_existing is not None and dst_existing.file_id == src_row.file_id
        if not await _quota_ok(
            state, sq, sq._require_user(), 0 if idem else src_row.byte_size
        ):
            await entry.outbox.put(_file_result(frame, error="quota_exceeded"))
            return

    async with conn.transaction():
        saved, err, _added = await _allocate_name(
            sq, scope=dst_scope, filename=dst_name, file_id=src_row.file_id,
            byte_size=src_row.byte_size, dedup="append_count",
        )
        if err is not None:
            await entry.outbox.put(_file_result(frame, error=err))
            return
        if cmd.delete_original:
            await sq.delete_file_name(src_scope, src_name)
        await queries.append_audit_event(
            conn,
            actor_kind="agent",
            actor_id=entry.agent_id,
            event="file.copy",
            target_kind="file",
            target_id=f"{dst_scope}/{saved}",
            payload={"src": cmd.src, "move": cmd.delete_original},
        )
    await entry.outbox.put(
        _file_result(frame, saved_name=_display_name(dst_persist, saved))
    )


async def _file_write(
    state: AppState,
    entry: SocketEntry,
    frame: FileManageFrame,
    conn: Any,
    sq: Any,
    user_id: str,
    session_id: str,
    cmd: WriteFileRequest,
) -> None:
    """Write a text file inline (no upload round-trip): hash the
    UTF-8 bytes, register the blob (content-addressed dedup), then
    allocate the name."""
    import hashlib  # noqa: PLC0415

    from bp_router.db import queries  # noqa: PLC0415
    from bp_router.storage.base import FileMeta  # noqa: PLC0415

    settings = state.settings  # type: ignore[attr-defined]
    if not _valid_bare_filename(cmd.filename):
        await entry.outbox.put(_file_result(frame, error="invalid_filename"))
        return
    data = cmd.text.encode("utf-8")
    if len(data) > settings.max_upload_bytes:
        await entry.outbox.put(_file_result(frame, error="too_large"))
        return
    scope = _scope_for(cmd.persistent, session_id)
    sha = hashlib.sha256(data).hexdigest()

    existing = await sq.resolve_file_name(scope, cmd.filename)
    blob = await sq.get_file_by_sha256(sha)
    is_idem = (
        existing is not None
        and blob is not None
        and existing.file_id == blob.file_id
    )
    if not await _quota_ok(state, sq, user_id, 0 if is_idem else len(data)):
        await entry.outbox.put(_file_result(frame, error="quota_exceeded"))
        return

    file_store = state.file_store  # type: ignore[attr-defined]
    async with conn.transaction():
        if blob is None:
            async def _src() -> Any:
                yield data

            storage_url = await file_store.put(
                sha,
                _src(),
                FileMeta(
                    sha256=sha, byte_size=len(data),
                    mime_type="text/plain; charset=utf-8",
                    original_filename=cmd.filename,
                ),
            )
            blob = await sq.insert_file(
                sha256=sha,
                session_id=None if cmd.persistent else session_id,
                task_id=frame.task_id,
                byte_size=len(data),
                mime_type="text/plain; charset=utf-8",
                storage_url=storage_url,
                original_filename=cmd.filename,
                expires_at=None,
            )
        saved, err, _added = await _allocate_name(
            sq, scope=scope, filename=cmd.filename, file_id=blob.file_id,
            byte_size=len(data), dedup=cmd.dedup,
        )
        if err is not None:
            await entry.outbox.put(_file_result(frame, error=err))
            return
        await queries.append_audit_event(
            conn,
            actor_kind="agent",
            actor_id=entry.agent_id,
            event="file.write",
            target_kind="file",
            target_id=f"{scope}/{saved}",
            payload={"byte_size": len(data), "dedup": cmd.dedup},
        )
    await entry.outbox.put(
        _file_result(frame, saved_name=_display_name(cmd.persistent, saved))
    )
