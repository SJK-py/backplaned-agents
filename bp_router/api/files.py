"""bp_router.api.files — Upload and download file-store blobs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from bp_router.db import queries
from bp_router.file_store import (
    _allocate_name,
    _display_name,
    _quota_ok,
    _scope_for,
    _split_stash_name,
)
from bp_router.filename_utils import (
    _FILENAME_REJECT,
    safe_filename_for_header,
)
from bp_router.security.jwt import (
    FileReadAccess,
    SessionPrincipal,
    TokenError,
    extract_bearer,
    file_read_access,
    issue_file_fetch_token,
    require_authenticated,
    verify_file_upload_token,
)
from bp_router.storage.base import FileMeta
from bp_router.upload_utils import UploadTooLarge, hash_with_size_cap

logger = logging.getLogger(__name__)

router = APIRouter()


def _now() -> datetime:
    return datetime.now(UTC)


def _mint_fetch_key(settings, file_id: str, user_id: str) -> str:  # type: ignore[no-untyped-def]
    """A `file-fetch` capability key bound to (file_id, user) so the
    keyed download (`GET /v1/files/{file_id}`) is self-authorising —
    `file_read_access` verifies exactly this token."""
    token, _exp, _jti = issue_file_fetch_token(
        file_id=file_id,
        user_id=user_id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.file_fetch_token_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )
    return token


@router.post("", status_code=201)
async def upload(
    file: UploadFile,
    request: Request,
    session_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, str]:
    """Stream-upload a file. Content-addressed by sha256.

    Pass 1 hashes + size-checks while reading. Pass 2 rewinds and
    streams to the backend without accumulating in memory. The size
    cap is enforced mid-stream so a malicious / runaway client can't
    spool gigabytes into RAM before being rejected.
    """
    state = request.app.state.bp
    settings = state.settings
    file_store = state.file_store

    # Reject malformed filenames before reading any bytes — saves the
    # uploader's bandwidth on a request that would 400 anyway.
    if file.filename and _FILENAME_REJECT.search(file.filename):
        raise HTTPException(
            status_code=400,
            detail="filename contains control characters or quotes",
        )

    # Pass 1: hash + size with mid-stream cap.
    try:
        sha256, size = await hash_with_size_cap(
            file, max_bytes=settings.max_upload_bytes
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    if size == 0:
        raise HTTPException(status_code=400, detail="empty upload")

    mime_type = file.content_type or None

    # Pass 2: rewind and stream to the backend without buffering. The
    # underlying `SpooledTemporaryFile` (FastAPI's UploadFile backing)
    # handles the seek + reread cleanly.
    await file.seek(0)

    async def _src() -> AsyncIterator[bytes]:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    storage_url = await file_store.put(
        sha256,
        _src(),
        FileMeta(
            sha256=sha256,
            byte_size=size,
            mime_type=mime_type,
            original_filename=file.filename,
        ),
    )

    expires_at = _now() + timedelta(seconds=settings.file_default_ttl_s)
    async with state.db_pool.acquire() as conn:
        scope = queries.Scope.user(conn, principal.user_id)
        # Validate session_id / task_id ownership BEFORE insert
        # The `files` table FKs check
        # row existence but NOT same-user_id, so a user with a
        # valid session could otherwise upload a file pinned to
        # ANOTHER user's session_id / task_id. The file's own
        # `user_id` stays the uploader (download remains user-
        # scoped), but admin queries that JOIN files.session_id
        # against sessions.user_id would see inconsistent rows,
        # and the `_gc_admin_test_sessions` `NOT EXISTS` filter
        # could be defeated by a foreign-user file pinned to an
        # admin-test session.
        if session_id is not None:
            if await scope.get_session(session_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail="session_id not found in caller's tree",
                )
        if task_id is not None:
            if await scope.get_task(task_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail="task_id not found in caller's tree",
                )
        row = await scope.insert_file(
            sha256=sha256,
            session_id=session_id,
            task_id=task_id,
            byte_size=size,
            mime_type=mime_type,
            storage_url=storage_url,
            original_filename=file.filename,
            expires_at=expires_at,
        )

    return {
        "file_id": row.file_id,
        "sha256": row.sha256,
        "byte_size": str(row.byte_size),
        "path": f"/v1/files/{row.file_id}",
        "protocol": "router-proxy",
        "key": _mint_fetch_key(settings, row.file_id, principal.user_id),
    }


@router.post("/upload", status_code=201)
async def upload_with_grant(
    file: UploadFile,
    request: Request,
) -> dict[str, str]:
    """Token-authed upload for agents.

    Agents only carry an `agent`-kind JWT, which the session-scoped
    `POST /v1/files` rejects (`wrong_kind`). Instead the agent
    negotiates a one-shot `file-upload` token over its already-
    authenticated ws (Phase 2) and streams here. The grant is
    content-bound: we re-hash the body and refuse it unless it
    matches the granted `sha256` (and is no larger than the
    granted `byte_size`), so a leaked grant can't be repurposed to
    upload arbitrary bytes. The stored row is scoped to the grant's
    `user_id` (authoritative — no session needed).
    """
    state = request.app.state.bp
    settings = state.settings
    file_store = state.file_store

    token = extract_bearer(request.headers.get("authorization", ""))
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        grant = verify_file_upload_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

    if file.filename and _FILENAME_REJECT.search(file.filename):
        raise HTTPException(
            status_code=400,
            detail="filename contains control characters or quotes",
        )

    # Cap at the smaller of the global limit and the grant's
    # declared size — a grant can't be used to spool more than it
    # promised, and the global cap still bounds a malformed grant.
    cap = min(settings.max_upload_bytes, grant.byte_size)
    try:
        sha256, size = await hash_with_size_cap(file, max_bytes=cap)
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    if size == 0:
        raise HTTPException(status_code=400, detail="empty upload")
    # Content binding: the grant authorised exactly this sha/size.
    # A mismatch means the grant is being reused for other bytes —
    # refuse (do NOT persist).
    if sha256 != grant.sha256 or size != grant.byte_size:
        raise HTTPException(
            status_code=400, detail="upload does not match grant"
        )

    await file.seek(0)

    async def _src() -> AsyncIterator[bytes]:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    storage_url = await file_store.put(
        sha256,
        _src(),
        FileMeta(
            sha256=sha256,
            byte_size=size,
            mime_type=grant.mime_type,
            original_filename=file.filename,
        ),
    )

    expires_at = _now() + timedelta(seconds=settings.file_default_ttl_s)
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, grant.user_id).insert_file(
            sha256=sha256,
            session_id=None,
            task_id=None,
            byte_size=size,
            mime_type=grant.mime_type,
            storage_url=storage_url,
            original_filename=file.filename,
            expires_at=expires_at,
        )

    return {
        "file_id": row.file_id,
        "sha256": row.sha256,
        "byte_size": str(row.byte_size),
        "path": f"/v1/files/{row.file_id}",
        "protocol": "router-proxy",
        "key": _mint_fetch_key(settings, row.file_id, grant.user_id),
    }


# ---------------------------------------------------------------------------
# Session-authed named store
#
# The WS file frames (`FileStore`/`FileFetch`/`FileManage`) are scoped to a
# task's ACTIVE EXECUTOR, which a gateway agent (channel / webapp) never is
# — it spawns tasks, it doesn't run them. These endpoints give such a
# gateway the same named-store operations under its own per-user session
# JWT: `user_id` is the authenticated principal and `session_id` is
# ownership-checked (the same guard as `upload`). They reuse the exact
# dedup / scope / quota helpers from `bp_router.file_store`, so the HTTP
# and WS stores behave identically.
# ---------------------------------------------------------------------------


class BindNameRequest(BaseModel):
    name: str
    """Caller-facing stash name: `{filename}` (session) or
    `persist/{filename}` (user-wide)."""
    sha256: str
    """Content hash of an already-uploaded blob (from `POST /v1/files`)."""
    session_id: str | None = None
    """Required for a session-scoped name; ignored for `persist/`."""
    dedup: Literal["append_count", "overwrite", "error"] = "append_count"


class DeleteNameRequest(BaseModel):
    name: str
    """Stash name to unbind: `{filename}` (session) or `persist/{filename}`."""
    session_id: str | None = None
    """Required for a session-scoped name; ignored for `persist/`."""


async def _resolve_scope(
    scope_q: queries.Scope,
    *,
    persistent: bool,
    session_id: str | None,
) -> str:
    """Compute the directory scope key, enforcing session ownership for
    session-scoped names (the persistent scope is user-wide). Raises
    400 / 404 exactly like the `upload` endpoint's session check."""
    if not persistent:
        if session_id is None:
            raise HTTPException(
                status_code=400,
                detail="session_id is required for a session-scoped name",
            )
        if await scope_q.get_session(session_id) is None:
            raise HTTPException(
                status_code=404,
                detail="session_id not found in caller's tree",
            )
    return _scope_for(persistent, session_id or "")


