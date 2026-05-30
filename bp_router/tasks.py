"""bp_router.tasks — Task lifecycle helpers and background loops.

High-level operations on tasks:
- admit_task    — validate + ACL + create row + dispatch
- complete_task — persist Result + propagate to parent
- cancel_task   — recursive cancellation
- fail_task     — terminal-FAILED transition + result fan-out
- timeout_sweep_loop / file_gc_loop — background maintenance
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bp_protocol.frames import (
    CancelFrame,
    NewTaskFrame,
    ResultFrame,
)
from bp_protocol.types import AgentOutput, TaskState, TaskStatus
from bp_router.acl import is_allowed_for
from bp_router.db import queries
from bp_router.delivery import AgentNotConnected, deliver_frame
from bp_router.observability import metrics
from bp_router.state import IllegalTransition, TaskNotFound, task_transition

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _abort_router_side_llm_tasks(
    state: AppState, task_ids: set[str]
) -> int:
    """Cancel every router-side LLM Task whose `_bp_task_id` is in
    `task_ids`. Returns the count cancelled (for diagnostics).

    The Tasks live on `entry.llm_tasks` per live socket; the
    `_bp_task_id` attribute is stamped at `dispatch._handle_llm_request`
    creation time. R8 fix for the cancel-task LLM-leak hazard
    flagged by the fourth-pass review.

    Best-effort: a stuck Task's cancel() may not fire immediately;
    the disconnect path will deal with truly wedged ones. Logging
    helps operators correlate cancel events with provider-cost
    drops.
    """
    cancelled = 0
    if not task_ids:
        return 0
    # R8 perf: O(1) lookup per cancelled task_id via the
    # `state.llm_tasks_by_task_id` index, instead of the pre-R8
    # O(live_sockets × llm_tasks_per_socket) scan of every socket
    # on every cancel (recursive cancel trees + the deadline sweep
    # make cancel frequent). The index is populated + pruned in
    # `dispatch._handle_llm_request`.
    idx = getattr(state, "llm_tasks_by_task_id", None)
    if idx is None:
        return 0
    for tid in task_ids:
        # Snapshot the set — `task.cancel()` schedules the
        # done-callback that mutates this bucket.
        for llm_task in list(idx.get(tid, ())):
            if not llm_task.done():
                llm_task.cancel()
                cancelled += 1
                logger.info(
                    "router_llm_task_cancelled_by_task_cancel",
                    extra={
                        "event": "router_llm_task_cancelled_by_task_cancel",
                        "bp.task_id": tid,
                    },
                )
    return cancelled


def _notify_task_terminal(state: AppState, task_id: str) -> None:
    """Wake any caller awaiting this task's terminal-state event AND
    evict the parent-agent cache entry for this task.

    Called by `complete_task`, `cancel_task`, and `fail_task` AFTER
    their transaction commits, so a concurrent reader who wakes via
    the event sees the terminal row when it queries. Same-worker
    only — a multi-worker deployment whose terminal transition
    happens on a DIFFERENT worker won't fire the listener's local
    event; consumers (`POST /v1/admin/tasks/test`) compensate with
    a coarse fallback poll.

    Also evicts the `caller_agent_cache` entry for this task — a
    terminal task will never emit another Progress frame, so the
    cached fan-out target is dead weight.

    No-op when no listener / cache entry is registered for `task_id`.
    Safe to call unconditionally on every terminal transition.
    """
    events = getattr(state, "task_terminal_events", None)
    if events is not None:
        event = events.pop(task_id, None)
        if event is not None:
            event.set()
    cache = getattr(state, "caller_agent_cache", None)
    if cache is not None:
        cache.pop(task_id, None)


def _cache_caller_agent(
    state: AppState,
    task_id: str,
    caller_agent_id: str,
    active_agent_id: str,
) -> None:
    """Cache `(caller_agent_id, active_agent_id)` for this task.

    `caller_agent_id` is the fan-out target for Progress / Result
    (always a real agent — channel agents for root tasks, the
    parent's destination for children, the synthetic `admin_console`
    for admin-tested tasks). `active_agent_id` is the agent currently
    authorised to EMIT for this task (its executor); a Progress whose
    sender is not the active agent is dropped (see `_handle_progress`)
    — the same authz rule `complete_task` enforces for Results. It
    rides in the cache so the per-frame authz stays O(1) at line
    rate; delegation keeps it correct via `_recache_active_agent`.
    """
    cache = getattr(state, "caller_agent_cache", None)
    if cache is None:
        return
    cache[task_id] = (caller_agent_id, active_agent_id)


def _recache_active_agent(
    state: AppState, task_id: str, active_agent_id: str
) -> None:
    """Update the cached active (emit-authorised) agent after a
    delegation reassignment, preserving the cached caller. If the
    task isn't cached (evicted / never cached on this worker)
    `_handle_progress` falls back to a fresh DB read, so a missing
    entry is safe — do NOT insert one here (it could resurrect an
    entry `_notify_task_terminal` deliberately evicted)."""
    cache = getattr(state, "caller_agent_cache", None)
    if cache is None:
        return
    entry = cache.get(task_id)
    if entry is not None:
        cache[task_id] = (entry[0], active_agent_id)


# ---------------------------------------------------------------------------
# Results / errors surfaced to dispatch
# ---------------------------------------------------------------------------


@dataclass
class AdmitResult:
    """What `admit_task` hands back to `_handle_new_task`.

    `task_id` is always the assigned/joined task id (ack payload).

    `replay_result` is set ONLY on an idempotency hit whose
    existing task is already TERMINAL: the original terminal
    `ResultFrame` was fanned out exactly once (to the request that
    created the task) and will never be emitted again, so a retry
    with the same idempotency_key would otherwise hang to its spawn
    timeout awaiting a frame that can't come. `_handle_new_task`
    re-emits this reconstructed frame after the ack; the SDK's
    `PendingMap` early-resolve buffer makes the ack→register→result
    ordering safe in both directions. None on every non-idempotent
    path and on an idempotency hit that is still in-flight (the
    retry correctly joins the live task)."""

    task_id: str
    replay_result: ResultFrame | None = None


_STATUS_FROM_STATE: dict[TaskState, TaskStatus] = {
    TaskState.SUCCEEDED: TaskStatus.SUCCEEDED,
    TaskState.FAILED: TaskStatus.FAILED,
    TaskState.CANCELLED: TaskStatus.CANCELLED,
    TaskState.TIMED_OUT: TaskStatus.TIMED_OUT,
}

# Faithful status_code for an idempotent replay whose stored
# `tasks.status_code` is NULL. Router-synthesised terminals leave
# the column NULL — notably CANCELLED: `cancel_task` calls
# `task_transition(... CANCELLED ...)` with no status_code, and
# `task_transition`'s `COALESCE($4, status_code)` keeps the
# pre-existing NULL — yet the ORIGINAL one-shot fan-out delivered
# a status-appropriate code (`cancel_task` sends 499). `fail_task`
# / agent SUCCEEDED|FAILED always persist a code, so in practice
# only CANCELLED reaches the NULL branch; the other three are
# defensive so the map is total over the terminal set (a new
# terminal status surfaces as a loud KeyError, not a silent 0).
# Mirrors the codes the original synthetic fan-outs use (499
# cancel, 504 deadline) so the replay matches what the original
# caller saw rather than the old `… or 0` artifact.
_DEFAULT_TERMINAL_STATUS_CODE: dict[TaskStatus, int] = {
    TaskStatus.SUCCEEDED: 200,
    TaskStatus.FAILED: 500,
    TaskStatus.CANCELLED: 499,
    TaskStatus.TIMED_OUT: 504,
}


def _status_from_state(state: TaskState) -> TaskStatus | None:
    """Inverse of `_state_from_status`. Returns None for a
    non-terminal state (no terminal Result to replay)."""
    return _STATUS_FROM_STATE.get(state)


def _safe_rehydrate_output(stored: object) -> AgentOutput | None:
    """Rehydrate a stored `output` blob into `AgentOutput`.

    `complete_task` always persists `AgentOutput.model_dump()`, so
    well-formed in normal operation. But a malformed/legacy/hand-
    edited blob would make `model_validate` raise `ValidationError`,
    which on the idempotent-replay path propagates out of
    `admit_task` and turns the replay into an `internal_error` ack
    — defeating the retry. Treat an un-parseable blob as "no
    output" so the terminal status/error still replay faithfully.
    """
    if stored is None:
        return None
    try:
        return AgentOutput.model_validate(stored)
    except Exception:  # noqa: BLE001
        logger.warning(
            "idempotent_replay_malformed_output",
            extra={"event": "idempotent_replay_malformed_output"},
        )
        return None


def _idempotent_admit_result(
    existing: Any, frame: NewTaskFrame
) -> AdmitResult:
    """Build the `AdmitResult` for an idempotency hit on `existing`.

    Terminal → reconstruct the one-shot terminal `ResultFrame` so
    `_handle_new_task` can replay it (the original fan-out happened
    exactly once, to the request that created the task; a retry
    would otherwise hang to its spawn timeout). Non-terminal → just
    return the id so the retry joins the live task. Used both by
    the up-front idempotency lookup AND by the concurrent-INSERT
    race handler so the two paths can't diverge.
    """
    replay_status = _status_from_state(existing.state)
    if replay_status is not None:
        replay = ResultFrame(
            agent_id=existing.agent_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            task_id=existing.task_id,
            parent_task_id=existing.parent_task_id,
            status=replay_status,
            status_code=(
                existing.status_code
                if existing.status_code is not None
                else _DEFAULT_TERMINAL_STATUS_CODE[replay_status]
            ),
            output=_safe_rehydrate_output(existing.output),
            error=existing.error,
        )
        return AdmitResult(task_id=existing.task_id, replay_result=replay)
    # Non-terminal (RUNNING / WAITING_CHILDREN): genuinely
    # in-flight. Return the id so the retry joins the live task —
    # its eventual terminal Result fans out normally.
    return AdmitResult(task_id=existing.task_id)


class AdmitError(Exception):
    """Wraps an ACL/quota/validation refusal at admission time."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # Populated by `quota_exceeded` so the admin API can emit a
        # `Retry-After` header. None for every other error code.
        self.retry_after_s = retry_after_s


