"""S3 backend streams uploads via multipart (R6 HIGH).

Pre-R6 `S3FileStore.put` accumulated every chunk into a `list[bytes]`
and then `b"".join`'d it, peaking RAM at ~2× the upload size. With
`max_upload_bytes` raised for video / model-weight workloads (per
the settings docstring), N concurrent uploaders × 2 × cap was a
real DOS axis.

Fix: stream via `create_multipart_upload` / `upload_part` /
`complete_multipart_upload` with `abort_multipart_upload` on any
exception (including `asyncio.CancelledError`, so a client
disconnect mid-upload doesn't leak a billable S3 orphan).

These tests pin the wire-call sequence using a stubbed S3 client
— they don't exercise a real bucket. Functional verification
against a real backend lives in integration suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect

import pytest


def _make_store():  # type: ignore[no-untyped-def]
    pytest.importorskip("bp_router.storage.s3")
    from bp_router.storage.s3 import S3FileStore

    return S3FileStore(bucket="test-bucket", prefix="test")


class _StubS3Client:
    """Records every method call. `aboto3.client('s3')` shape."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.parts: list[dict] = []
        self.completed: list[dict] = []
        self.aborted: list[dict] = []
        self._upload_id = "upload-abc"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def create_multipart_upload(self, **kwargs):
        self.created.append(kwargs)
        return {"UploadId": self._upload_id}

    async def upload_part(self, **kwargs):
        self.parts.append(kwargs)
        return {"ETag": f"etag-{kwargs['PartNumber']}"}

    async def complete_multipart_upload(self, **kwargs):
        self.completed.append(kwargs)
        return {"Bucket": kwargs["Bucket"], "Key": kwargs["Key"]}

    async def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs)
        return None


def _patch_client(store: object, client: _StubS3Client) -> None:
    """Swap `_client()` so it returns the stub."""
    def _factory():  # type: ignore[no-untyped-def]
        return client
    store._client = _factory  # type: ignore[attr-defined]


def _async_iter(chunks: list[bytes]):
    async def _gen():
        for c in chunks:
            yield c
    return _gen()


def test_small_upload_uses_one_part() -> None:
    """A file smaller than _MULTIPART_PART_SIZE (8 MiB) flushes
    as a single part on EOF. The final-part-after-loop branch
    handles this; pre-R6 there was no part split at all."""
    pytest.importorskip("aioboto3")
    from bp_router.storage.base import FileMeta

    async def _run() -> None:
        store = _make_store()
        client = _StubS3Client()
        _patch_client(store, client)

        body = b"hello world " * 100  # ~1.2 KiB
        sha = hashlib.sha256(body).hexdigest()

        url = await store.put(
            sha,
            _async_iter([body]),
            FileMeta(
                sha256=sha,
                byte_size=len(body),
                mime_type="text/plain",
                original_filename="t.txt",
            ),
        )

        assert url.startswith("s3://test-bucket/")
        assert len(client.created) == 1
        assert client.created[0]["ContentType"] == "text/plain"
        # Exactly one part — small upload fits in the trailing flush.
        assert len(client.parts) == 1
        assert client.parts[0]["PartNumber"] == 1
        assert len(client.completed) == 1
        # The complete call carries the etag list.
        completed_parts = client.completed[0]["MultipartUpload"]["Parts"]
        assert len(completed_parts) == 1
        # No abort on the success path.
        assert client.aborted == []

    asyncio.run(_run())


def test_large_upload_splits_into_multiple_parts() -> None:
    """Verify the 8 MiB part-size flush path. Feed 20 MiB → 3
    parts (8, 8, 4)."""
    pytest.importorskip("aioboto3")
    from bp_router.storage.base import FileMeta

    async def _run() -> None:
        store = _make_store()
        client = _StubS3Client()
        _patch_client(store, client)

        body = b"a" * (20 * 1024 * 1024)
        sha = hashlib.sha256(body).hexdigest()

        await store.put(
            sha,
            _async_iter([body]),
            FileMeta(
                sha256=sha,
                byte_size=len(body),
                mime_type=None,
                original_filename=None,
            ),
        )

        # 3 upload_part calls: 8 MiB, 8 MiB, 4 MiB.
        assert len(client.parts) == 3
        # Per-part size pin.
        sizes = [len(p["Body"]) for p in client.parts]
        assert sizes == [8 * 1024 * 1024, 8 * 1024 * 1024, 4 * 1024 * 1024]
        # Sequential part numbers.
        nums = [p["PartNumber"] for p in client.parts]
        assert nums == [1, 2, 3]
        assert len(client.completed) == 1


