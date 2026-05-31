"""bp_sdk.files — Per-task `FileStash`, the agent-facing handle on
the router-managed named file store.

Files are addressed by NAME; bytes never ride the agent→router WS
frame (upload over HTTP, read over a signed URL, LLM feeding via a
name reference the router resolves). See `FileStash` for the API.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import shutil
import uuid
from collections.abc import AsyncIterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from bp_protocol.frames import (
    CopyFileRequest,
    DeleteFileRequest,
    FileFetchFrame,
    FileManageFrame,
    FileResultFrame,
    FileStoreFrame,
    FileUploadGrantFrame,
    FileUploadRequestFrame,
    ListFileRequest,
    WriteFileRequest,
)

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext

logger = logging.getLogger(__name__)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class FileStash:
    """Per-task handle on the router-managed named file store
    (`docs/design/router-managed-file-store.md`).

    Files are addressed by NAME — `{filename}` in the session's
    ephemeral stash (GC'd on session close) or `persist/{filename}`
    in the user-wide persistent stash. Every method round-trips a
    `File*` frame to the router (which derives the authoritative
    `(user_id, session_id)` from this task), and returns names, not
    opaque refs — so a peer in the same user+session can reach a
    file by mentioning its name.

    Bytes never ride the agent→router WS frame: `store` uploads over
    a separate HTTP connection (the content-bound upload-with-grant
    path); `read` pulls over a short-TTL signed URL; LLM feeding uses
    `llm_ref` (a name reference the ROUTER resolves into the provider
    call).
    """

    def __init__(
        self,
        ctx: TaskContext,
        *,
        inbox_dir: Path,
        router_url: str,
        dispatcher: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._inbox_dir = inbox_dir
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        self._router_url = router_url.rstrip("/")
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------
    # Frame round-trip
    # ------------------------------------------------------------------

    async def _round_trip(self, frame: Any) -> FileResultFrame:
        """Send a File* frame and await the correlated `FileResult`
        on `pending_acks` (rejected immediately if the owning handler
        exits, like `peers.spawn`). Raises on a router-reported
        `error`."""
        d = self._dispatcher
        if d is None:
            raise RuntimeError(
                "FileStash requires a dispatcher (external agent context)"
            )
        fut = d.register_for_task(
            d.pending_acks, frame.correlation_id, self._ctx.task_id
        )
        await d.transport.send(frame)
        try:
            res = await fut
        except TimeoutError as exc:
            raise RuntimeError("file operation timed out") from exc
        if not isinstance(res, FileResultFrame):
            raise RuntimeError("unexpected response to file operation")
        if res.error is not None:
            raise FileStoreError(res.error)
        return res

    def _store_frame_base(self) -> dict[str, Any]:
        d = self._dispatcher
        if d is None:
            raise RuntimeError(
                "FileStash requires a dispatcher (external agent context)"
            )
        return {
            "agent_id": d.agent.info.agent_id,
            "trace_id": self._ctx.trace_id,
            "span_id": self._ctx.span_id,
            "task_id": self._ctx.task_id,
        }

    # ------------------------------------------------------------------
    # Store (bytes/path/stream) → name
    # ------------------------------------------------------------------

    async def store(
        self,
        src: Path | bytes | AsyncIterable[bytes],
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        persistent: bool = False,
        dedup: str = "append_count",
    ) -> str:
        """Upload bytes and bind them to a name. Returns the ACTUAL
        saved name (which may differ from `filename` after a dedup
        append — always use the returned value, never assume).

        Bytes stream to the router over HTTP (the content-bound
        upload-with-grant path); the returned `FileStore` frame names
        the blob under the session (`persistent=False`) or persistent
        (`persistent=True`) scope."""
        upload_path, sha, size, resolved_filename, mime = await self._materialise(
            src, filename, mime_type
        )
        await self._upload_blob(upload_path, sha, size, mime, resolved_filename)
        res = await self._round_trip(
            FileStoreFrame(
                **self._store_frame_base(),
                sha256=sha,
                byte_size=size,
                filename=filename or resolved_filename,
                persistent=persistent,
                dedup=dedup,  # type: ignore[arg-type]
                mime_type=mime,
            )
        )
        assert res.saved_name is not None
        return res.saved_name

    async def write(
        self,
        filename: str,
        text: str,
        *,
        persistent: bool = False,
        dedup: str = "append_count",
    ) -> str:
        """Write a text file inline (no upload round-trip). Returns
        the saved name."""
        res = await self._round_trip(
            FileManageFrame(
                **self._store_frame_base(),
                command=WriteFileRequest(
                    filename=filename, text=text, persistent=persistent,
                    dedup=dedup,  # type: ignore[arg-type]
                ),
            )
        )
        assert res.saved_name is not None
        return res.saved_name

    # ------------------------------------------------------------------
    # Read a name → local bytes (agent-side; NOT the LLM-feed path)
    # ------------------------------------------------------------------

    async def read(self, name: str) -> Path:
        """Fetch a stash file's bytes to a local path. For an agent
        that needs to PROCESS the bytes itself — NOT the LLM-feed
        path (use `llm_ref` to show a file to an LLM; the router
        resolves that without the bytes ever entering the agent)."""
        res = await self._round_trip(
            FileFetchFrame(**self._store_frame_base(), name=name)
        )
        assert res.fetch_url and res.fetch_token
        dest = self._inbox_dir / f"{uuid.uuid4().hex}_{name.replace('/', '_')}"
        url = f"{self._router_url}{res.fetch_url}"
        # follow_redirects: the download endpoint 302s to a backend-direct
        # presigned URL when the store supports it (e.g. SeaweedFS); without
        # this the SDK gets the raw 302 and raise_for_status() throws. httpx
        # drops the Authorization header on a cross-host redirect, so the
        # router bearer token isn't leaked to the object store — and the
        # presigned URL carries its own signature, so it doesn't need it.
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            async with client.stream(
                "GET", url,
                headers={"Authorization": f"Bearer {res.fetch_token}"},
            ) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        return dest

    async def read_bytes(self, name: str) -> bytes:
        path = await self.read(name)
        return await asyncio.to_thread(path.read_bytes)

    # ------------------------------------------------------------------
    # Manage
    # ------------------------------------------------------------------

    async def list(
        self,
        *,
        persistent: bool = False,
        query: str | None = None,
        stored_after: Any | None = None,
    ) -> list[str]:
        res = await self._round_trip(
            FileManageFrame(
                **self._store_frame_base(),
                command=ListFileRequest(
                    persistent=persistent, query=query,
                    stored_after=stored_after,
                ),
            )
        )
        return res.names or []

    async def delete(self, name: str) -> int:
        """Delete a name (or a `*`-glob). Returns the count removed."""
        res = await self._round_trip(
            FileManageFrame(
                **self._store_frame_base(),
                command=DeleteFileRequest(name=name),
            )
        )
        return res.deleted_count or 0

    async def copy(self, src: str, dst: str, *, move: bool = False) -> str:
        """Copy (or move, `move=True`) a stash file. Returns the saved
        destination name."""
        res = await self._round_trip(
            FileManageFrame(
                **self._store_frame_base(),
                command=CopyFileRequest(src=src, dst=dst, delete_original=move),
            )
        )
        assert res.saved_name is not None
        return res.saved_name

    # ------------------------------------------------------------------
    # LLM feeding — a NAME reference the ROUTER resolves (no bytes here)
    # ------------------------------------------------------------------

    def llm_ref(
        self, name: str, *, as_: str | None = None
    ) -> dict[str, Any]:
        """Build a `file_ref` content part referencing a stash file by
        NAME for an LLM message. The router resolves the name into the
        provider call (bytes never cross the agent→router frame, so a
        file over the WS cap is fed without tripping it). `as_` picks
        the modality envelope (`image` / `document`); defaults are
        inferred at the router from the blob's mime type."""
        ref: dict[str, Any] = {"name": name}
        if as_ is not None:
            ref["as"] = as_
        return {"file_ref": ref}

    # ------------------------------------------------------------------
    # Internal: materialise + upload (mirrors the upload-with-grant
    # path; bulk bytes never touch the ws control pump)
    # ------------------------------------------------------------------

    async def _materialise(
        self,
        src: Path | bytes | AsyncIterable[bytes],
        filename: str | None,
        mime_type: str | None,
    ) -> tuple[Path, str, int, str, str | None]:
        if isinstance(src, Path):
            sha = await asyncio.to_thread(_file_sha256, src)
            size = src.stat().st_size
            fn = filename or src.name
            mime = mime_type or mimetypes.guess_type(str(src))[0]
            return src, sha, size, fn, mime
        tmp = self._inbox_dir / f"upload_{uuid.uuid4().hex}"
        h = hashlib.sha256()
        size = 0
        with tmp.open("wb") as fh:
            if isinstance(src, bytes):
                fh.write(src)
                h.update(src)
                size = len(src)
            else:
                async for chunk in src:
                    fh.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        fn = filename or tmp.name
        mime = mime_type or (mimetypes.guess_type(fn)[0] if filename else None)
        return tmp, h.hexdigest(), size, fn, mime

    async def _upload_blob(
        self, path: Path, sha256: str, byte_size: int,
        mime_type: str | None, filename: str,
    ) -> None:
        d = self._dispatcher
        if d is None:
            raise RuntimeError("FileStash.store() requires a dispatcher")
        req = FileUploadRequestFrame(
            agent_id=d.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=self._ctx.task_id,
            sha256=sha256,
            byte_size=byte_size,
            mime_type=mime_type,
            filename=filename,
        )
        fut = d.register_for_task(
            d.pending_acks, req.correlation_id, self._ctx.task_id
        )
        await d.transport.send(req)
        grant = await fut
        if not isinstance(grant, FileUploadGrantFrame):
            raise RuntimeError("unexpected response to upload negotiation")
        if grant.error is not None:
            raise FileStoreError(grant.error)
        if not grant.upload_url or not grant.upload_token:
            raise RuntimeError("upload grant carried no url+token")
        url = f"{self._router_url}{grant.upload_url}"
        async with httpx.AsyncClient(timeout=300.0) as client:
            with path.open("rb") as fh:
                resp = await client.post(
                    url,
                    files={"file": (filename, fh,
                                    mime_type or "application/octet-stream")},
                    headers={"Authorization": f"Bearer {grant.upload_token}"},
                )
                resp.raise_for_status()

    async def cleanup(self) -> None:
        """Delete the per-task inbox (downloaded `read()` bytes +
        upload spool files). Called by the dispatcher on task
        teardown. Best-effort — the router-side stash is unaffected."""
        try:
            await asyncio.to_thread(
                shutil.rmtree, self._inbox_dir, ignore_errors=True
            )
        except Exception:  # noqa: BLE001
            logger.debug("inbox cleanup failed", exc_info=True)


class FileStoreError(RuntimeError):
    """A router-reported file-operation error (the `FileResult.error`
    code: denied / quota_exceeded / filename_exists / not_found /
    invalid_filename / too_large / rate_limited)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