@router.post("/names", status_code=201)
async def bind_name(
    body: BindNameRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, str]:
    """Bind an already-uploaded blob to a NAME in the caller's stash.

    Mirrors the `FileStore` frame for a gateway agent that holds a
    session JWT but no task context — same dedup policy, scope keys, and
    quota gate. Returns the ACTUAL saved name (may differ after an
    `append_count` collision — reference THIS, not the requested name)."""
    parsed = _split_stash_name(body.name)
    if parsed is None:
        raise HTTPException(status_code=400, detail="invalid filename")
    persistent, bare = parsed

    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope_q = queries.Scope.user(conn, principal.user_id)
        scope = await _resolve_scope(
            scope_q, persistent=persistent, session_id=body.session_id
        )
        blob = await scope_q.get_file_by_sha256(body.sha256)
        if blob is None:
            raise HTTPException(status_code=404, detail="blob not found")

        # Quota gate BEFORE allocation (worst case = full byte_size; an
        # idempotent re-bind of the same blob adds nothing). Mirrors
        # `_handle_file_store`.
        existing = await scope_q.resolve_file_name(scope, bare)
        is_idempotent = existing is not None and existing.file_id == blob.file_id
        worst_add = 0 if is_idempotent else blob.byte_size
        if not await _quota_ok(state, scope_q, principal.user_id, worst_add):
            raise HTTPException(status_code=413, detail="storage quota exceeded")

        async with conn.transaction():
            saved, err, _added = await _allocate_name(
                scope_q, scope=scope, filename=bare, file_id=blob.file_id,
                byte_size=blob.byte_size, dedup=body.dedup,
            )
            if err is not None:
                # Only `filename_exists` (a dedup="error" collision).
                raise HTTPException(status_code=409, detail=err)
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="file.store",
                target_kind="file",
                target_id=f"{scope}/{saved}",
                payload={"byte_size": blob.byte_size, "dedup": body.dedup},
            )

    return {"saved_name": _display_name(persistent, saved)}


