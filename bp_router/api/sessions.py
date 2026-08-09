"""bp_router.api.sessions — Open / list / close user sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from bp_protocol.frames import (
    AcquireLeaseOp,
    GetStateOp,
    HandOverOp,
    ListThreadsOp,
    ReadOp,
    ReleaseLeaseOp,
    SessionMessage,
    SessionOp,
    SessionOpResult,
    SetStateOp,
    StateValue,
    ThreadStat,
)
from bp_router.db import queries
from bp_router.delivery import notify_lease_promotions
from bp_router.security.jwt import SessionPrincipal, require_authenticated
from bp_router.session_store import (
    READ_BUDGET_FRACTION,
    SessionStoreError,
    StoreScope,
    execute_batch,
)
from bp_router.tasks import cancel_task


def _utcnow() -> datetime:
    return datetime.now(UTC)

router = APIRouter()


class OpenSessionRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionView(BaseModel):
    session_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskSummaryView(BaseModel):
    task_id: str
    parent_task_id: str | None
    state: str
    status_code: int | None = None
    agent_id: str
    created_at: datetime
    updated_at: datetime


def _session_to_view(row) -> SessionView:  # type: ignore[no-untyped-def]
    return SessionView(
        session_id=row.session_id,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        metadata=row.metadata,
    )


@router.post("", response_model=SessionView, status_code=201)
async def open_session(
    req: OpenSessionRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> SessionView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        # Atomic session insert + audit append.
        # Without the transaction the audit append could fail
        # independently and leave a session opened with no audit row.
        async with conn.transaction():
            row = await queries.Scope.user(conn, principal.user_id).open_session(
                metadata=req.metadata
            )
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="session.opened",
                target_kind="session",
                target_id=row.session_id,
            )
    return _session_to_view(row)


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    request: Request,
    purge: bool = False,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> None:
    """Close (archive) the session. With `?purge=true`, also **hard-delete**
    it and its router-side data — tasks, task events, the file-name directory;
    `files` rows are detached for the reclaim sweep — after closing. That's
    the webapp's "remove session". Idempotent; 404 if not the caller's."""
    state = request.app.state.bp
    existed = await _close_session(state, session_id, principal.user_id)
    if not existed:
        raise HTTPException(status_code=404, detail="session not found")
    if not purge:
        return None
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            scope = queries.Scope.user(conn, principal.user_id)
            await scope.purge_session(session_id)
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="session.purged",
                target_kind="session",
                target_id=session_id,
            )
    return None


async def _close_session(state: Any, session_id: str, user_id: str) -> bool:
    """Cancel in-flight tasks + archive the session (+ GC its file-name
    directory). Returns False if the session isn't the user's, True otherwise
    (idempotent when already closed). Shared by the close + purge paths."""
    async with state.db_pool.acquire() as conn:
        scope = queries.Scope.user(conn, user_id)
        existing = await scope.get_session(session_id)
        if existing is None:
            return False
        already_closed = existing.closed_at is not None
        rows = (
            []
            if already_closed
            else await conn.fetch(
                """
                SELECT task_id FROM tasks
                WHERE user_id = $1 AND session_id = $2
                  AND state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
                """,
                user_id,
                session_id,
            )
        )

    for r in rows:
        await cancel_task(
            state,
            r["task_id"],
            user_id=user_id,
            reason="session_closed",
            initiator=user_id,
        )

    if not already_closed:
        async with state.db_pool.acquire() as conn:
            # Atomic session-close + file-store GC + audit append.
            async with conn.transaction():
                scope = queries.Scope.user(conn, user_id)
                await scope.close_session(session_id)
                # Reclaim the session's ephemeral file stash: delete every
                # `file_names` directory row under this session's scope.
                # The now-unreferenced blobs are reclaimed by the refcount
                # sweep — NOT inline, to keep an S3 delete storm off the
                # close request path. `persist/` rows are user-wide and
                # untouched.
                gc_count = await scope.delete_file_names_for_scope(
                    f"session:{session_id}"
                )
                await queries.append_audit_event(
                    conn,
                    actor_kind="user",
                    actor_id=user_id,
                    event="session.closed",
                    target_kind="session",
                    target_id=session_id,
                    payload={"file_names_gc": gc_count} if gc_count else None,
                )
    return True


@router.post("/{session_id}/reopen", response_model=SessionView)
async def reopen_session(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> SessionView:
    """Re-open a previously closed session: clears `closed_at` so task
    injection is accepted again. History/metadata are retained; cancelled
    tasks and the GC'd file stash are NOT restored. Idempotent if already
    open (no audit emitted); 404 if not the caller's."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            scope = queries.Scope.user(conn, principal.user_id)
            existing = await scope.get_session(session_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="session not found")
            if existing.closed_at is not None:
                await scope.reopen_session(session_id)
                await queries.append_audit_event(
                    conn,
                    actor_kind="user",
                    actor_id=principal.user_id,
                    event="session.reopened",
                    target_kind="session",
                    target_id=session_id,
                )
                existing = await scope.get_session(session_id)
    return _session_to_view(existing)


