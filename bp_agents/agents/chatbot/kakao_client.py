"""chatbot.kakao_client — KakaoTalk transport (outbound-only).

The agent never listens for KakaoTalk. A Cloudflare Worker relay answers
Kakao's 5s webhook and enqueues each turn on a Cloudflare Queue
([../../../docs/design/kakao-channel.md] §3–4). This client is the
agent's outbound half: it PULLS jobs from the queue and ACKs them over
the CF Queues HTTP pull API — the moral equivalent of
`HttpTelegramClient.get_updates`, but with no server and no inbound port.

`KakaoClient` is a Protocol so the consumer/gateway can be driven with a
fake in tests; `HttpKakaoClient` is the real httpx implementation.

NOTE: the exact CF Queues pull/ack request + response shape is flagged
for verification against the current API ([kakao-channel.md] §16). The
field names below (`batch_size`, `visibility_timeout_ms`,
`result.messages[].{id,lease_id,body}`, ack `{"acks":[{"lease_id":…}]}`)
encode that understanding in one place so a correction is a local edit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)

_CF_API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass
class KakaoJob:
    """One pulled KakaoTalk turn.

    `lease_id` acks the message; `msg_id` is the stable id used to dedupe
    at-least-once redelivery ([kakao-channel.md] §13). `body` is the relay
    payload: `chat_id`, `utterance`, `image_url`, `callback_url`,
    `received_at`.
    """

    msg_id: str
    lease_id: str
    body: dict[str, Any]


class KakaoClient(Protocol):
    async def pull(
        self, *, batch_size: int, visibility_timeout_s: int
    ) -> list[KakaoJob]: ...

    async def ack(self, lease_ids: list[str]) -> None: ...

    async def aclose(self) -> None: ...


class HttpKakaoClient:
    """Real CF Queues pull-consumer client over httpx (outbound-only)."""

    def __init__(self, settings: SuiteSettings) -> None:
        # The caller gates on `_kakao_configured` (all three present), so
        # these are non-None here; assert to satisfy the type narrowing.
        assert settings.kakao_cf_account_id is not None
        assert settings.kakao_cf_queue_id is not None
        assert settings.kakao_cf_api_token is not None
        acct = settings.kakao_cf_account_id
        queue = settings.kakao_cf_queue_id
        self._messages_base = (
            f"{_CF_API_BASE}/accounts/{acct}/queues/{queue}/messages"
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": (
                    f"Bearer {settings.kakao_cf_api_token.get_secret_value()}"
                ),
            },
        )

    async def pull(
        self, *, batch_size: int, visibility_timeout_s: int
    ) -> list[KakaoJob]:
        resp = await self._client.post(
            f"{self._messages_base}/pull",
            json={
                "batch_size": batch_size,
                "visibility_timeout_ms": visibility_timeout_s * 1000,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", False):
            logger.warning(
                "kakao_pull_not_ok", extra={"event": "kakao_pull_not_ok"}
            )
            return []
        messages = (body.get("result") or {}).get("messages") or []
        return [
            KakaoJob(msg_id=m["id"], lease_id=m["lease_id"], body=m["body"])
            for m in messages
        ]

    async def ack(self, lease_ids: list[str]) -> None:
        if not lease_ids:
            return
        resp = await self._client.post(
            f"{self._messages_base}/ack",
            json={"acks": [{"lease_id": lid} for lid in lease_ids]},
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
