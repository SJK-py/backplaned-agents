"""bp_router.api.sessions — Open / list / close user sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bp_router.db import queries
from bp_router.security.jwt import SessionPrincipal, require_authenticated
from bp_router.tasks import cancel_task

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
