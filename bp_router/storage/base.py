"""bp_router.storage.base — FileStore protocol.

All implementations are content-addressed by sha256: the same content
under different filenames stores once.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FileMeta:
    sha256: str
    byte_size: int
    mime_type: str | None = None
    original_filename: str | None = None


class FileStore(Protocol):
    """Pluggable backend for file-store blob bytes.

    Implementations: `LocalFileStore`, `S3FileStore`. Selection happens
    in `bp_router.storage.build_file_store` based on `settings.file_store`.

    The `sha256` argument is the canonical identifier. Implementations
    SHOULD verify that uploaded content matches the claimed hash.
    """

    backend_name: str
    """Short string used in metric labels (`local`, `s3`, ...)."""

    async def put(
        self,
        sha256: str,
        src: AsyncIterable[bytes],
        meta: FileMeta,
    ) -> str:
        """Stream content under the hash. Returns a backend-internal URL.

        The URL is opaque to callers and is stored in `files.storage_url`.
        Subsequent `open` / `presigned_url` / `delete` calls take the URL
        OR the sha256 — implementations may use either.
        """
        ...

    async def open(self, sha256: str) -> AsyncIterator[bytes]:
        """Stream the bytes of a stored object."""
        ...

    async def presigned_url(
        self,
        sha256: str,
        *,
        ttl_s: int,
        content_disposition: str | None = None,
        content_type: str | None = None,
    ) -> str | None:
        """Backend-direct URL with TTL, or None if unsupported.

        Returning a URL lets callers redirect clients straight to the
        backend, taking the router out of the byte path. Local
        filesystem returns None; S3/GCS/R2 return signed URLs.

        `content_disposition` / `content_type`: when the backend
        serves the bytes directly, it MUST pin the response's
        `Content-Disposition` / `Content-Type` to these values so the
        backend-direct path inherits the SAME download-forcing +
        MIME-sanitisation the streamed path applies. Otherwise an
        uploaded `text/html` renders inline off the backend origin
        (stored XSS). The router computes both with the same helpers
        used for the streamed response — single source of truth.
        """
        ...

    async def delete(self, sha256: str) -> None:
        """Best-effort delete. Idempotent — missing object is not an error."""
        ...

    async def exists(self, sha256: str) -> bool:
        ...