def test_sha_mismatch_aborts_multipart() -> None:
    """A sha mismatch raises BEFORE complete — so the abort
    branch fires, preventing a billable orphan multipart session."""
    pytest.importorskip("aioboto3")
    from bp_router.storage.base import FileMeta

    async def _run() -> None:
        store = _make_store()
        client = _StubS3Client()
        _patch_client(store, client)

        body = b"actual content"
        claimed = "a" * 64  # bogus sha

        with pytest.raises(ValueError) as exc_info:
            await store.put(
                claimed,
                _async_iter([body]),
                FileMeta(
                    sha256=claimed, byte_size=len(body),
                    mime_type=None, original_filename=None,
                ),
            )
        assert "sha256 mismatch" in str(exc_info.value)
        # Abort fired with the same upload_id.
        assert len(client.aborted) == 1
        assert client.aborted[0]["UploadId"] == client._upload_id
        # NO complete call.
        assert client.completed == []

    asyncio.run(_run())


def test_cancellation_mid_upload_aborts_multipart() -> None:
    """`asyncio.CancelledError` mid-iteration must also trigger
    the abort. Without `except BaseException` the cancel would
    propagate without aborting — leaking the multipart session."""
    pytest.importorskip("aioboto3")
    from bp_router.storage.base import FileMeta

    async def _bad_iter():
        yield b"a" * 1024
        raise asyncio.CancelledError("client disconnected")

    async def _run() -> None:
        store = _make_store()
        client = _StubS3Client()
        _patch_client(store, client)

        with pytest.raises(asyncio.CancelledError):
            await store.put(
                "0" * 64,
                _bad_iter(),
                FileMeta(
                    sha256="0" * 64, byte_size=0,
                    mime_type=None, original_filename=None,
                ),
            )
        assert len(client.aborted) == 1
        assert client.completed == []

    asyncio.run(_run())


def test_abort_failure_does_not_mask_original_exception() -> None:
    """If the abort itself raises (S3 transient error), the
    ORIGINAL exception still surfaces to the caller. The abort
    failure is logged but swallowed."""
    pytest.importorskip("aioboto3")
    from bp_router.storage.base import FileMeta

    class _AbortFailsClient(_StubS3Client):
        async def abort_multipart_upload(self, **kwargs):
            raise RuntimeError("S3 down")

    async def _run() -> None:
        store = _make_store()
        client = _AbortFailsClient()
        _patch_client(store, client)

        with pytest.raises(ValueError) as exc_info:
            await store.put(
                "0" * 64,
                _async_iter([b"x"]),
                FileMeta(
                    sha256="0" * 64, byte_size=1,
                    mime_type=None, original_filename=None,
                ),
            )
        # Original sha-mismatch error surfaces, not the abort failure.
        assert "sha256 mismatch" in str(exc_info.value)

    asyncio.run(_run())


def test_source_pin_multipart_constants() -> None:
    """Sanity: the part size + max-parts constants are present
    and within S3's documented bounds (5 MiB minimum, 10,000
    parts max)."""
    pytest.importorskip("aioboto3")
    from bp_router.storage.s3 import S3FileStore

    assert S3FileStore._MULTIPART_PART_SIZE >= 5 * 1024 * 1024
    assert S3FileStore._MULTIPART_MAX_PARTS == 10_000


def test_source_pin_put_uses_multipart_api() -> None:
    """Source pin: `put` calls the three multipart methods (not
    the old `put_object`)."""
    pytest.importorskip("aioboto3")
    from bp_router.storage import s3

    src = inspect.getsource(s3.S3FileStore.put)
    assert "create_multipart_upload" in src
    assert "upload_part" in src
    assert "complete_multipart_upload" in src
    assert "abort_multipart_upload" in src
    # And the catch is `BaseException` (covers CancelledError).
    assert "except BaseException" in src