# ---------------------------------------------------------------------------
# Admission (NewTask → tasks row → dispatch)
# ---------------------------------------------------------------------------


async def _admit_delegation(
    state: AppState,
    frame: NewTaskFrame,
    *,
    caller_agent_id: str,
) -> str:
    """Delegation branch of `admit_task`.

    The caller must be the task's current `active_agent_id`. ACL is
    checked on `(caller, destination)` just like spawn. The outbound
    NewTaskFrame carries `delegating_agent_id=caller_agent_id` so the
    destination handler can branch on `ctx.delegating_agent_id`.

    Ordering:
      1. SELECT FOR UPDATE the task row (also serialises vs cancel).
      2. Validate caller, destination, ACL.
      3. Dispatch to the new destination and AWAIT its ack.
      4. On accept: atomic UPDATE of `active_agent_id` guarded by the
         caller's view, plus an `agent.delegated` audit row, plus an
         `agent.delegated` task_event.
      5. On reject: raise `AdmitError`. The task row is unchanged;
         the original L0 remains the active executor.

    The "ack before flip" order matters: if L1 won't accept and we'd
    flipped first, the task would be stuck pointing at an agent that
    never received it.
    """
    assert frame.task_id is not None

    pool = state.db_pool  # type: ignore[attr-defined]

    # Lookup caller / destination outside the transaction; agent rows
    # don't move under load and locking them isn't necessary.
    async with pool.acquire() as conn:
        caller_row = await queries.get_agent(conn, caller_agent_id)
        callee_row = await queries.get_agent(conn, frame.destination_agent_id)
    if caller_row is None:
        raise AdmitError("agent_not_found", f"caller '{caller_agent_id}' unknown")
    if callee_row is None:
        raise AdmitError(
            "agent_not_found",
            f"destination '{frame.destination_agent_id}' unknown",
        )
    if callee_row.status != "active":
        raise AdmitError(
            "agent_not_found",
            f"destination '{frame.destination_agent_id}' not active",
        )

    level = await _session_level(state, frame.user_id)
    if level is None:
        raise AdmitError(
            "user_unknown",
            f"user '{frame.user_id}' is unknown or suspended",
        )

    decision = is_allowed_for(
        state.rules.rules,  # type: ignore[attr-defined]
        caller_id=caller_row.agent_id,
        caller_groups=caller_row.groups,
        caller_capabilities=caller_row.capabilities,
        callee_id=callee_row.agent_id,
        callee_groups=callee_row.groups,
        callee_capabilities=callee_row.capabilities,
        user_level=level,
    )
    if not decision.allow:
        raise AdmitError(
            "acl_denied",
            f"caller '{caller_agent_id}' may not delegate to "
            f"'{frame.destination_agent_id}' (rule={decision.rule_name})",
        )

    # Phase A — validate under a SHORT-LIVED FOR UPDATE lock.
    #
    # Pre-R8 the whole validate → dispatch → flip ran inside ONE
    # transaction that held the `SELECT … FOR UPDATE` row lock across
    # the up-to-`pending_ack_timeout_s` (default 30s)
    # `deliver_frame(await_ack=True)`. With the default 10-conn pool,
    # ~10 concurrent delegations to slow/dead destinations exhausted
    # the entire pool and stalled every other router DB operation;
    # the held row lock also blocked concurrent cancel/complete on
    # the task. Spawn (`admit_task`) has no such problem — it creates
    # a brand-new row, commits + releases the connection, then awaits
    # the ack connectionless. Delegation can't just copy that because
    # it mutates an existing shared row and needs FOR UPDATE to
    # serialise the read-validate-flip; a Postgres row lock IS the
    # transaction IS the connection.
    #
    # So: validate (short lock) → release → ack (no conn) → re-lock
    # → re-validate → flip. The re-validation only needs to re-check
    # "still non-terminal, still mine?" because `_recv_loop`
    # processes a socket's frames sequentially (the delegating
    # executor is frozen for the whole of `_admit_delegation`) and
    # the active-executor check means no other agent can delegate
    # this task — so the ONLY concurrent mutation possible during
    # the ack window is `cancel_task` / deadline-timeout.
    async with pool.acquire() as conn:
        async with conn.transaction():
            scope = queries.Scope.user(conn, frame.user_id)
            task_row = await scope.lock_task_for_delegation(frame.task_id)
            if task_row is None:
                raise AdmitError(
                    "task_unknown",
                    f"task '{frame.task_id}' not found in caller's user scope",
                )
            if task_row.state in (
                TaskState.SUCCEEDED, TaskState.FAILED,
                TaskState.CANCELLED, TaskState.TIMED_OUT,
            ):
                raise AdmitError(
                    "task_terminal",
                    f"task '{frame.task_id}' is already in terminal state "
                    f"{task_row.state.value}",
                )
            if task_row.active_agent_id != caller_agent_id:
                raise AdmitError(
                    "not_active_executor",
                    f"caller '{caller_agent_id}' is not the active executor "
                    f"for task '{frame.task_id}' "
                    f"(current active: '{task_row.active_agent_id}')",
                )

            # Cycle / depth check — Phase A ONLY. The delegation
            # history can't change during the ack window: concurrent
            # same-task delegation is impossible (the delegating
            # executor's socket is frozen) and cancel/timeout don't
            # append `delegated` events. Re-running it in Phase C
            # would be redundant work.
            max_depth = state.settings.task_delegation_max_depth  # type: ignore[attr-defined]
            prior_destinations = await scope.list_delegation_destinations(
                task_row.task_id, limit=max_depth + 1,
            )
            if len(prior_destinations) >= max_depth:
                raise AdmitError(
                    "delegation_depth_exceeded",
                    f"task '{task_row.task_id}' has already been delegated "
                    f"{len(prior_destinations)} times (max {max_depth}); "
                    f"refusing further hops to avoid runaway chain growth",
                )
            # `task_row.agent_id` is the ORIGINAL active executor
            # (set at task creation). Every prior delegation's `to`
            # has also been active. If the new destination is in
            # that set, this delegation re-enters an agent the task
            # already passed through — a cycle by definition.
            visited = {task_row.agent_id, *prior_destinations}
            if frame.destination_agent_id in visited:
                raise AdmitError(
                    "delegation_cycle",
                    f"delegation to '{frame.destination_agent_id}' would "
                    f"create a cycle; task '{task_row.task_id}' has already "
                    f"passed through that agent",
                )

            # Capture the immutable fields Phase B/C need. task_id,
            # parent_task_id, priority, deadline never change for a
            # task once created, so reading them here (before the
            # lock is released) is safe.
            task_id = task_row.task_id
            parent_task_id = task_row.parent_task_id
            priority = task_row.priority
            deadline = task_row.deadline
    # conn + FOR UPDATE lock released here — the Phase A transaction
    # was read-only (two SELECTs), so commit just drops the lock and
    # returns the connection to the pool. Nothing to roll back.

    # Phase B — dispatch + await the destination's ack with NO
    # connection held. This is the up-to-30s wait that previously
    # starved the pool. The error handlers below just raise
    # `AdmitError`; Phase A made no mutations so there is nothing to
    # undo.
    delivery_frame = NewTaskFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        destination_agent_id=frame.destination_agent_id,
        user_id=frame.user_id,
        user_level=level,
        session_id=frame.session_id,
        priority=priority,
        deadline=deadline,
        payload=frame.payload,
        delegating_agent_id=caller_agent_id,
        input_mode=frame.input_mode,
    )
    try:
        ack = await deliver_frame(
            state,
            frame.destination_agent_id,
            delivery_frame,
            await_ack=True,
            timeout_s=state.settings.pending_ack_timeout_s,  # type: ignore[attr-defined]
        )
    except AgentNotConnected as exc:
        raise AdmitError(
            "agent_disconnected",
            "destination agent has no live socket",
        ) from exc
    except TimeoutError as exc:
        raise AdmitError(
            "ack_timeout",
            "destination agent did not ack delegation in time",
        ) from exc

    if ack is not None and not ack.accepted:
        raise AdmitError(
            "rejected",
            ack.reason or "destination rejected the delegation",
        )

    # Phase C — re-acquire, re-lock, re-validate, flip. A fresh
    # FOR UPDATE serialises the flip the same way Phase A's lock did,
    # but only for these few fast statements rather than the whole
    # ack wait.
    async with pool.acquire() as conn:
        async with conn.transaction():
            scope = queries.Scope.user(conn, frame.user_id)
            task_row = await scope.lock_task_for_delegation(task_id)
            if task_row is None:
                # Defensive — a task can't be deleted, but a
                # cross-user scope drift would land here too.
                raise AdmitError(
                    "task_unknown",
                    f"task '{task_id}' not found in caller's user scope",
                )
            if task_row.state in (
                TaskState.SUCCEEDED, TaskState.FAILED,
                TaskState.CANCELLED, TaskState.TIMED_OUT,
            ):
                # A cancel / deadline-timeout landed while the
                # destination was being asked to accept. Refuse —
                # the task is gone. Accepted tradeoff: the
                # destination already acked and now has an orphaned
                # execution; the deadline sweep reaps it (TIMED_OUT).
                # Strictly better than holding a pooled connection +
                # row lock for the entire 30s ack window.
                raise AdmitError(
                    "task_terminal",
                    f"task '{task_id}' reached terminal state "
                    f"{task_row.state.value} while the destination was "
                    f"being asked to accept the delegation",
                )
            if task_row.active_agent_id != caller_agent_id:
                # Defensive: per the concurrency model this can't
                # happen (no concurrent delegation possible). The
                # reassign WHERE-guard below also backstops it.
                raise AdmitError(
                    "not_active_executor",
                    f"caller '{caller_agent_id}' is no longer the active "
                    f"executor for task '{task_id}' "
                    f"(current active: '{task_row.active_agent_id}')",
                )

            flipped = await scope.reassign_active_agent(
                task_id,
                new_active_agent_id=frame.destination_agent_id,
                expected_current_agent_id=caller_agent_id,
            )
            if not flipped:
                # The re-lock + re-validate should have prevented
                # this; treat as internal_error so we don't silently
                # swallow.
                raise AdmitError(
                    "internal_error",
                    "active_agent_id reassignment lost the optimistic "
                    "concurrency check; investigate",
                )
            # The executor changed: the delegate is now the only
            # agent authorised to emit Progress/Result for this task.
            # Keep the per-frame authz cache correct so the delegate's
            # Progress is fanned out and the old agent's is dropped.
            _recache_active_agent(
                state, task_id, frame.destination_agent_id
            )
            await scope.insert_task_event(
                task_id=task_id,
                kind="delegated",
                actor_agent_id=caller_agent_id,
                payload={
                    "from": caller_agent_id,
                    "to": frame.destination_agent_id,
                },
            )
            await queries.append_audit_event(
                conn,
                actor_kind="agent",
                actor_id=caller_agent_id,
                event="task.delegated",
                target_kind="task",
                target_id=task_id,
                payload={
                    "from": caller_agent_id,
                    "to": frame.destination_agent_id,
                },
            )

    return task_id



