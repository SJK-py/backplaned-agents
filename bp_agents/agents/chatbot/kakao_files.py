"""chatbot.kakao_files — outbound image hosting for KakaoTalk.

To render an image in KakaoTalk you must hand Kakao a PUBLIC `imageUrl`
its servers fetch directly. The router's blob store is internal-only and
streams downloads through the router under an auth token, so it can't
serve that ([../../../docs/design/kakao-channel.md] §8). This module
uploads an outbound image to a dedicated S3-compatible bucket (R2) and
returns a short-TTL presigned GET — a public surface decoupled from the
router. Inbound images, by contrast, reuse the router named store.

Mirrors `bp_router/storage/s3.py`: lazy `aioboto3.Session`, a botocore
`Config` that pins pre-1.36 checksum behaviour (required for R2/MinIO),
and — the gotcha — `generate_presigned_url` is a coroutine and must be
awaited.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)

# Leading magic-byte signatures → image MIME. Kakao inbound/outbound media
# is images; a non-image type falls through to the extension/octet-stream.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def detect_image_mime(data: bytes, filename: str = "") -> str:
    """Best-effort image MIME: sniff magic bytes, then the filename
    extension, then octet-stream."""
    for sig, mime in _IMAGE_MAGIC:
        if data.startswith(sig):
            return mime
    # RIFF....WEBP — the "WEBP" tag sits at offset 8, after the size field.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def egress_key(session_id: str, filename: str) -> str:
    """A unique, unguessable object key for an outbound image (the
    presigned URL is the only handle; the key itself is not a capability)."""
    base = filename.rsplit("/", 1)[-1] or "image"
    return f"kakao/{session_id}/{uuid.uuid4().hex}/{base}"


class R2FileEgress:
    """Uploads an image to an S3-compatible bucket and mints a presigned
    GET url for Kakao to fetch. One instance per process; the aioboto3
    session is built lazily so the suite boots without the extra unless
    KakaoTalk image egress is actually configured."""

    def __init__(self, settings: SuiteSettings) -> None:
        assert settings.kakao_r2_bucket is not None
        assert settings.kakao_r2_endpoint_url is not None
        assert settings.kakao_r2_access_key_id is not None
        assert settings.kakao_r2_secret_access_key is not None
        self._bucket = settings.kakao_r2_bucket
        self._endpoint = settings.kakao_r2_endpoint_url
        self._access_key = settings.kakao_r2_access_key_id
        self._secret_key = settings.kakao_r2_secret_access_key.get_secret_value()
        self._ttl = settings.kakao_r2_url_ttl_s
        self._session: Any = None

    @staticmethod
    def configured(settings: SuiteSettings) -> bool:
        """True when all R2 credentials are present (the egress gate)."""
        return all(
            (
                settings.kakao_r2_endpoint_url,
                settings.kakao_r2_bucket,
                settings.kakao_r2_access_key_id,
                settings.kakao_r2_secret_access_key,
            )
        )

    def _session_factory(self) -> Any:
        if self._session is None:
            import aioboto3  # noqa: PLC0415

            self._session = aioboto3.Session(
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                # SigV4 needs a region or botocore raises NoRegionError. R2
                # ignores it; "auto" is its convention (override via the
                # AWS_REGION env for a region-sensitive S3 backend).
                region_name="auto",
            )
        return self._session

    @staticmethod
    def _botocore_config() -> Any:
        # Pin pre-1.36 checksum behaviour — botocore>=1.36 adds x-amz-checksum
        # trailers many S3-compatible servers (R2/MinIO) reject. See
        # bp_router/storage/s3.py for the full rationale.
        from botocore.config import Config  # noqa: PLC0415

        return Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

    def _client(self) -> Any:
        return self._session_factory().client(
            "s3", endpoint_url=self._endpoint, config=self._botocore_config()
        )

    async def put_image(self, data: bytes, *, content_type: str, key: str) -> str:
        """Upload `data` and return a short-TTL presigned GET url."""
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
            # NOTE: a coroutine in aioboto3 — must be awaited.
            return await s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._ttl,
            )
