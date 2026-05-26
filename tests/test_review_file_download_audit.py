"""File-storage download-hardening pins (third-pass review HIGH fixes).

  1. `GET /v1/files/{file_id}` writes a `file.downloaded` audit
     event. Pre-R6: downloads were not audited at all, leaving
     post-incident triage with no record of who pulled what bytes.
  2. A backend-direct (presigned) redirect inherits the same
     download-forcing disposition + MIME sanitisation as the streamed
     path, so an S3-served `text/html` can't render inline.

Source-pin + functional pins.
"""

from __future__ import annotations

import inspect

import pytest


def test_download_handler_writes_audit_event() -> None:
    """Source pin: the `download` handler calls
    `append_audit_event` with event `file.downloaded` and the
    file_id as target."""
    pytest.importorskip("fastapi")
    from bp_router.api import files

    src = inspect.getsource(files.download)
    assert "append_audit_event" in src
    assert '"file.downloaded"' in src
    assert "target_id=file_id" in src
    assert "target_kind=\"file\"" in src


def test_download_audit_failure_does_not_block_download() -> None:
    """The audit-write is wrapped in try/except — a transient DB
    hiccup must not prevent the legitimate download. Pin the
    try/except + the warn log shape so a refactor that promotes
    the audit error doesn't accidentally break the file API."""
    pytest.importorskip("fastapi")
    from bp_router.api import files

    src = inspect.getsource(files.download)
    assert "except Exception" in src
    assert "file_download_audit_failed" in src


def test_download_audit_runs_inside_acquire_block() -> None:
    """Source pin: the audit_event call lives inside the same
    `async with pool.acquire()` as the file row lookup. Keeping
    them on one connection means a transient DB hiccup affects
    both (or neither), not a split-brain where the download
    happens but the audit silently misses."""
    pytest.importorskip("fastapi")
    from bp_router.api import files

    src = inspect.getsource(files.download)
    # Indented relative to the `async with state.db_pool.acquire`.
    # Brittle but the cleanest pin — refactor lifting the audit
    # OUTSIDE the connection would fail this.
    assert (
        src.index("append_audit_event")
        > src.index("state.db_pool.acquire")
    )
    assert (
        src.index("append_audit_event")
        < src.index("file_store = state.file_store")
    )


def test_s3_presigned_url_pins_disposition_and_content_type() -> None:
    """L3 source pin: `S3FileStore.presigned_url` forwards the
    router-computed `content_disposition`/`content_type` into the
    signed request as `ResponseContentDisposition` /
    `ResponseContentType`, so a backend-direct redirect inherits the
    same download-forcing / MIME-sanitisation as the streamed path
    (S3 can't carry `nosniff`, so attachment + safe type is the
    defence on that path)."""
    s3mod = pytest.importorskip("bp_router.storage.s3")

    src = inspect.getsource(s3mod.S3FileStore.presigned_url)
    assert "ResponseContentDisposition" in src
    assert "ResponseContentType" in src
    assert "content_disposition" in src
    assert "content_type" in src