async def admit_task(
    state: AppState,
    frame: NewTaskFrame,
    *,
    caller_agent_id: str,
) -> str:
    """Validate, ACL-check, persist, and dispatch a new task.

    Returns the assigned task_id. For idempotent retries (matching
    `idempotency_key`), the existing task_id is returned and no new
    work is scheduled.

    Delegation: when `frame.task_id` is set, this is a hand-off of an
    existing task. The caller must be the task's current
    `active_agent_id`; on success the column is atomically reassigned
    to the new destination. No new task row is created. See
    `_admit_delegation` for the dedicated path.
    """
    if frame.task_id is not None:
        return AdmitResult(
            task_id=await _admit_delegation(
                state, frame, caller_agent_id=caller_agent_id
            )
        )

    import asyncpg  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]

    # 1. Idempotency lookup.
    if frame.idempotency_key:
        async with pool.acquire() as conn:
            existing = await queries.Scope.user(
                conn, frame.user_id
            ).find_idempotent(
                frame.idempotency_key, caller_agent_id=caller_agent_id
            )
        if existing is not None:
            # The retry FOUND the row (it was created + committed
            # before this lookup). Reconstruct the terminal replay
            # or join the live task — see `_idempotent_admit_result`.
            return _idempotent_admit_result(existing, frame)

    # 2. Look up caller and callee agent rows.
    async with pool.acquire() as conn:
        caller_row = await queries.get_agent(conn, caller_agent_id)
        callee_row = await queries.get_agent(conn, frame.destination_agent_id)
    if caller_row is None:
        raise AdmitError("agent_not_found", f"caller '{caller_agent_id}' unknown")
    if callee_row is None:
        raise AdmitError(
            "agent_not_found",
            f"destination '{frame.destination_agent_id}' unknown",
        )
    if callee_row.status != "active":
        raise AdmitError(
            "agent_not_found",
            f"destination '{frame.destination_agent_id}' not active",
        )

    # 3a. Per-mode payload schema validation. `accepts_schema` is now
    #     `{input_mode: <JSON Schema>|null}`. Pick the schema for
    #     `frame.input_mode`; reject an unknown / ambiguous mode HERE
    #     (fail fast — no task row) rather than letting it slip to a
    #     `no_handler` ack. `null` for a mode == the dict-input
    #     escape hatch: admitted without payload validation, but the
    #     mode must still be a known key. A `None` map (no published
    #     schema at all — legacy / operator-cleared) skips validation
    #     entirely; the consumer resolves the sole handler.
    schemas = (callee_row.agent_info or {}).get("accepts_schema")
    schema_for_admit: Any = None
    if isinstance(schemas, dict) and schemas:
        mode = frame.input_mode
        if mode is None:
            if len(schemas) == 1:
                schema_for_admit = next(iter(schemas.values()))
            else:
                raise AdmitError(
                    "schema_mismatch",
                    "destination exposes multiple modes "
                    f"({sorted(schemas)}); NewTaskFrame.input_mode is "
                    "required",
                )
        elif mode not in schemas:
            raise AdmitError(
                "schema_mismatch",
                f"unknown input_mode {mode!r}; destination modes: "
                f"{sorted(schemas)}",
            )
        else:
            schema_for_admit = schemas[mode]
    if schema_for_admit:
        try:
            import jsonschema  # noqa: PLC0415

            jsonschema.validate(instance=frame.payload, schema=schema_for_admit)
        except jsonschema.ValidationError as exc:
            raise AdmitError(
                "schema_mismatch",
                f"payload does not match destination's accepts_schema"
                f"[{frame.input_mode!r}]: {exc.message}",
            ) from exc
        except jsonschema.SchemaError as exc:
            # The destination's schema itself is malformed — that's a
            # router/admin-time bug, not a caller error. Surface it as
            # internal_error.
            raise AdmitError(
                "internal_error",
                f"destination's accepts_schema[{frame.input_mode!r}] is "
                f"invalid: {exc.message}",
            ) from exc

    # 3. ACL permission check.
    level = await _session_level(state, frame.user_id)
    if level is None:
        raise AdmitError(
            "user_unknown",
            f"user '{frame.user_id}' is unknown or suspended",
        )

    # 3b. Session-pair consistency. Two checks in one query:
    #     (a) the (user_id, session_id) pair points at a real row, so
    #         an agent can't tag a task as user A but route it through
    #         user B's session;
    #     (b) the session is still open. Closed sessions stop accepting
    #         new tasks; agents that see `session_closed` should drop
    #         their state and let the BFF open a fresh session.
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT closed_at FROM sessions "
            "WHERE session_id = $1 AND user_id = $2",
            frame.session_id,
            frame.user_id,
        )
    if row is None:
        raise AdmitError(
            "session_unknown",
            f"session '{frame.session_id}' not found "
            f"or does not belong to user '{frame.user_id}'",
        )
    if row["closed_at"] is not None:
        raise AdmitError(
            "session_closed",
            f"session '{frame.session_id}' is closed; open a new session",
        )

    decision = is_allowed_for(
        state.rules.rules,  # type: ignore[attr-defined]
        caller_id=caller_row.agent_id,
        caller_groups=caller_row.groups,
        caller_capabilities=caller_row.capabilities,
        callee_id=callee_row.agent_id,
        callee_groups=callee_row.groups,
        callee_capabilities=callee_row.capabilities,
        user_level=level,
    )
    if not decision.allow:
        raise AdmitError(
            "acl_denied",
            f"caller '{caller_agent_id}' may not invoke "
            f"'{frame.destination_agent_id}' (rule={decision.rule_name})",
        )

    # 3c. Per-user admit-rate token bucket.
    #
    # See `docs/design/quota-enforcement.md` §7 for the design. Bucket
    # is per-`(user_id, level)` so two users at the same tier don't
    # share a bucket. `None` rate = no cap (admin / service defaults);
    # short-circuit before touching the bucket helper so the no-cap
    # path doesn't pay for a Redis round-trip.
    rate = state.settings.quota_admit_rate_per_s.get(level)  # type: ignore[attr-defined]
    burst = state.settings.quota_admit_burst.get(level)  # type: ignore[attr-defined]
    if rate is not None and burst is not None:
        bucket = state.admit_quota  # type: ignore[attr-defined]
        d = await bucket.try_consume(
            f"{frame.user_id}:{level}",
            rate_per_s=rate,
            burst=burst,
        )
        if not d.allowed:
            metrics.quota_exceeded_total.labels(
                counter="admit_rate", level=level
            ).inc()
            raise AdmitError(
                "quota_exceeded",
                f"per-user admit rate exceeded "
                f"(level={level}, retry after {d.retry_after_s:.2f}s)",
                retry_after_s=d.retry_after_s,
            )

    # 3d. Spawn-depth cap. Without this, agent
    # A → B → A → ... is unbounded and a runaway recursion bug or
    # adversarial topology would exhaust the DB connection pool,
    # the per-socket WS outbox, and the `tasks` row count.
    # `count_task_chain_depth` walks the parent chain via the same
    # recursive-CTE pattern (user-scoped + depth-bounded) used in
    # `list_descendants` / `task_has_ancestor_with_agent`. Cap is
    # configurable via `Settings.spawn_max_depth` (default 16);
    # the new task would be at depth `parent_depth + 1`.
    if frame.parent_task_id is not None:
        max_depth = state.settings.spawn_max_depth  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            parent_depth = await queries.count_task_chain_depth(
                conn,
                task_id=frame.parent_task_id,
                user_id=frame.user_id,
            )
        if parent_depth >= max_depth:
            raise AdmitError(
                "spawn_depth_exceeded",
                f"spawn would exceed max depth "
                f"({parent_depth + 1} > {max_depth}); "
                "agents may have entered a recursion cycle",
            )

    # 4. Persist task row + initial event.
    deadline = frame.deadline or (
        _now()
        + timedelta(seconds=state.settings.default_task_deadline_s)  # type: ignore[attr-defined]
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Re-check session validity INSIDE the transaction with
            # `FOR UPDATE` to close the TOCTOU window. The pre-flight
            # check at step 3b is fast
            # and preserves error-precedence ordering, but it runs
            # on a separate connection — between that read and this
            # insert, a concurrent `DELETE /v1/sessions/{id}` could
            # have closed the session. The FK on `tasks.session_id`
            # only checks row existence, NOT `closed_at IS NULL`, so
            # without this re-check a task could land in a closed
            # session and persist there until it terminates. The
            # `FOR UPDATE` row lock serialises admit-vs-close on
            # the same session_id; concurrent admits on different
            # sessions don't contend.
            sess_row = await conn.fetchrow(
                "SELECT closed_at FROM sessions "
                "WHERE session_id = $1 AND user_id = $2 "
                "FOR UPDATE",
                frame.session_id,
                frame.user_id,
            )
            if sess_row is None:
                raise AdmitError(
                    "session_unknown",
                    f"session '{frame.session_id}' not found "
                    f"or does not belong to user '{frame.user_id}'",
                )
            if sess_row["closed_at"] is not None:
                raise AdmitError(
                    "session_closed",
                    f"session '{frame.session_id}' was closed "
                    "between pre-flight and admit; open a new session",
                )

            try:
                task_row = await queries.Scope.user(
                    conn, frame.user_id
                ).create_task(
                    session_id=frame.session_id,
                    agent_id=frame.destination_agent_id,
                    caller_agent_id=caller_agent_id,
                    parent_task_id=frame.parent_task_id,
                    priority=frame.priority,
                    deadline=deadline,
                    idempotency_key=frame.idempotency_key,
                    input=frame.payload,
                )
            except queries.CrossUserTaskAccess as exc:
                # `parent_task_id` either doesn't exist or belongs to
                # a different user. Either way it's a
                # caller-supplied bad reference; refuse the admit with
                # the same `AdmitError` shape as other validation
                # failures rather than letting an asyncpg ForeignKey
                # violation bubble up.
                raise AdmitError(
                    "invalid_parent_task",
                    f"parent_task_id {exc.task_id!r} not found in caller's tree",
                ) from exc
            except asyncpg.UniqueViolationError as exc:
                # A concurrent same-(user_id, idempotency_key) admit
                # WON the race: it created + committed the row in the
                # window between this request's `find_idempotent`
                # miss (step 1) and this INSERT. The canonical
                # post-ack_timeout retry legitimately lands on a
                # second socket / worker, so the two admits don't
                # serialise. Pre-fix the raw `UniqueViolationError`
                # propagated to `_handle_new_task`'s bare except →
                # `internal_error` ack — defeating idempotency in
                # exactly the retry race it exists for. Resolve
                # against the winner's row exactly as step 1 would
                # have (terminal replay / live-task join). Only the
                # idempotency constraint is handled; any other
                # unique violation is a real bug — re-raise. The
                # current `conn`'s transaction is aborted by the
                # violation, so the re-lookup uses a FRESH pooled
                # connection.
                if (
                    getattr(exc, "constraint_name", None)
                    != "tasks_idempotency_unique"
                ):
                    raise
                async with pool.acquire() as conn2:
                    racer = await queries.Scope.user(
                        conn2, frame.user_id
                    ).find_idempotent(
                        frame.idempotency_key, caller_agent_id=caller_agent_id
                    )
                if racer is None:
                    # Defensive: the constraint fired but the row
                    # isn't visible (should be impossible — the
                    # winner committed before releasing the lock the
                    # constraint enforces). Surface the original.
                    raise
                return _idempotent_admit_result(racer, frame)
            await queries.Scope.user(conn, frame.user_id).insert_task_event(
                task_id=task_row.task_id,
                kind="admitted",
                actor_agent_id=caller_agent_id,
                payload={"caller": caller_agent_id},
            )

    # Populate the caller-agent cache so subsequent ProgressFrames
    # from this task fan-out without a per-frame DB lookup. Unlike
    # the previous `parent_agent` cache, this is unconditional —
    # `caller_agent_id` is always a real agent id (root tasks point
    # at the channel agent / admin_console synthetic), so the
    # fan-out target is well-defined.
    _cache_caller_agent(
        state, task_row.task_id, caller_agent_id, frame.destination_agent_id
    )

    # 5. Dispatch the NewTask frame to the destination's socket.
    delivery_frame = NewTaskFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        task_id=task_row.task_id,
        parent_task_id=task_row.parent_task_id,
        destination_agent_id=frame.destination_agent_id,
        user_id=frame.user_id,
        user_level=level,
        session_id=frame.session_id,
        priority=frame.priority,
        deadline=task_row.deadline,
        payload=frame.payload,
        input_mode=frame.input_mode,
    )

    async def _safe_fail(*, status_code: int, reason: str, error: dict) -> None:
        """Best-effort terminal transition on an admission failure.

        `fail_task` can itself raise (pool exhausted, DB blip). If it
        propagated, the `raise AdmitError` below would be skipped,
        `_handle_new_task` would catch the bare exception and ack a
        generic `internal_error`, and the QUEUED row (created
        earlier) would never transition — a zombie occupying
        spawn-depth budget until the deadline sweep. Swallow the
        `fail_task` error (logged) so the AdmitError ALWAYS
        propagates: the caller's correct admission-failed ack is
        what matters for liveness, and the deadline sweep is the
        backstop for the un-transitioned row."""
        try:
            await fail_task(
                state,
                task_row.task_id,
                user_id=frame.user_id,
                status_code=status_code,
                reason=reason,
                error=error,
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "admit_failure_fail_task_errored",
                extra={
                    "event": "admit_failure_fail_task_errored",
                    "bp.task_id": task_row.task_id,
                    "reason": reason,
                },
            )
        # `fail_task` raised. Best-effort single-statement fallback so
        # the row doesn't linger QUEUED — a never-dispatched zombie
        # would (a) be handed back by a later `find_idempotent` retry
        # with no terminal Result to replay, hanging that caller, and
        # (b) keep consuming `spawn_max_depth` budget until the
        # deadline sweep. Guarded so it can only fail a still-non-
        # terminal row. Swallow its own errors — this is already the
        # degraded path; the AdmitError still propagates regardless.
        try:
            async with pool.acquire() as conn:
                forced = await queries.Scope.user(
                    conn, frame.user_id
                ).force_fail_task(
                    task_row.task_id,
                    status_code=status_code,
                    error=error,
                )
            logger.warning(
                "admit_failure_force_fail_fallback",
                extra={
                    "event": "admit_failure_force_fail_fallback",
                    "bp.task_id": task_row.task_id,
                    "forced": forced,
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "admit_failure_force_fail_errored",
                extra={
                    "event": "admit_failure_force_fail_errored",
                    "bp.task_id": task_row.task_id,
                },
            )

    try:
        ack = await deliver_frame(
            state,
            frame.destination_agent_id,
            delivery_frame,
            await_ack=True,
            timeout_s=state.settings.pending_ack_timeout_s,  # type: ignore[attr-defined]
        )
    except AgentNotConnected as exc:
        await _safe_fail(
            status_code=503,
            reason="agent_disconnected",
            error={"code": "agent_disconnected"},
        )
        raise AdmitError(
            "agent_disconnected", "destination agent has no live socket"
        ) from exc
    except TimeoutError as exc:
        await _safe_fail(
            status_code=504,
            reason="ack_timeout",
            error={"code": "ack_timeout"},
        )
        raise AdmitError(
            "ack_timeout", "destination agent did not ack in time"
        ) from exc

    if ack is not None and not ack.accepted:
        # R8 (HIGH): defensive bound on the destination's reason
        # before reflecting back into the caller's Ack. The
        # `AckFrame.reason` field is now `max_length=256` at the
        # protocol level, but we slice here too to surface a
        # clean truncated string (not a Pydantic ValidationError)
        # when an old client sends a longer reason that we
        # already accepted before the validation tightened. The
        # 240/200 split leaves room for the wrapping `"rejected: "`
        # / similar prefix the AdmitError adds.
        bounded_reason = (ack.reason or "rejected")[:240]
        await _safe_fail(
            status_code=400,
            reason=bounded_reason,
            error={"code": "rejected", "reason": bounded_reason},
        )
        raise AdmitError("rejected", bounded_reason or "destination rejected the task")

    # 6. Transition QUEUED → RUNNING (the agent has accepted).
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                await task_transition(
                    conn,
                    task_row.task_id,
                    TaskState.RUNNING,
                    reason="agent_accepted",
                    actor_agent_id=frame.destination_agent_id,
                )
            except (IllegalTransition, TaskNotFound):
                # Result may already have arrived for very fast handlers;
                # tolerate.
                pass

    return AdmitResult(task_id=task_row.task_id)


# ---------------------------------------------------------------------------
# Completion / fan-out
# ---------------------------------------------------------------------------


async def complete_task(
    state: AppState,
    frame: ResultFrame,
    *,
    reporting_agent_id: str,
) -> None:
    """Persist a Result and forward it to the caller agent."""
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Look up task to find user_id + caller + active executor.
            #
            # `FOR UPDATE` inside the SAME transaction as the
            # `task_transition` below. Pre-R9 this read ran in
            # autocommit BEFORE the transaction opened, so the
            # `reporting_agent_id != active_agent_id` auth check used
            # an UNLOCKED snapshot. `_admit_delegation` Phase C also
            # takes `SELECT … FOR UPDATE` on this row to flip
            # `active_agent_id`; with the old unlocked read, a flip
            # committing between this read and the transition dropped
            # a legitimate Result from the new active executor and
            # the task hung until the deadline sweep (R8 PR #196
            # only made it countable; this closes the race). With
            # the lock, the two serialise: either `complete_task`
            # locks first (Phase C then re-reads, sees the task is
            # now terminal, and correctly refuses the delegation), or
            # Phase C locks first (flip commits, this read sees the
            # post-flip `active_agent_id`, auth passes, Result lands).
            row = await conn.fetchrow(
                "SELECT user_id, parent_task_id, caller_agent_id, "
                "       agent_id, active_agent_id "
                "FROM tasks WHERE task_id = $1 FOR UPDATE",
                frame.task_id,
            )
            if row is None:
                logger.warning(
                    "result_for_unknown_task",
                    extra={
                        "event": "result_for_unknown_task",
                        "bp.task_id": frame.task_id,
                    },
                )
                return
            caller_agent_id = row["caller_agent_id"]
            owning_agent_id = row["agent_id"]
            active_agent_id = row["active_agent_id"]
            parent_task_id = row["parent_task_id"]

            # Auth: the agent currently executing the task is the
            # only one allowed to emit a terminal Result. Under
            # delegation, the L1 that received the task via
            # DelegationFrame becomes the active executor — the old
            # "compare against destination" check silently dropped a
            # legitimate terminal Result in that case.
            if reporting_agent_id != active_agent_id:
                # Observability for the otherwise-silent drop.
                # `owning` = the original task agent_id reporting
                # late after a legitimate delegation hand-off
                # (benign — the delegate produces the real Result).
                # `other` = any other mismatch. With the FOR UPDATE
                # read above, the previously-racy delegation window
                # is closed, so a sustained `other` rate now
                # indicates a genuinely misbehaving reporter rather
                # than the flip race.
                metrics.result_from_wrong_agent_total.labels(
                    reporter=(
                        "owning"
                        if reporting_agent_id == owning_agent_id
                        else "other"
                    )
                ).inc()
                logger.warning(
                    "result_from_wrong_agent",
                    extra={
                        "event": "result_from_wrong_agent",
                        "bp.task_id": frame.task_id,
                        "expected_active": active_agent_id,
                        "expected_owning": owning_agent_id,
                        "actual": reporting_agent_id,
                    },
                )
                return

            new_state = _state_from_status(frame.status)
            # Already inside the FOR UPDATE transaction opened above —
            # the state read, the auth check, and this transition are
            # one atomic unit (the whole point of the R9 fix). No
            # nested `conn.transaction()` (it would just be a
            # redundant savepoint).
            try:
                await task_transition(
                    conn,
                    frame.task_id,
                    new_state,
                    reason=f"result_{frame.status.value}",
                    actor_agent_id=reporting_agent_id,
                    status_code=frame.status_code,
                    output=(frame.output.model_dump() if frame.output else None),
                    error=frame.error,
                )
            except IllegalTransition:
                # Already terminal — drop the duplicate Result.
                logger.info(
                    "duplicate_result_dropped",
                    extra={
                        "event": "duplicate_result_dropped",
                        "bp.task_id": frame.task_id,
                    },
                )
                return

    # Wake any in-process listener waiting for this task's terminal
    # state (e.g. `POST /v1/admin/tasks/test`). Outside the connection
    # so the listener observes a committed row when it queries.
    _notify_task_terminal(state, frame.task_id)

    # Fan out to the caller agent. Unlike the old parent-walk, this
    # works for root tasks too (the caller IS a real agent: the
    # channel agent or the synthetic admin_console).
    try:
        await deliver_frame(
            state,
            caller_agent_id,
            ResultFrame(
                # Preserve the producing agent's identity instead of
                # stamping "router". Channel agents (webapp, telegram)
                # otherwise had to guess which agent answered.
                agent_id=reporting_agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                task_id=frame.task_id,
                parent_task_id=parent_task_id,
                status=frame.status,
                status_code=frame.status_code,
                output=frame.output,
                error=frame.error,
            ),
            await_ack=False,
        )
    except (AgentNotConnected, asyncio.QueueFull):
        logger.info(
            "caller_offline_result_dropped",
            extra={
                "event": "caller_offline_result_dropped",
                "bp.task_id": frame.task_id,
            },
        )


def _state_from_status(status: TaskStatus) -> TaskState:
    return {
        TaskStatus.SUCCEEDED: TaskState.SUCCEEDED,
        TaskStatus.FAILED: TaskState.FAILED,
        TaskStatus.CANCELLED: TaskState.CANCELLED,
        TaskStatus.TIMED_OUT: TaskState.TIMED_OUT,
    }[status]


# ---------------------------------------------------------------------------
# Cancellation (recursive)
# ---------------------------------------------------------------------------


async def cancel_task(
    state: AppState,
    task_id: str,
    *,
    user_id: str,
    reason: str = "user_aborted",
    initiator: str = "user",
) -> int:
    """Cancel a task and all of its descendants.

    For each task in the cancelled set, this function:
      1. Atomically transitions to CANCELLED via `task_transition`.
         A row that's already terminal raises `IllegalTransition`,
         which we treat as "lost the race to a legitimate Result"
         and skip — the legitimate `complete_task` is fanning that
         Result out to the parent, so we mustn't double-emit.
      2. On a winning transition: emits a synthetic
         Result(CANCELLED) to the parent agent so the parent's
         `peers.spawn(...)` future resolves immediately. Without
         this, the parent's await would either hang until
         `correlation_timeout` (the agent's natural Result lands
         AFTER cancel and gets dropped by `complete_task` on
         `IllegalTransition`) or — if the agent never emits one —
         hang indefinitely.
      3. Sends a CancelFrame to the OWNING agent so it can stop
         doing the work.

    The "only emit Result on transition success" guarantee gives
    the protocol's "exactly one terminal frame per task" promise:
      - cancel won the race → cancel_task emits CANCELLED;
        agent's late Result is dropped by `complete_task`.
      - agent's Result won the race → `complete_task` emits
        SUCCEEDED/FAILED; cancel_task's transition raises
        IllegalTransition and skips the synthetic emit.
    """
    pool = state.db_pool  # type: ignore[attr-defined]
    cancelled = 0

    # Hold ONE connection across the entire DB sweep.
    # Previously this loop did `pool.acquire()` 2-3× per descendant
    # (transition + owner lookup + optional parent-owner lookup), which
    # under a deep cancel tree monopolised the default 10-conn pool
    # against legitimate user traffic. We collect everything needed for
    # frame delivery FIRST, then release the connection BEFORE
    # `deliver_frame` — those WS sends await Acks and must NOT hold a
    # DB connection across the wait.
    fanout_plan: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        scope = queries.Scope.user(conn, user_id)
        descendants = await scope.list_descendants(task_id)
        targets = [task_id, *(d.task_id for d in descendants)]

        for tid in targets:
            async with conn.transaction():
                try:
                    await task_transition(
                        conn,
                        tid,
                        TaskState.CANCELLED,
                        reason=reason,
                        actor_agent_id=initiator,
                    )
                    cancelled += 1
                except IllegalTransition:
                    continue
                except TaskNotFound:
                    continue

                # Read the fan-out targets INSIDE the transaction,
                # under the same row lock `task_transition` took
                # (its UPDATE holds the row to commit). Pre-R8 this
                # ran AFTER the transaction committed, in autocommit:
                # a concurrent `_admit_delegation` flip could change
                # `active_agent_id` between the CANCELLED commit and
                # this SELECT, so the CancelFrame was delivered to
                # the wrong agent (a just-arrived delegate, or a
                # stale executor) while the real executor kept
                # running uncancelled. Reading under the same lock
                # makes the fan-out plan a consistent snapshot of
                # the row we just cancelled.
                owner = await conn.fetchrow(
                    "SELECT active_agent_id, caller_agent_id, parent_task_id "
                    "FROM tasks WHERE task_id = $1",
                    tid,
                )

            # Wake any listener. Per-tid because a
            # cancel sweep can resolve N tasks at once.
            _notify_task_terminal(state, tid)
            if owner is None:
                continue

            fanout_plan.append({
                "tid": tid,
                # Where the CancelFrame goes — the executor doing the
                # work. Under delegation that's not the original
                # destination.
                "active_agent_id": owner["active_agent_id"],
                # Where the synthetic Result(CANCELLED) goes — the
                # caller that's awaiting a terminal frame.
                "caller_agent_id": owner["caller_agent_id"],
                "parent_task_id": owner["parent_task_id"],
            })

    # Connection released. Frame delivery below — these calls await
    # Acks and would block the pool if held.

    # R8 (HIGH): abort any router-side LLM Tasks tied to the
    # cancelled task_ids BEFORE sending the CancelFrames downstream.
    # Pre-R8, `cancel_task` only sent CancelFrames; the
    # `_run_llm_call` Tasks on the router side kept streaming
    # provider tokens until the natural end (or until the calling
    # agent disconnected). This walks every live socket and
    # cancels any task whose `_bp_task_id` matches a cancelled
    # task. Tracking via the stamp set in
    # `dispatch._handle_llm_request` (line 469) — see comment
    # there for the rationale.
    _abort_router_side_llm_tasks(state, {p["tid"] for p in fanout_plan})

    for plan in fanout_plan:
        tid = plan["tid"]
        # Fan a synthetic Result(CANCELLED) to the PARENT agent (if
        # any). Only reached when `task_transition` succeeded above
        # — `IllegalTransition` / `TaskNotFound` `continue`s before
        # appending to `fanout_plan`, so a legitimate `complete_task`
        # Result that won the race is never accompanied by our
        # synthetic CANCELLED.
        # Fan synthetic Result(CANCELLED) to the caller — works for
        # both root tasks and children (caller_agent_id is always a
        # real agent).
        try:
            await deliver_frame(
                state,
                plan["caller_agent_id"],
                ResultFrame(
                    agent_id="router",
                    trace_id="0" * 32,
                    span_id="0" * 16,
                    task_id=tid,
                    parent_task_id=plan["parent_task_id"],
                    status=TaskStatus.CANCELLED,
                    status_code=499,
                    error={
                        "code": "cancelled",
                        "message": reason,
                    },
                ),
                await_ack=False,
            )
        except (AgentNotConnected, asyncio.QueueFull):
            logger.info(
                "caller_offline_cancel_result_dropped",
                extra={
                    "event": "caller_offline_cancel_result_dropped",
                    "bp.task_id": tid,
                },
            )

        try:
            await deliver_frame(
                state,
                plan["active_agent_id"],
                CancelFrame(
                    agent_id="router",
                    trace_id="0" * 32,
                    span_id="0" * 16,
                    task_id=tid,
                    reason=reason,
                ),
                await_ack=False,
            )
        except (AgentNotConnected, asyncio.QueueFull):
            pass

    return cancelled


# ---------------------------------------------------------------------------
# Failure helper
# ---------------------------------------------------------------------------


async def fail_task(
    state: AppState,
    task_id: str,
    *,
    user_id: str | None = None,
    status_code: int,
    reason: str,
    error: dict[str, Any] | None = None,
    actor_agent_id: str | None = None,
    conn: Any | None = None,
    terminal_state: TaskState = TaskState.FAILED,
) -> None:
    """Transition a task to a non-terminal-failure state (`FAILED` by
    default, or `TIMED_OUT` for the deadline sweep) and propagate a
    matching Result to the parent.

    `terminal_state` MUST be `FAILED` or `TIMED_OUT` — the two
    router-initiated failure terminals. It drives BOTH the task's own
    state change and the `status` on the parent fan-out Result, so a
    deadline-expired task is reported `timed_out` (not `failed`) to its
    caller, in audit, and on idempotent replay. The descendant subtree
    is cascade-CANCELLED regardless (the children are abandoned either
    way).

    `conn` is an optional caller-held DB connection. When provided,
    `fail_task` reuses it for both the transition transaction AND
    the parent-owner lookup instead of acquiring two new
    connections from the pool. Used by `fail_inflight_for_agent`
    to fail an arbitrary number of an agent's tasks without
    monopolising the pool."""
    pool = state.db_pool  # type: ignore[attr-defined]
    # Audit/fan-out reason for the cascaded descendant cancellations: name the
    # parent's actual terminal cause (a deadline-expired parent timed out, it
    # didn't "fail"). Cosmetic — the descendant terminal STATE is CANCELLED
    # either way; this only makes audit/Result wording accurate.
    cascade_reason = (
        "parent_timed_out"
        if terminal_state == TaskState.TIMED_OUT
        else "parent_failed"
    )

    async def _do_db_work(c: Any) -> dict | None:
        nonlocal user_id
        if user_id is None:
            row = await c.fetchrow(
                "SELECT user_id FROM tasks WHERE task_id = $1",
                task_id,
            )
            if row is None:
                return None
            user_id = row["user_id"]

        async with c.transaction():
            # Read the parent task_id WITHIN the transaction. The
            # column is immutable, but reading on the same connection
            # outside the transaction would put a follow-up read in
            # autocommit mode — a future maintainer adding a write
            # there would silently bypass atomicity. Keeping it inside
            # the transaction makes the boundary explicit.
            parent_row = await c.fetchrow(
                "SELECT parent_task_id FROM tasks WHERE task_id = $1",
                task_id,
            )
            if parent_row is None:
                return None
            try:
                await task_transition(
                    c,
                    task_id,
                    terminal_state,
                    reason=reason,
                    actor_agent_id=actor_agent_id,
                    status_code=status_code,
                    error=error or {"code": reason},
                )
            except IllegalTransition:
                return None
            except TaskNotFound:
                return None
        return dict(parent_row)

    if conn is not None:
        parent_row_data = await _do_db_work(conn)
    else:
        async with pool.acquire() as c:
            parent_row_data = await _do_db_work(c)

    if parent_row_data is None:
        return

    # Wake any in-process listener. Outside the
    # connection so the listener observes a committed row.
    _notify_task_terminal(state, task_id)

    # Cascade-cancel the descendant subtree. A task that fails or
    # times out strands its children: nothing will ever consume
    # their Results, yet they keep running — burning compute and
    # provider tokens — until their OWN deadlines fire (and a child
    # with no deadline runs forever). `fail_task` previously only
    # propagated UP to the parent; the downward subtree was leaked.
    #
    # This MUST run before the `parent_task_id is None` early-return
    # below — a root task that times out has descendants too.
    #
    # Mirrors cancel_task's proven shape: a per-tid transaction (one
    # already-terminal child must not roll back the cancel of its
    # siblings), the fan-out plan captured under the row lock, frames
    # delivered AFTER the DB work with await_ack=False. Pool-
    # respecting: the deadline sweep and disconnect cleanup hold ONE
    # shared connection across a 100-row batch and pass it in via
    # `conn` — re-use it instead of acquiring per descendant.
    #
    # `user_id` is guaranteed non-None here: `_do_db_work` only
    # returns a non-None row after it has resolved/validated it.
    assert user_id is not None

    async def _cancel_subtree(c: Any) -> list[dict[str, Any]]:
        scope = queries.Scope.user(c, user_id)
        descendants = await scope.list_descendants(task_id)
        plan: list[dict[str, Any]] = []
        for d in descendants:
            # Per-descendant isolation, mirroring _sweep_once /
            # fail_inflight_for_agent. The expected `task_transition`
            # failures (IllegalTransition / TaskNotFound) are handled
            # inline, but an UNEXPECTED asyncpg error on ONE child
            # (serialization failure, deadlock, statement timeout, a
            # task_events constraint hiccup) must NOT propagate out of
            # _cancel_subtree and abandon every remaining descendant
            # of this parent — that re-creates the exact orphan-leak
            # this cascade exists to prevent, just triggered by a
            # poison row inside a 100-row sweep / disconnect batch.
            # asyncpg's transaction() CM rolls back on exception so
            # the shared `c` is clean for the next descendant; the
            # outer guard logs + continues.
            try:
                async with c.transaction():
                    try:
                        await task_transition(
                            c,
                            d.task_id,
                            TaskState.CANCELLED,
                            reason=cascade_reason,
                            actor_agent_id="router",
                        )
                    except IllegalTransition:
                        # Lost the race to a legitimate terminal
                        # frame for this child — its real Result is
                        # already fanning out; don't double-emit.
                        continue
                    except TaskNotFound:
                        continue
                    owner = await c.fetchrow(
                        "SELECT active_agent_id, caller_agent_id, parent_task_id "
                        "FROM tasks WHERE task_id = $1",
                        d.task_id,
                    )
                _notify_task_terminal(state, d.task_id)
                if owner is None:
                    continue
                plan.append({
                    "tid": d.task_id,
                    "active_agent_id": owner["active_agent_id"],
                    "caller_agent_id": owner["caller_agent_id"],
                    "parent_task_id": owner["parent_task_id"],
                })
            except Exception:  # noqa: BLE001
                logger.exception(
                    "fail_task_cascade_row_failed",
                    extra={
                        "event": "fail_task_cascade_row_failed",
                        "bp.task_id": d.task_id,
                        "bp.parent_task_id": task_id,
                    },
                )
                continue
        return plan

    if conn is not None:
        subtree_plan = await _cancel_subtree(conn)
    else:
        async with pool.acquire() as c:
            subtree_plan = await _cancel_subtree(c)

    # Stop any router-side LLM streams tied to the cancelled
    # descendants before the CancelFrames go out (same rationale as
    # cancel_task: a wedged provider stream keeps billing tokens).
    _abort_router_side_llm_tasks(
        state, {p["tid"] for p in subtree_plan}
    )

    for plan in subtree_plan:
        tid = plan["tid"]
        # Synthetic Result(CANCELLED) to the caller awaiting this
        # child so its `peers.spawn(...)` future resolves now instead
        # of hanging to correlation_timeout.
        try:
            await deliver_frame(
                state,
                plan["caller_agent_id"],
                ResultFrame(
                    agent_id="router",
                    trace_id="0" * 32,
                    span_id="0" * 16,
                    task_id=tid,
                    parent_task_id=plan["parent_task_id"],
                    status=TaskStatus.CANCELLED,
                    status_code=499,
                    error={
                        "code": "cancelled",
                        "message": cascade_reason,
                    },
                ),
                await_ack=False,
            )
        except (AgentNotConnected, asyncio.QueueFull):
            pass
        # CancelFrame to the executor doing the work so it stops.
        try:
            await deliver_frame(
                state,
                plan["active_agent_id"],
                CancelFrame(
                    agent_id="router",
                    trace_id="0" * 32,
                    span_id="0" * 16,
                    task_id=tid,
                    reason=cascade_reason,
                ),
                await_ack=False,
            )
        except (AgentNotConnected, asyncio.QueueFull):
            pass

    if parent_row_data["parent_task_id"] is None:
        return

    if conn is not None:
        parent_owner = await conn.fetchrow(
            "SELECT agent_id FROM tasks WHERE task_id = $1",
            parent_row_data["parent_task_id"],
        )
    else:
        async with pool.acquire() as c:
            parent_owner = await c.fetchrow(
                "SELECT agent_id FROM tasks WHERE task_id = $1",
                parent_row_data["parent_task_id"],
            )
    if parent_owner is None:
        return
    try:
        await deliver_frame(
            state,
            parent_owner["agent_id"],
            ResultFrame(
                agent_id="router",
                trace_id="0" * 32,
                span_id="0" * 16,
                task_id=task_id,
                parent_task_id=parent_row_data["parent_task_id"],
                status=_STATUS_FROM_STATE[terminal_state],
                status_code=status_code,
                error=error or {"code": reason},
            ),
            await_ack=False,
        )
    except (AgentNotConnected, asyncio.QueueFull):
        pass


# ---------------------------------------------------------------------------
# Disconnect cleanup
# ---------------------------------------------------------------------------


async def fail_inflight_for_agent(
    state: AppState, agent_id: str, *, reason: str = "agent_disconnected"
) -> int:
    """Fail every non-terminal task currently assigned to `agent_id`.

    Called from ws_hub._on_disconnect when the resume window does not
    apply.

    Holds ONE connection across the entire fail-loop.
    Each `fail_task` call would otherwise acquire 2 fresh connections;
    for an agent with N in-flight tasks, that's 2N + 1 acquires —
    enough to monopolise the default 10-conn pool when an agent with
    a long task list disconnects.
    """
    pool = state.db_pool  # type: ignore[attr-defined]
    failed = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT task_id FROM tasks
            WHERE agent_id = $1
              AND state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
            """,
            agent_id,
        )
        for r in rows:
            # Isolate a poison row. `fail_task` handles
            # IllegalTransition / TaskNotFound internally, but an
            # UNEXPECTED error (serialization failure, deadlock, a
            # constraint violation on the task_event insert) would
            # otherwise propagate out of this loop and abandon every
            # REMAINING in-flight task for the disconnected agent —
            # they'd hang until the deadline sweep. asyncpg's
            # `transaction()` CM rolls back on exception, so the
            # shared `conn` is clean for the next iteration; we just
            # need to not let one bad row kill the batch.
            try:
                await fail_task(
                    state,
                    r["task_id"],
                    status_code=503,
                    reason=reason,
                    error={"code": reason, "agent_id": agent_id},
                    actor_agent_id="router",
                    conn=conn,
                )
                failed += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "fail_inflight_row_failed",
                    extra={
                        "event": "fail_inflight_row_failed",
                        "bp.task_id": r["task_id"],
                        "bp.agent_id": agent_id,
                    },
                )
                continue
    return failed


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


def _sample_db_pool_metrics(pool: Any) -> None:
    """Sample asyncpg pool occupancy onto `db_pool_connections`.

    Best-effort: a pool that doesn't expose the introspection API
    (test fakes / a future pool impl) is silently skipped — a
    metric must never break deadline enforcement. `in_use` is
    derived (`size - idle`) because asyncpg has no direct
    checked-out count; `size` is conns the pool has actually
    opened (≤ max), so `in_use` is exact.
    """
    try:
        size = pool.get_size()
        idle = pool.get_idle_size()
        mx = pool.get_max_size()
    except Exception:  # noqa: BLE001
        return
    metrics.db_pool_connections.labels(state="in_use").set(size - idle)
    metrics.db_pool_connections.labels(state="idle").set(idle)
    metrics.db_pool_connections.labels(state="max").set(mx)


async def timeout_sweep_loop(
    state: AppState, *, interval_s: float = 5.0
) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            # Sample BEFORE the sweep acquires its own connection so
            # the gauge reflects ambient pool pressure, not the
            # sweep's transient +1.
            _sample_db_pool_metrics(state.db_pool)  # type: ignore[attr-defined]
            await _sweep_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "timeout_sweep_failed", extra={"event": "timeout_sweep_failed"}
            )


async def _sweep_once(state: AppState) -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    timed_out = 0
    # Hold ONE connection across the sweep. Each
    # `fail_task` call would otherwise acquire 2 fresh connections;
    # for a 100-row sweep batch that's 201 acquires per 5-s tick —
    # enough to monopolise the default 10-conn pool against user
    # traffic. Pass the held conn through.
    async with pool.acquire() as conn:
        rows = await queries.find_expired_tasks(conn, now=_now(), limit=100)
        for row in rows:
            # Per-row isolation. The outer `timeout_sweep_loop`
            # except only stops the WHOLE sweep dying; it does NOT
            # protect the remaining rows in THIS batch, and an
            # asyncpg error mid-`fail_task` on the shared `conn`
            # would otherwise poison every subsequent iteration
            # ("current transaction is aborted"). asyncpg's
            # transaction CM rolls back on exception so the conn is
            # clean for the next row — wrap + continue so one bad
            # row doesn't stall deadline enforcement for the other
            # ~99 expired tasks until the next 5-s tick.
            try:
                await fail_task(
                    state,
                    row.task_id,
                    user_id=row.user_id,
                    status_code=504,
                    reason="deadline_exceeded",
                    error={"code": "deadline_exceeded"},
                    conn=conn,
                    terminal_state=TaskState.TIMED_OUT,
                )
                timed_out += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "timeout_sweep_row_failed",
                    extra={
                        "event": "timeout_sweep_row_failed",
                        "bp.task_id": row.task_id,
                    },
                )
                continue
    return timed_out


async def file_gc_loop(state: AppState, *, interval_s: float = 300.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _gc_files_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "file_gc_failed", extra={"event": "file_gc_failed"}
            )


async def _gc_files_once(state: AppState) -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    deleted = 0
    storage_to_delete: list[tuple[str, str]] = []  # (file_id, sha256)

    # Hold ONE connection across the entire DB sweep.
    # The original code did `pool.acquire()` per file in a batch of up
    # to 1000 files — a 300-s sweep cycle could acquire a thousand
    # connections, contending with user traffic for the default
    # 10-conn pool. We collect storage-delete intents first and run
    # them AFTER the DB conn is released, since the storage backend
    # call (S3 PutObject for example) is slow network I/O that must
    # NOT hold a DB connection.
    async with pool.acquire() as conn:
        rows = await queries.find_expired_files(conn, now=_now(), limit=1000)
        for row in rows:
            # Cross-user dedup means user A and user B can each hold a
            # `files` row pointing at the same content-addressed
            # storage object. Delete the expired ROW first; only
            # delete the underlying STORAGE bytes when no other row
            # still references this `sha256`. Without this check, A's
            # expiry would silently 404 B's next download (review
            # item M6).
            async with conn.transaction():
                await queries.delete_file_row(conn, row.file_id)
                other_refs = await queries.count_other_file_refs(
                    conn, sha256=row.sha256, exclude_file_id=row.file_id
                )
            deleted += 1
            if other_refs > 0:
                # Storage bytes are still in use by another user —
                # leave them in place. The DB row is gone so this
                # entry won't be reconsidered on the next pass.
                continue
            storage_to_delete.append((row.file_id, row.sha256))

    # Connection released. Storage deletes happen here — these are
    # network I/O (S3, etc.) and would block the DB pool if held.
    for file_id, sha256 in storage_to_delete:
        # Re-check references in a SHORT fresh transaction
        # immediately before the storage delete. The in-loop
        # `count_other_file_refs` above only closes the
        # intra-snapshot race; it does NOT cover the window between
        # that transaction committing and this storage delete. In
        # that window a concurrent `insert_file` for the SAME
        # content-addressed `sha256` (cross-user dedup makes this
        # common — two users uploading the same bytes) creates a
        # brand-new `files` row pointing at these storage bytes.
        # Deleting them now 404s the new owner's next download.
        # `exclude_file_id` is the already-deleted row, so it
        # matches nothing — this counts ANY live reference. >0 means
        # a new owner appeared; skip the delete and let the next
        # sweep re-evaluate (the bytes stay until truly unreferenced).
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    live_refs = await queries.count_other_file_refs(
                        conn, sha256=sha256, exclude_file_id=file_id
                    )
        except Exception:  # noqa: BLE001
            # Re-check itself failed (pool/DB blip). Conservative:
            # SKIP the storage delete — leaking bytes is recoverable
            # on a later sweep; deleting bytes a new owner now
            # references is not.
            logger.warning(
                "file_gc_recheck_failed_skipping_delete",
                extra={
                    "event": "file_gc_recheck_failed_skipping_delete",
                    "bp.file_id": file_id,
                },
            )
            continue
        if live_refs > 0:
            logger.info(
                "file_gc_storage_delete_skipped_reref",
                extra={
                    "event": "file_gc_storage_delete_skipped_reref",
                    "bp.file_id": file_id,
                    "live_refs": live_refs,
                },
            )
            continue
        try:
            await state.file_store.delete(sha256)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            # Row is already deleted; storage bytes leak but don't
            # corrupt anything. Operator-actionable via the log line.
            logger.exception(
                "file_delete_failed",
                extra={"event": "file_delete_failed", "file_id": file_id},
            )
    return deleted


# ---------------------------------------------------------------------------
# Lifespan helper
# ---------------------------------------------------------------------------


async def session_gc_loop(
    state: AppState,
    *,
    interval_s: float = 3_600.0,
    retention_days: int = 30,
) -> None:
    """Drop ephemeral admin-test sessions older than `retention_days`
    that have no remaining tasks. Conservative — never touches user
    sessions or sessions whose tasks are still around."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _gc_admin_test_sessions(state, retention_days=retention_days)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "session_gc_failed", extra={"event": "session_gc_failed"}
            )


