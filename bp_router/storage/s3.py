"""bp_router.storage.s3 — S3-compatible FileStore.

Works against AWS S3, Cloudflare R2, MinIO, etc. via aioboto3.

Required `file_store_options`:
    bucket:           str
Optional:
    region_name:      str
    endpoint_url:     str   (R2/MinIO)
    access_key_id:    str
    secret_access_key:str
    prefix:           str   (key prefix; default "")
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from bp_router.storage.base import FileMeta, FileStore

logger = logging.getLogger(__name__)


class S3FileStore(FileStore):
    backend_name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        prefix: str = "",
    ) -> None:
        self.bucket = bucket
        self.region_name = region_name
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.prefix = prefix.rstrip("/")
        self._session: Any | None = None

    @classmethod
    def from_options(cls, options: dict) -> S3FileStore:
        return cls(
            bucket=options["bucket"],
            region_name=options.get("region_name"),
            endpoint_url=options.get("endpoint_url"),
            access_key_id=options.get("access_key_id"),
            secret_access_key=options.get("secret_access_key"),
            prefix=options.get("prefix", ""),
        )

    # ------------------------------------------------------------------

    def _key(self, sha256: str) -> str:
        # Defence-in-depth path validation: a malformed sha256 from
        # the DB or a future caller can't smuggle path segments that
        # would alter the S3 prefix layout. Mirrors
        # `LocalFileStore._path`.
        from bp_router.storage.local import _validate_sha256  # noqa: PLC0415
        _validate_sha256(sha256)
        sub = f"{sha256[:2]}/{sha256[2:4]}/{sha256}"
        return f"{self.prefix}/{sub}" if self.prefix else sub

    def _session_factory(self):  # type: ignore[no-untyped-def]
        if self._session is None:
            try:
                import aioboto3  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "aioboto3 not installed; "
                    "`pip install backplaned[storage-s3]`"
                ) from exc
            self._session = aioboto3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region_name,
            )
        return self._session

    def _client(self):  # type: ignore[no-untyped-def]
        session = self._session_factory()
        return session.client(
            "s3", endpoint_url=self.endpoint_url, config=self._botocore_config()
        )

    @staticmethod
    def _botocore_config():  # type: ignore[no-untyped-def]
        # botocore 1.36 (pulled in by aioboto3>=14) flipped S3 request
        # checksums on by default (request_checksum_calculation="when_supported"):
        # it now adds x-amz-checksum-* trailers via `aws-chunked`
        # content-encoding on PUT/UploadPart. Many S3-COMPATIBLE servers
        # (rustfs beta, older MinIO, some R2 paths) don't implement that
        # trailer protocol, so the body is misframed and the part is rejected —
        # create_multipart_upload succeeds but the very next upload_part (and
        # even abort) fail with NoSuchUpload. Pin both knobs back to AWS's
        # pre-1.36 behaviour (only checksum when the operation requires it).
        # Against real AWS this is a no-op for correctness. See boto3#4392.
        from botocore.config import Config  # noqa: PLC0415
        return Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    # S3 multipart parameters. Pre-R6 the put() implementation
    # accumulated every chunk into an in-memory list and then
    # b"".join'd it, so an upload of `max_upload_bytes` peaked at
    # ~2× that size in RAM. With `max_upload_bytes` raised for
    # video / model-weight workloads (per the docstring), N
    # concurrent uploaders × 2 × cap = real DOS axis. R6
    # third-pass review flagged this as HIGH.
    #
    # Multipart streaming buffers ONE part at a time. The minimum
    # S3 part size is 5 MiB (5 * 1024 * 1024) for all parts
    # except the last. We use 8 MiB for a clean power-of-two and
    # to amortise the per-part HTTP round-trip on large uploads.
    _MULTIPART_PART_SIZE = 8 * 1024 * 1024
    # S3 supports up to 10,000 parts. With 8 MiB parts that's an
    # 80 GiB ceiling — way above any reasonable single-file cap.
    _MULTIPART_MAX_PARTS = 10_000

    async def put(
        self,
        sha256: str,
        src: AsyncIterable[bytes],
        meta: FileMeta,
    ) -> str:
        """Upload to s3://bucket/key with sha256 verification.

        Streams via S3 multipart upload — one part at a time held
        in memory (`_MULTIPART_PART_SIZE`, 8 MiB), regardless of
        the total upload size. The previous shape buffered the
        whole upload into a list and then joined it, peaking at
        2× the total in RAM (R6 third-pass DOS fix).

        Failure path: if anything raises mid-upload (sha mismatch,
        upstream error, asyncio cancellation), we issue
        `abort_multipart_upload` so the in-progress session
        doesn't leak as a billable S3 orphan. The abort is best-
        effort — wrapped in its own try/except so the original
        exception still surfaces to the caller.
        """
        h = hashlib.sha256()
        size = 0
        key = self._key(sha256)

        async with self._client() as s3:
            create_kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": key,
            }
            if meta.mime_type:
                create_kwargs["ContentType"] = meta.mime_type
            create = await s3.create_multipart_upload(**create_kwargs)
            upload_id = create["UploadId"]

            parts: list[dict[str, Any]] = []
            part_number = 1
            buffer = bytearray()
            try:
                async for chunk in src:
                    h.update(chunk)
                    size += len(chunk)
                    buffer.extend(chunk)
                    # Flush whole parts. The LAST part can be smaller
                    # than _MULTIPART_PART_SIZE; that flush happens
                    # after the `async for` loop ends.
                    while len(buffer) >= self._MULTIPART_PART_SIZE:
                        if part_number > self._MULTIPART_MAX_PARTS:
                            raise ValueError(
                                "S3 multipart part count would exceed "
                                f"{self._MULTIPART_MAX_PARTS}; "
                                "upload too large for current part size"
                            )
                        body = bytes(buffer[: self._MULTIPART_PART_SIZE])
                        del buffer[: self._MULTIPART_PART_SIZE]
                        resp = await s3.upload_part(
                            Bucket=self.bucket,
                            Key=key,
                            UploadId=upload_id,
                            PartNumber=part_number,
                            Body=body,
                        )
                        parts.append(
                            {"PartNumber": part_number, "ETag": resp["ETag"]}
                        )
                        part_number += 1

                # Flush the final (possibly < part size) chunk.
                # S3 requires AT LEAST ONE part even for an empty
                # upload — but zero-byte uploads are rejected
                # upstream of here. For tiny uploads (< 8 MiB) this
                # is the single part.
                if buffer or part_number == 1:
                    resp = await s3.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=bytes(buffer),
                    )
                    parts.append(
                        {"PartNumber": part_number, "ETag": resp["ETag"]}
                    )

                # Verify size/hash BEFORE completing the upload.
                # Failing here aborts the multipart session in the
                # except branch below — the bytes are never made
                # visible at the final key.
                actual = h.hexdigest()
                if actual != sha256:
                    raise ValueError(
                        f"sha256 mismatch on s3 put: claimed {sha256}, "
                        f"actual {actual}"
                    )
                if meta.byte_size > 0 and size != meta.byte_size:
                    raise ValueError(
                        f"size mismatch on s3 put: claimed "
                        f"{meta.byte_size}, actual {size}"
                    )

                await s3.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            except BaseException:  # noqa: BLE001
                # `BaseException` so we also abort on
                # `asyncio.CancelledError` (client disconnect mid-
                # upload). Without the abort, S3 keeps the
                # multipart session for ~7 days by default and
                # charges for the storage of buffered parts.
                try:
                    await s3.abort_multipart_upload(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "s3_abort_multipart_upload_failed",
                        extra={
                            "event": "s3_abort_multipart_upload_failed",
                            "bp.s3_bucket": self.bucket,
                            "bp.s3_key": key,
                            "bp.upload_id": upload_id,
                        },
                    )
                raise

        return f"s3://{self.bucket}/{key}"

    # ------------------------------------------------------------------
    # open
    # ------------------------------------------------------------------

    async def open(self, sha256: str) -> AsyncIterator[bytes]:
        key = self._key(sha256)

        async def _gen() -> AsyncIterator[bytes]:
            async with self._client() as s3:
                resp = await s3.get_object(Bucket=self.bucket, Key=key)
                async for chunk in resp["Body"].iter_chunks(chunk_size=64 * 1024):
                    yield chunk

        return _gen()

    # ------------------------------------------------------------------
    # presigned_url
    # ------------------------------------------------------------------

    async def presigned_url(
        self,
        sha256: str,
        *,
        ttl_s: int,
        content_disposition: str | None = None,
        content_type: str | None = None,
    ) -> str | None:
        key = self._key(sha256)
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}
        # Pin the SAME download-forcing / MIME policy the streamed
        # path applies. S3 only lets us override these response
        # headers via the signed query params; `X-Content-Type-
        # Options: nosniff` cannot ride this path, so the forced
        # `attachment` disposition + a sanitised content-type is the
        # closable defence for the backend-direct redirect.
        if content_disposition is not None:
            params["ResponseContentDisposition"] = content_disposition
        if content_type is not None:
            params["ResponseContentType"] = content_type
        async with self._client() as s3:
            url = await s3.generate_presigned_url(
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=ttl_s,
            )
        return url

    # ------------------------------------------------------------------
    # delete / exists
    # ------------------------------------------------------------------

    async def delete(self, sha256: str) -> None:
        key = self._key(sha256)
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001
            logger.debug("s3 delete failed (may be missing)", exc_info=True)

    async def exists(self, sha256: str) -> bool:
        key = self._key(sha256)
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False
