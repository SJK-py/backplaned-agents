"""bp_router.storage.local — Local filesystem FileStore.

Content-addressed under `<root>/<sha256[:2]>/<sha256[2:4]>/<sha256>`.
Suitable for single-node deployments and tests; not safe for
multi-worker without a shared filesystem.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

from bp_router.storage.base import FileMeta, FileStore

# Hex-only sha256: exactly 64 lowercase hex chars. Used by `_path` to
# refuse path-traversal payloads. Without the validation,
# `Path / ".." / "etc/passwd"` flows
# through to `open()` because `Path` does NOT collapse `..` segments
# itself. Today the only producer is `hash_with_size_cap` which always
# yields valid hex, but `_path` is also called on values fetched from
# the DB during download/delete — defence-in-depth means refusing the
# bad shape at the boundary regardless of what the caller claims.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_sha256(sha256: str) -> None:
    """Raise ValueError if `sha256` isn't 64 lowercase hex chars.

    Centralised so every storage backend can reuse it without
    duplicating the regex.
    """
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError(
            "sha256 must be exactly 64 lowercase hex chars; "
            "received a value that didn't match — refusing to "
            "construct a storage path from it"
        )


class LocalFileStore(FileStore):
    backend_name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_options(cls, options: dict) -> LocalFileStore:
        path = options.get("path", "./router_files")
        return cls(Path(path))

    # ------------------------------------------------------------------

    def _path(self, sha256: str) -> Path:
        # `Path` doesn't collapse `..` segments — without validation,
        # `sha256 = "../../../etc/passwd"` would flow through to
        # `open()` outside the storage root. Hex-only check refuses
        # the bad shape upfront.
        _validate_sha256(sha256)
        return self.root / sha256[:2] / sha256[2:4] / sha256

    async def put(
        self, sha256: str, src: AsyncIterable[bytes], meta: FileMeta
    ) -> str:
        dest = self._path(sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")

        h = hashlib.sha256()
        size = 0

        def _writer():  # type: ignore[no-untyped-def]
            return open(tmp, "wb")

        f = await asyncio.to_thread(_writer)
        try:
            async for chunk in src:
                size += len(chunk)
                h.update(chunk)
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)

        actual = h.hexdigest()
        if actual != sha256:
            await asyncio.to_thread(tmp.unlink, missing_ok=True)  # type: ignore[arg-type]
            raise ValueError(f"sha256 mismatch: claimed {sha256}, actual {actual}")
        if size != meta.byte_size and meta.byte_size > 0:
            await asyncio.to_thread(tmp.unlink, missing_ok=True)  # type: ignore[arg-type]
            raise ValueError(f"size mismatch: claimed {meta.byte_size}, actual {size}")

        await asyncio.to_thread(os.replace, tmp, dest)
        return f"file://{dest}"

    async def open(self, sha256: str) -> AsyncIterator[bytes]:
        path = self._path(sha256)

        async def _gen() -> AsyncIterator[bytes]:
            f = await asyncio.to_thread(open, path, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(f.read, 65_536)
                    if not chunk:
                        return
                    yield chunk
            finally:
                await asyncio.to_thread(f.close)

        return _gen()

    async def presigned_url(
        self,
        sha256: str,
        *,
        ttl_s: int,
        content_disposition: str | None = None,
        content_type: str | None = None,
    ) -> str | None:
        # Local filesystem cannot issue presigned URLs — caller must
        # use /v1/files/{id}, which applies the disposition/MIME
        # hardening itself. Signature mirrors the ABC.
        return None

    async def delete(self, sha256: str) -> None:
        path = self._path(sha256)
        await asyncio.to_thread(path.unlink, missing_ok=True)  # type: ignore[arg-type]

    async def exists(self, sha256: str) -> bool:
        return await asyncio.to_thread(self._path(sha256).is_file)