async def _gc_admin_test_sessions(
    state: AppState, *, retention_days: int
) -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    cutoff = _now() - timedelta(days=retention_days)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM sessions s
            WHERE s.metadata ->> 'kind' = 'admin_test'
              AND s.opened_at < $1
              AND NOT EXISTS (
                  SELECT 1 FROM tasks t WHERE t.session_id = s.session_id
              )
            """,
            cutoff,
        )
    parts = result.split()
    deleted = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    if deleted:
        logger.info(
            "admin_test_sessions_gc",
            extra={"event": "admin_test_sessions_gc", "deleted": deleted},
        )
    return deleted


async def registration_attempts_gc_loop(
    state: AppState,
    *,
    interval_s: float = 3_600.0,
    retention_days: int = 30,
) -> None:
    """Sweep `registration_attempts` rows older than `retention_days`.

    F7 audit table. Grows unbounded — one row per submit
    regardless of rate-limit outcome — but the rate-limit logic
    only needs the most-recent minute or two, so a generous
    retention window plus a daily-ish sweep keeps the table size
    bounded without losing useful audit."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _gc_registration_attempts(state, retention_days=retention_days)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "registration_attempts_gc_failed",
                extra={"event": "registration_attempts_gc_failed"},
            )


async def _gc_registration_attempts(
    state: AppState, *, retention_days: int
) -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    cutoff = _now() - timedelta(days=retention_days)
    async with pool.acquire() as conn:
        deleted = await queries.gc_registration_attempts(conn, cutoff=cutoff)
    if deleted:
        logger.info(
            "registration_attempts_gc",
            extra={
                "event": "registration_attempts_gc",
                "deleted": deleted,
                "retention_days": retention_days,
            },
        )
    return deleted