@router.delete("/names")
async def delete_name(
    body: DeleteNameRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, int]:
    """Unbind a NAME from the caller's stash (session or `persist/`). The blob
    is left for the refcount sweep, exactly like the WS `FileManage` delete.
    Returns `{"deleted": 0|1}` — 0 means the name wasn't bound (idempotent)."""
    parsed = _split_stash_name(body.name)
    if parsed is None:
        raise HTTPException(status_code=400, detail="invalid filename")
    persistent, bare = parsed

    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope_q = queries.Scope.user(conn, principal.user_id)
        scope = await _resolve_scope(
            scope_q, persistent=persistent, session_id=body.session_id
        )
        async with conn.transaction():
            deleted = await scope_q.delete_file_name(scope, bare)
            if deleted:
                await queries.append_audit_event(
                    conn,
                    actor_kind="user",
                    actor_id=principal.user_id,
                    event="file.delete",
                    target_kind="file",
                    target_id=f"{scope}/{bare}",
                    payload={},
                )
    return {"deleted": deleted}


@router.get("/names")
async def list_names(
    request: Request,
    session_id: str | None = Query(default=None),
    persistent: bool = Query(default=False),
    query: str | None = Query(default=None),
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, list[str]]:
    """List stash names in the caller's session (or `persist/`) scope,
    newest first. `query` is a literal case-insensitive substring."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope_q = queries.Scope.user(conn, principal.user_id)
        scope = await _resolve_scope(
            scope_q, persistent=persistent, session_id=session_id
        )
        rows = await scope_q.list_file_names(scope, query=query)
    return {"names": [_display_name(persistent, r.filename) for r in rows]}


@router.get("/names/resolve")
async def resolve_name(
    request: Request,
    name: str = Query(...),
    session_id: str | None = Query(default=None),
    principal: SessionPrincipal = Depends(require_authenticated),
) -> dict[str, str]:
    """Resolve a stash name to its blob `file_id` (+ a self-authorising
    fetch key) so the caller can then `GET /v1/files/{file_id}`."""
    parsed = _split_stash_name(name)
    if parsed is None:
        raise HTTPException(status_code=400, detail="invalid filename")
    persistent, bare = parsed

    state = request.app.state.bp
    settings = state.settings
    async with state.db_pool.acquire() as conn:
        scope_q = queries.Scope.user(conn, principal.user_id)
        scope = await _resolve_scope(
            scope_q, persistent=persistent, session_id=session_id
        )
        row = await scope_q.resolve_file_name(scope, bare)
        if row is None:
            raise HTTPException(status_code=404, detail="name not bound")

    return {
        "name": _display_name(persistent, bare),
        "file_id": row.file_id,
        "byte_size": str(row.byte_size),
        "path": f"/v1/files/{row.file_id}",
        "key": _mint_fetch_key(settings, row.file_id, principal.user_id),
    }


@router.get("/{file_id}")
async def download(
    file_id: str,
    request: Request,
    access: FileReadAccess = Depends(file_read_access),
):  # type: ignore[no-untyped-def]
    """Stream a file or 302 to its presigned URL.

    Dual-auth (`file_read_access`): a session principal (UI/admin)
    OR a router-minted `file-fetch` key (agent attachment fetch).
    A key is bound to one file_id — enforce it here so a key minted
    for file A can't read file B even though both hit this route.
    """
    if (
        access.via_key_file_id is not None
        and access.via_key_file_id != file_id
    ):
        raise HTTPException(status_code=403, detail="key not valid for this file")
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        if access.via_key_file_id is not None:
            # Keyed fetch: the capability (signature- + file-bound,
            # verified in `file_read_access`) is the sole
            # authorization — no user filter. A key may legitimately
            # reference another user's file (delegation /
            # forwarding).
            row = await queries.get_file_by_id(conn, file_id)
        else:
            # Session / admin principal: stays user-scoped.
            row = await queries.Scope.user(
                conn, access.user_id
            ).get_file(file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="file not found")
        # Audit row written inside the SAME connection (separate tx
        # via append_audit_event's internal `conn.transaction`) so
        # the download decision and the audit trail land atomically
        # from the operator's view. Pre-R6 the download path emitted
        # no audit event — every other principal-touching write
        # already audits, so post-incident triage for exfiltration
        # had no row. Wrapped: a failed audit must NOT block the
        # legitimate download (we still return the file).
        try:
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=access.user_id,
                event="file.downloaded",
                target_kind="file",
                target_id=file_id,
                payload={
                    "byte_size": row.byte_size,
                    "sha256": row.sha256,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "file_download_audit_failed",
                extra={
                    "event": "file_download_audit_failed",
                    "bp.user_id": access.user_id,
                    "bp.file_id": file_id,
                },
            )

    file_store = state.file_store
    settings = state.settings

    # Single source of truth for the download-hardening policy,
    # applied identically whether we stream the bytes or redirect to
    # a backend-direct (presigned) URL. Forcing `attachment` even
    # without a stored filename stops a malicious upload (e.g.
    # `text/html` posing as an avatar) opening inline; the MIME is
    # allowlisted/downgraded so the browser won't render it.
    disposition = (
        safe_filename_for_header(row.original_filename)
        if row.original_filename
        else "attachment"
    )
    safe_mime = _safe_response_mime(row.mime_type)

    # Optionally 302 to a backend-direct presigned URL (S3/GCS/R2) — pinning
    # the SAME disposition/MIME so the redirect can't bypass the hardening the
    # streamed branch applies (pre-fix: an S3-served `text/html` rendered inline
    # → stored XSS).
    #
    # Gated on `file_download_presigned` (default OFF). Every current consumer
    # of this route is an in-cluster, server-side caller — SDK agents, the
    # chatbot's fetch_file, and the webapp backend's fetch_file (which proxies
    # bytes to the browser) — and none sit on the object store's private
    # network, so a presigned URL pointing at e.g. `seaweedfs:8333` is
    # unresolvable to them (ConnectError). Stream through the router by default;
    # operators that front the store on a client-reachable host can opt in.
    if settings.file_download_presigned:
        presigned = await file_store.presigned_url(
            row.sha256,
            ttl_s=300,
            content_disposition=disposition,
            content_type=safe_mime,
        )
        if presigned:
            return RedirectResponse(presigned, status_code=302)

    # Streamed fallback. `nosniff` can only be set here (it can't
    # ride the presigned query params); the presigned path relies on
    # the forced `attachment` + sanitised type instead.
    headers = {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(row.byte_size),
    }

    stream = await file_store.open(row.sha256)
    return StreamingResponse(stream, media_type=safe_mime, headers=headers)


# Allowlist of mime types we're willing to echo back on download.
# Anything outside this set gets downgraded to
# `application/octet-stream` so a malicious uploader can't trick the
# browser into rendering arbitrary content inline. The DB row keeps
# the original `mime_type` for honesty (operators querying metadata
# see what was uploaded); the wire response is sanitised.
_SAFE_RESPONSE_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/octet-stream",
        "application/zip",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
        "text/csv",
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "video/mp4",
        "video/webm",
    }
)


def _safe_response_mime(mime_type: str | None) -> str:
    """Return `mime_type` if it's on the allowlist, else
    `application/octet-stream`. Strips parameters (e.g.
    `text/html; charset=utf-8` → `text/html`) before checking."""
    if not mime_type:
        return "application/octet-stream"
    base = mime_type.split(";", 1)[0].strip().lower()
    if base in _SAFE_RESPONSE_MIME_TYPES:
        return base
    return "application/octet-stream"
