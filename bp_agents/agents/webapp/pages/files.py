"""bp_agents.agents.webapp.pages.files — the file stash pane ([webapp.md] §7).

List + upload + download over the router's name store, with the user's own
token. Two scopes: the session stash (`session:{id}`, ephemeral, GC'd on
close) and the user-wide persistent stash (`persist/`). Bound to a session
because the session scope needs a session_id; the persistent tab is shown
alongside.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse

from bp_agents.agents.webapp.pages._common import owned_session
from bp_agents.agents.webapp.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()

# Cap a single browser upload (the router enforces its own max; this just
# avoids spooling an absurd body before the upstream rejects it).
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Bound concurrent resolve+fetch when bundling a stash into a .zip.
_ARCHIVE_CONCURRENCY = 8


def _content_disposition(filename: str) -> str:
    """Build an RFC 6266 / RFC 5987 attachment ``Content-Disposition`` value.

    HTTP header values must be latin-1, so interpolating a non-ASCII filename
    (e.g. a Korean / Japanese name) straight into ``filename="..."`` makes
    Starlette raise ``UnicodeEncodeError`` while building the response → a 500
    that the browser saves AS the file ("Internal Server Error"). Emit an
    ASCII-safe ``filename="..."`` (defanged) plus ``filename*=UTF-8''<percent>``
    for modern clients. Mirrors ``bp_router.filename_utils`` — kept local
    because the agent suite doesn't depend on the router package.
    """
    base = filename.replace("\\", "/").split("/")[-1] or "download"
    ascii_safe = base.encode("ascii", "ignore").decode("ascii")
    # Collapse unsafe chars to underscore so filename="..." stays a well-formed
    # quoted string (also guards CR/LF header injection).
    ascii_safe = re.sub(r'[\\"\x00-\x1f\x7f]', "_", ascii_safe) or "download"
    star = quote(base, safe="")
    return f"attachment; filename=\"{ascii_safe}\"; filename*=UTF-8''{star}"


@router.get("/files/{session_id}", response_class=HTMLResponse)
async def stash_view(
    session_id: str, request: Request, tab: str = "session"
) -> HTMLResponse:
    info = await owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    upstream = request.app.state.upstream
    access = request.session["access_token"]
    error = None
    session_files: list[str] = []
    persist_files: list[str] = []
    try:
        session_files = await upstream.list_names(
            access_token=access, session_id=session_id, persistent=False
        )
        persist_files = await upstream.list_names(
            access_token=access, persistent=True
        )
    except UpstreamError as exc:
        logger.warning(
            "webapp_stash_list_failed",
            extra={"event": "webapp_stash_list_failed", "status_code": exc.status_code},
        )
        error = "Couldn't load your files. Please retry."

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "files/stash.html",
        {
            "session_id": session_id,
            "session_files": session_files,
            "persist_files": persist_files,
            "tab": "persist" if tab == "persist" else "session",
            "error": error,
            "active_section": "sessions",
        },
    )


@router.post("/files/{session_id}")
async def stash_upload(
    session_id: str,
    request: Request,
    file: UploadFile,
    scope: str = Form("session"),
) -> Response:
    info = await owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    persistent = scope == "persist"
    data = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    filename = (file.filename or "upload").rsplit("/", 1)[-1]
    upstream = request.app.state.upstream
    access = request.session["access_token"]
    try:
        await upstream.upload_file(
            access_token=access, filename=filename, data=data,
            session_id=session_id, persistent=persistent,
            mime_type=file.content_type,
        )
    except UpstreamError as exc:
        logger.warning(
            "webapp_stash_upload_failed",
            extra={"event": "webapp_stash_upload_failed", "status_code": exc.status_code},
        )
        raise HTTPException(status_code=502) from exc
    tab = "persist" if persistent else "session"
    return Response(
        status_code=204, headers={"HX-Redirect": f"/files/{session_id}?tab={tab}"}
    )


@router.get("/files/{session_id}/download.zip")
async def download_archive(
    session_id: str, request: Request, tab: str = "session"
) -> Response:
    """Bundle every file in the chosen stash tab into one .zip — the user's own
    token. Declared BEFORE the `{name:path}` download route so the literal
    `download.zip` path isn't captured by it (a file actually named
    `download.zip` is the only collision — vanishingly rare and benign)."""
    info = await owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    persistent = tab == "persist"
    upstream = request.app.state.upstream
    access = request.session["access_token"]
    try:
        names = await upstream.list_names(
            access_token=access,
            session_id=None if persistent else session_id,
            persistent=persistent,
        )
    except UpstreamError as exc:
        logger.warning(
            "webapp_stash_archive_list_failed",
            extra={"event": "webapp_stash_archive_list_failed", "status_code": exc.status_code},
        )
        raise HTTPException(status_code=502) from exc
    if not names:
        raise HTTPException(status_code=404, detail="no files to download")

    sem = asyncio.Semaphore(_ARCHIVE_CONCURRENCY)

    async def _fetch(name: str) -> tuple[str, bytes] | None:
        """`(arcname, bytes)` for one stash name, or None if it can't be read
        (a single bad file is skipped rather than failing the whole archive)."""
        async with sem:
            try:
                file_id = await upstream.resolve_named_file(
                    access_token=access, session_id=session_id, name=name
                )
                if file_id is None:
                    return None
                data = await upstream.fetch_file(access_token=access, file_id=file_id)
            except UpstreamError as exc:
                logger.warning(
                    "webapp_stash_archive_fetch_failed",
                    extra={
                        "event": "webapp_stash_archive_fetch_failed",
                        "status_code": exc.status_code,
                    },
                )
                return None
        # Persist names list as `persist/<file>`; drop the prefix in the zip.
        arcname = name[len("persist/"):] if name.startswith("persist/") else name
        return arcname, data

    fetched = await asyncio.gather(*(_fetch(n) for n in names))

    buf = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in fetched:
            if item is None:
                continue
            arcname, data = item
            if arcname in seen:  # names are unique per scope; guard anyway
                continue
            seen.add(arcname)
            zf.writestr(arcname, data)
    if not seen:
        raise HTTPException(status_code=502, detail="could not read any files")

    archive_name = f"{'persistent' if persistent else 'session'}-stash.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(archive_name)},
    )


@router.get("/files/{session_id}/{name:path}")
async def download_file(session_id: str, name: str, request: Request) -> Response:
    """Resolve a stash NAME (`{file}` session-scoped, or `persist/{file}`)
    to its blob and stream the bytes — the user's own token. Powers the
    chat answer's download chips and the stash list links."""
    info = await owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    upstream = request.app.state.upstream
    access = request.session["access_token"]
    try:
        file_id = await upstream.resolve_named_file(
            access_token=access, session_id=session_id, name=name
        )
        if file_id is None:
            raise HTTPException(status_code=404)
        data = await upstream.fetch_file(access_token=access, file_id=file_id)
    except UpstreamError as exc:
        logger.warning(
            "webapp_file_download_failed",
            extra={"event": "webapp_file_download_failed", "status_code": exc.status_code},
        )
        raise HTTPException(status_code=502) from exc
    filename = name.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": _content_disposition(filename)},
    )