async def invitation_gc_loop(
    state: AppState,
    *,
    interval_s: float = 3_600.0,
    retention_days: int = 7,
) -> None:
    """Sweep terminal (used or expired) invitations older than
    `retention_days`.

    The suite mints a fresh single-use invitation per agent on EVERY launch
    (`scripts/prod.sh` `refresh_invitations`, 10-min TTL), so dead rows pile up
    ~one per agent per relaunch. A shorter retention than the other GC loops
    (7d vs 30d) — these are high-churn and the auth audit log already records
    onboard outcomes — but still generous enough to keep recent invitation
    history for debugging. LIVE (unused, unexpired) invitations are never
    touched (see `queries.gc_expired_invitations`)."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _gc_expired_invitations(state, retention_days=retention_days)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "invitation_gc_failed",
                extra={"event": "invitation_gc_failed"},
            )


async def _gc_expired_invitations(
    state: AppState, *, retention_days: int
) -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    cutoff = _now() - timedelta(days=retention_days)
    async with pool.acquire() as conn:
        deleted = await queries.gc_expired_invitations(conn, cutoff=cutoff)
    if deleted:
        logger.info(
            "invitation_gc",
            extra={
                "event": "invitation_gc",
                "deleted": deleted,
                "retention_days": retention_days,
            },
        )
    return deleted


async def start_background_loops(state: AppState) -> list[asyncio.Task]:
    return [
        asyncio.create_task(timeout_sweep_loop(state)),
        asyncio.create_task(file_gc_loop(state)),
        asyncio.create_task(session_gc_loop(state)),
        asyncio.create_task(registration_attempts_gc_loop(state)),
        asyncio.create_task(invitation_gc_loop(state)),
    ]


# ---------------------------------------------------------------------------
# Session / role lookup helper
# ---------------------------------------------------------------------------


async def _session_level(state: AppState, user_id: str) -> str | None:
    """Return the principal level (admin | service | tierN) for the user,
    or None if the user is missing, suspended, or soft-deleted.

    R8 fix (HIGH): the prior SELECT only filtered `suspended_at IS NULL`,
    NOT `deleted_at IS NULL` — soft-deleted users could still have
    tasks admitted on their behalf. R1's `queries.user_is_active`
    helper folds both checks; route through `resolve_user_level`
    (which already does both via `user_is_active`) for consistency.

    R8 perf: route through `LlmService._user_level_cache` (60s TTL,
    5000-entry LRU). Pre-R8 every admit did a fresh `pool.acquire()`
    + SELECT on the critical path, even though the same cache that
    `_principal_from_request` consults for auth was right there.
    """
    if not user_id:
        return None
    llm_service = getattr(state, "llm_service", None)
    # Fast path: in-memory cache lookup (R5 added the metric so we
    # can observe hit rate).
    if llm_service is not None:
        cached = llm_service.peek_user_level_cached(user_id)
        if cached is not None:
            return cached
    # Cache miss → DB round trip via `resolve_user_level`, which
    # itself checks both `suspended_at` and `deleted_at` via
    # `user_is_active` (queries.py).
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        if llm_service is not None:
            return await llm_service.resolve_user_level(conn, user_id)
        # Defensive: no llm_service on `state` (test fixtures may
        # skip it). Fall through to the original SELECT shape but
        # WITH the deleted_at guard added.
        row = await conn.fetchrow(
            "SELECT level FROM users "
            "WHERE user_id = $1 "
            "  AND suspended_at IS NULL "
            "  AND deleted_at IS NULL",
            user_id,
        )
        if row is None:
            return None
        return row["level"]