@router.get("", response_model=list[SessionView])
async def list_sessions(
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[SessionView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.Scope.user(conn, principal.user_id).list_sessions()
    return [_session_to_view(r) for r in rows]


@router.get("/{session_id}/tasks", response_model=list[TaskSummaryView])
async def list_session_tasks(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[TaskSummaryView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope = queries.Scope.user(conn, principal.user_id)
        if await scope.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        rows = await scope.list_session_tasks(session_id)
    return [
        TaskSummaryView(
            task_id=r.task_id,
            parent_task_id=r.parent_task_id,
            state=r.state.value,
            status_code=r.status_code,
            agent_id=r.agent_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Session store — the steward (gateway) surface
#
# A channel or webapp SPAWNS tasks; it is never a task's active executor, so
# it has no task from which the store can derive scope. These endpoints are
# its way in, authorised by the caller's session JWT and ownership-checked
# against `sessions.user_id` — the same shape `/v1/files/names` uses for the
# same reason.
#
# There is deliberately NO message-POST endpoint. A steward cannot append to
# any thread, at any time: `StoreScope(agent_id=None)` has no owner to stamp,
# so `Append` refuses. A steward that wants something in a thread enqueues a
# hand-over and the owning agent materialises it under its own authorship
# (`docs/design/router-managed-session-store.md` §7, §8).
# ---------------------------------------------------------------------------


_STEWARD_OPS = TypeAdapter(list[SessionOp])


class HandoverRequest(BaseModel):
    target_agent_id: str
    item_kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    thread_key: str = ""


class StatePatchRequest(BaseModel):
    """Session-scoped state only — a steward has no thread state of its own.
    `value=None` deletes; `expected_version` is the CAS token (§3.6)."""

    key: str
    value: str | None = None
    expected_version: int | None = None


class LeaseRequest(BaseModel):
    holder_id: str
    ttl_ms: int = Field(default=300_000, ge=1_000, le=3_600_000)


class SessionMetadataPatch(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)


async def _owned_session(state: Any, session_id: str, user_id: str) -> None:
    """404 unless the session exists and belongs to the caller. Same
    non-enumerable posture as the file endpoints: a foreign session is
    indistinguishable from a missing one."""
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, user_id).get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")


async def _run_steward_batch(
    state: Any,
    session_id: str,
    user_id: str,
    ops: list[Any],
    *,
    scope: str = "session",
) -> list[SessionOpResult]:
    """Execute a batch with NO owning agent — which is exactly what makes
    the steward surface safe: every thread-writing op refuses `denied`
    inside the store, with no endpoint-level allowlist to keep in sync."""
    store_scope = StoreScope(
        user_id=user_id,
        session_id=None if scope == "user" else session_id,
        agent_id=None,
    )
    budget = int(state.settings.max_payload_bytes * READ_BUDGET_FRACTION)
    async with state.db_pool.acquire() as conn:
        try:
            async with conn.transaction():
                outcome = await execute_batch(
                    conn, store_scope, ops, budget_bytes=budget
                )
        except SessionStoreError as exc:
            status = 409 if exc.code in _CONFLICT_CODES else 403
            raise HTTPException(
                status_code=status,
                detail={"error": exc.code, "op_index": exc.index},
            ) from exc
    notify_lease_promotions(state, outcome.promotions)
    return outcome.results


# Refusals a caller can resolve by retrying with fresher state, versus ones
# that mean "you may not do this at all".
_CONFLICT_CODES = {
    "thread_conflict",
    "version_conflict",
    "floor_regression",
    "session_busy",
    "lease_lost",
    "quota_exceeded",
    "content_too_large",
}


@router.get("/{session_id}/messages", response_model=list[SessionMessage])
async def list_session_messages(
    session_id: str,
    request: Request,
    owner_agent_id: str,
    thread_key: str = "",
    roles: str | None = None,
    include_retired: bool = False,
    include_redacted: bool = False,
    include_hidden: bool = True,
    since_id: int | None = None,
    before_id: int | None = None,
    limit: int = 200,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[SessionMessage]:
    """Transcript rendering. Cursor-paginated by `id`; this is the
    unbounded path (the WS read is budget-capped by the frame ceiling —
    §9.2). `roles` is a comma-separated list; omitting it returns every
    role, because the router does not know which ones a caller considers
    conversational."""
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [
            ReadOp(
                owner_agent_id=owner_agent_id,
                thread_key=thread_key,
                roles=roles.split(",") if roles else None,
                include_retired=include_retired,
                include_redacted=include_redacted,
                include_hidden=include_hidden,
                since_id=since_id,
                before_id=before_id,
                limit=max(1, min(limit, 5000)),
            )
        ],
    )
    return results[0].messages or []


@router.get("/{session_id}/threads", response_model=list[ThreadStat])
async def list_session_threads(
    session_id: str,
    request: Request,
    owner_agent_id: str | None = None,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[ThreadStat]:
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [ListThreadsOp(owner_agent_id=owner_agent_id)],
    )
    return results[0].threads or []


@router.post("/{session_id}/handovers", status_code=201)
async def enqueue_handover(
    session_id: str,
    req: HandoverRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, Any]:
    """Enqueue an item for an agent — the steward's ONLY route into another
    agent's context, and not history until that agent materialises it."""
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [
            HandOverOp(
                target_agent_id=req.target_agent_id,
                thread_key=req.thread_key,
                item_kind=req.item_kind,
                payload=req.payload,
            )
        ],
    )
    return {"id": results[0].message_id}


@router.get("/{session_id}/state", response_model=list[StateValue])
async def get_session_state(
    session_id: str,
    request: Request,
    owner_agent_id: str | None = None,
    thread_key: str = "",
    keys: str | None = None,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[StateValue]:
    """Read session-scoped state (default), or a thread's state when
    `owner_agent_id` names one — reads are session-scoped by design."""
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [
            GetStateOp(
                session_scoped=owner_agent_id is None,
                owner_agent_id=owner_agent_id,
                thread_key=thread_key,
                keys=keys.split(",") if keys else None,
            )
        ],
    )
    return results[0].state or []


@router.patch("/{session_id}/state", response_model=list[StateValue])
async def patch_session_state(
    session_id: str,
    req: StatePatchRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[StateValue]:
    """Write a SESSION-scoped key. Thread state is unreachable from here —
    it belongs to its owning agent."""
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [
            SetStateOp(
                session_scoped=True,
                key=req.key,
                value=req.value,
                expected_version=req.expected_version,
            )
        ],
    )
    return results[0].state or []


@router.post("/{session_id}/ops")
async def session_ops(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, Any]:
    """An arbitrary batch under steward authority: reads, session state,
    hand-overs, lease ops. Thread writes refuse `denied` in the store, so
    this endpoint needs no allowlist of its own."""
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    body = await request.json()
    try:
        ops = _STEWARD_OPS.validate_python(body.get("ops", []))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid ops") from exc
    if len(ops) > 32:
        raise HTTPException(status_code=413, detail="batch_too_large")
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        ops,
        scope=body.get("scope", "session"),
    )
    return {"results": [r.model_dump(mode="json") for r in results]}


@router.post("/{session_id}/lease")
async def acquire_session_lease(
    session_id: str,
    req: LeaseRequest,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, Any]:
    """Take the session's turn lease, or get a ticket.

    A steward has no socket the router can push a promotion to, so it polls:
    `409` carries `Retry-After` derived from the current holder's TTL, which
    is the same deadline a WS waiter arms its fallback timer on (§6.4)."""
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    results = await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [AcquireLeaseOp(holder_id=req.holder_id, ttl_ms=req.ttl_ms)],
    )
    lease = results[0].lease
    if lease is None or not lease.granted:
        retry_after = 1
        if lease is not None and lease.holder_expires_at is not None:
            delta = (lease.holder_expires_at - _utcnow()).total_seconds()
            retry_after = max(1, min(int(delta) + 1, 3600))
        response.headers["Retry-After"] = str(retry_after)
        response.status_code = 409
        return {
            "granted": False,
            "ticket": lease.ticket if lease else None,
            "holder_expires_at": (
                lease.holder_expires_at.isoformat()
                if lease and lease.holder_expires_at
                else None
            ),
        }
    return {
        "granted": True,
        "ticket": lease.ticket,
        "holder_expires_at": lease.holder_expires_at.isoformat()
        if lease.holder_expires_at
        else None,
    }


@router.delete("/{session_id}/lease", status_code=204)
async def release_session_lease(
    session_id: str,
    request: Request,
    holder_id: str,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> None:
    state = request.app.state.bp
    await _owned_session(state, session_id, principal.user_id)
    await _run_steward_batch(
        state,
        session_id,
        principal.user_id,
        [ReleaseLeaseOp(holder_id=holder_id)],
    )
    return None


@router.patch("/{session_id}", response_model=SessionView)
async def patch_session(
    session_id: str,
    req: SessionMetadataPatch,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> SessionView:
    """Shallow-merge into the session's `metadata` (null values delete a
    key). This is where conversation descriptors live — title, channel,
    chat id — instead of in a table shadowing the session row."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, principal.user_id).patch_session_metadata(
            session_id, req.patch
        )
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _session_to_view(row)
