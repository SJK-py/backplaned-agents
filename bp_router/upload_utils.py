"""bp_router.upload_utils — Streaming hash + size guard for file uploads.

Lives outside `bp_router.api` so tests can drive the size-cap logic
without importing fastapi (the real upload handler binds to
`UploadFile`, which transitively imports the SDK).

The original upload code accumulated chunks into a list in memory:

    chunks: list[bytes] = []
    while True:
        chunk = await file.read(64*1024)
        if not chunk:
            break
        chunks.append(chunk)

That meant any authenticated tier-N user could OOM the router with a
multi-GB upload. The fix here:

  - Read the source in a single pass, hashing as we go.
  - Track running size; raise `UploadTooLarge` the moment it exceeds
    the configured cap, so the request short-circuits without
    spooling further bytes.
  - Don't accumulate. The caller then rewinds and streams a second
    time directly into the storage backend's `put`.

This is two-pass I/O — slightly more work than a single pass — but
content-addressed storage needs the sha256 *before* the put begins
(it IS the storage key), and FastAPI's `UploadFile` is backed by a
`SpooledTemporaryFile` that handles the seek-and-reread cleanly.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol


class _AsyncReader(Protocol):
    """Anything with an awaitable ``read(n)`` returning bytes. Matches
    fastapi's ``UploadFile`` and any async file-like in the test
    suite."""

    async def read(self, size: int = ...) -> bytes:
        ...


class UploadTooLarge(Exception):
    """The upload size exceeded `max_bytes` mid-stream. The caller is
    expected to map this to HTTP 413 (Payload Too Large)."""

    def __init__(self, *, observed_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"upload exceeds max_upload_bytes={max_bytes} "
            f"(observed {observed_bytes}+)"
        )
        self.observed_bytes = observed_bytes
        self.max_bytes = max_bytes


async def hash_with_size_cap(
    reader: Any,  # _AsyncReader, but Protocol can't enforce at runtime
    *,
    max_bytes: int,
    chunk_size: int = 64 * 1024,
) -> tuple[str, int]:
    """Hash the entire source while enforcing a size cap.

    Returns ``(sha256_hex, total_bytes)``. Raises ``UploadTooLarge``
    the moment the running total exceeds ``max_bytes`` — the loop
    aborts without reading the rest, so an attacker streaming an
    enormous body doesn't get to spool more than `max_bytes + chunk_size`
    into the upstream's transport buffer (a 64 KiB ceiling, which is
    how much a single ``read()`` returns).
    """
    h = hashlib.sha256()
    size = 0
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise UploadTooLarge(observed_bytes=size, max_bytes=max_bytes)
        h.update(chunk)
    return h.hexdigest(), size
