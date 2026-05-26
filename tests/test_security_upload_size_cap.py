"""Tests for the upload-DoS guard.

Original upload code accumulated `chunks: list[bytes]` in memory with
no size cap, so any authenticated tier-N user could OOM the router
with a multi-GB upload. The fix: stream-hash with a mid-stream
`UploadTooLarge` raise the moment running size exceeds
``settings.max_upload_bytes``.

Tested directly against `hash_with_size_cap` since FastAPI's
`UploadFile` isn't installed in CI; the helper accepts any object
with an awaitable ``read(n) -> bytes``.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from bp_router.upload_utils import UploadTooLarge, hash_with_size_cap


class _BytesReader:
    """Async-readable wrapper around an in-memory bytes buffer.

    Mirrors the slice-and-return behaviour of fastapi's UploadFile —
    each `read(n)` returns up to `n` bytes from the current position
    and advances the cursor, returning `b""` at EOF.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


# ---------------------------------------------------------------------------
# Happy-path hashing
# ---------------------------------------------------------------------------


def test_small_upload_returns_correct_hash_and_size() -> None:
    payload = b"hello world"
    expected_hash = hashlib.sha256(payload).hexdigest()
    sha256, size = asyncio.run(
        hash_with_size_cap(_BytesReader(payload), max_bytes=1024)
    )
    assert sha256 == expected_hash
    assert size == len(payload)


def test_multi_chunk_upload_hashes_correctly() -> None:
    """Hash must be over the full byte-stream, not any one chunk."""
    payload = b"x" * (200 * 1024)  # 200 KiB → multiple 64 KiB reads
    expected_hash = hashlib.sha256(payload).hexdigest()
    sha256, size = asyncio.run(
        hash_with_size_cap(
            _BytesReader(payload), max_bytes=1024 * 1024
        )
    )
    assert sha256 == expected_hash
    assert size == len(payload)


def test_empty_upload_returns_empty_hash_and_zero_size() -> None:
    """Caller still maps size=0 to HTTP 400; the helper itself has no
    opinion on empty inputs."""
    sha256, size = asyncio.run(
        hash_with_size_cap(_BytesReader(b""), max_bytes=1024)
    )
    assert size == 0
    assert sha256 == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Size cap — the actual DoS guard
# ---------------------------------------------------------------------------


def test_upload_at_exact_boundary_succeeds() -> None:
    """Boundary case: size == max_bytes is allowed."""
    payload = b"x" * 100
    sha256, size = asyncio.run(
        hash_with_size_cap(_BytesReader(payload), max_bytes=100)
    )
    assert size == 100


def test_upload_one_byte_over_raises_immediately() -> None:
    payload = b"x" * 101
    with pytest.raises(UploadTooLarge) as exc_info:
        asyncio.run(
            hash_with_size_cap(_BytesReader(payload), max_bytes=100)
        )
    assert exc_info.value.max_bytes == 100
    assert exc_info.value.observed_bytes >= 101


def test_upload_dramatically_over_aborts_within_one_chunk() -> None:
    """Verify the guard fires within ONE chunk past the limit, not
    after spooling the whole body. We drive a small chunk_size to
    make the bound tight."""
    payload = b"x" * (10 * 1024 * 1024)  # 10 MiB
    chunk_size = 1024  # 1 KiB

    reader = _BytesReader(payload)
    with pytest.raises(UploadTooLarge):
        asyncio.run(
            hash_with_size_cap(
                reader, max_bytes=4096, chunk_size=chunk_size
            )
        )
    # The reader's cursor must NOT have advanced past
    # max_bytes + one chunk. Otherwise we'd be spooling unbounded RAM.
    assert reader._pos <= 4096 + chunk_size


def test_upload_too_large_message_includes_limit() -> None:
    """The detail string lands in the HTTP 413 body — make sure it's
    informative."""
    with pytest.raises(UploadTooLarge, match="max_upload_bytes=42"):
        asyncio.run(
            hash_with_size_cap(_BytesReader(b"x" * 100), max_bytes=42)
        )
