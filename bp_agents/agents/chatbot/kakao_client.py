"""chatbot.kakao_client — KakaoTalk transport (outbound-only).

The agent never listens for KakaoTalk. A Cloudflare Worker relay answers
Kakao's 5s webhook and enqueues each turn on a Cloudflare Queue
([../../../docs/design/kakao-channel.md] §3–4). This client is the
agent's outbound half:

  * `pull` / `ack` — drain the queue over the CF Queues HTTP pull API,
    the moral equivalent of `HttpTelegramClient.get_updates`.
  * `post_callback` — deliver an answer (or a status) back to Kakao on the
    turn's single-use `callbackUrl`, the equivalent of `send_message`.

`KakaoClient` is a Protocol so the consumer/gateway can be driven with a
fake in tests; `HttpKakaoClient` is the real httpx implementation.

Two separate httpx clients on purpose: the CF API bearer token authorizes
`pull`/`ack` only — it must NEVER ride along on the callback POST to a
kakao.com URL, so callbacks use a second, header-free client.

NOTE: the exact CF Queues pull/ack and the Kakao callback shapes are
flagged for verification against current docs ([kakao-channel.md] §16).
The field names are encoded in one place so a correction is a local edit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)

_CF_API_BASE = "https://api.cloudflare.com/client/v4"

# A Kakao callback template renders at most a few simpleText bubbles; cap
# how many an over-long reply is split into (the tail is truncated).
_MAX_CALLBACK_BUBBLES = 3
_TRUNCATED_SUFFIX = "…(생략됨)"


def chunk_for_kakao(text: str, *, limit: int, max_bubbles: int) -> list[str]:
    """Split `text` into ≤`limit`-char bubbles, at most `max_bubbles`,
    preferring a newline then a space boundary in the back half of the
    window. An overflow past the last bubble is truncated with a marker."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit and len(chunks) < max_bubbles - 1:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            sp = window.rfind(" ")
            cut = sp if sp >= limit // 2 else limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
        if rest[:1] in ("\n", " "):
            rest = rest[1:]
    if len(rest) > limit:
        rest = rest[: limit - len(_TRUNCATED_SUFFIX)] + _TRUNCATED_SUFFIX
    chunks.append(rest)
    return chunks


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

    async def post_callback(
        self,
        callback_url: str,
        text: str,
        *,
        quick_replies: list[tuple[str, str]] | None = None,
    ) -> None: ...

    async def aclose(self) -> None: ...


class HttpKakaoClient:
    """Real CF Queues pull-consumer + Kakao callback client (outbound-only)."""

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
        self._char_limit = settings.kakao_msg_char_limit
        # CF-authed client for pull/ack only.
        self._queue_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": (
                    f"Bearer {settings.kakao_cf_api_token.get_secret_value()}"
                ),
            },
        )
        # Header-free client for the kakao.com callback — must not carry the
        # CF token. Kept separate so the bearer header can never leak.
        self._callback_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def pull(
        self, *, batch_size: int, visibility_timeout_s: int
    ) -> list[KakaoJob]:
        resp = await self._queue_client.post(
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
        resp = await self._queue_client.post(
            f"{self._messages_base}/ack",
            json={"acks": [{"lease_id": lid} for lid in lease_ids]},
        )
        resp.raise_for_status()

    async def post_callback(
        self,
        callback_url: str,
        text: str,
        *,
        quick_replies: list[tuple[str, str]] | None = None,
    ) -> None:
        """Deliver `text` on a Kakao callback url as simpleText bubble(s),
        optionally with quick-reply buttons (label, messageText)."""
        outputs = [
            {"simpleText": {"text": chunk}}
            for chunk in chunk_for_kakao(
                text, limit=self._char_limit, max_bubbles=_MAX_CALLBACK_BUBBLES
            )
        ]
        template: dict[str, Any] = {"outputs": outputs}
        if quick_replies:
            template["quickReplies"] = [
                {"label": label, "action": "message", "messageText": msg}
                for label, msg in quick_replies
            ]
        resp = await self._callback_client.post(
            callback_url, json={"version": "2.0", "template": template}
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._queue_client.aclose()
        await self._callback_client.aclose()
