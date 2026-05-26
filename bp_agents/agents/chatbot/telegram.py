"""chatbot.telegram — Telegram Bot API client + offset persistence.

`TelegramClient` is a Protocol so the gateway can be driven with a fake
in tests; `HttpTelegramClient` is the real long-poll implementation.
The poll offset is persisted to a file in the agent state dir so a
restart doesn't reprocess the backlog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass
class Update:
    """A normalized inbound Telegram update (text messages only — other
    update kinds are skipped at parse time)."""

    update_id: int
    chat_id: str
    text: str


class TelegramClient(Protocol):
    async def get_updates(
        self, *, offset: int | None, timeout_s: int
    ) -> list[Update]: ...

    async def send_message(self, *, chat_id: str, text: str) -> None: ...


class HttpTelegramClient:
    """Real Telegram Bot API client over httpx long-polling."""

    def __init__(
        self, token: str, *, base_url: str = "https://api.telegram.org"
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        # `getUpdates` long-polls up to `timeout_s`; give the HTTP read a
        # margin over that so the poll itself isn't cut short.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    async def get_updates(
        self, *, offset: int | None, timeout_s: int
    ) -> list[Update]:
        params: dict[str, Any] = {"timeout": timeout_s}
        if offset is not None:
            params["offset"] = offset
        resp = await self._client.get(
            f"{self._base}/getUpdates",
            params=params,
            timeout=httpx.Timeout(timeout_s + 15.0),
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            logger.warning(
                "telegram_get_updates_not_ok",
                extra={"event": "telegram_get_updates_not_ok"},
            )
            return []
        return [u for raw in body.get("result", []) if (u := _parse_update(raw))]

    async def send_message(self, *, chat_id: str, text: str) -> None:
        resp = await self._client.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_update(raw: dict[str, Any]) -> Update | None:
    """Extract a text message update; skip everything else (edited
    messages, callbacks, channel posts, non-text messages)."""
    update_id = raw.get("update_id")
    message = raw.get("message")
    if update_id is None or not isinstance(message, dict):
        return None
    text = message.get("text")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if text is None or chat_id is None:
        return None
    return Update(update_id=int(update_id), chat_id=str(chat_id), text=str(text))


class FileOffsetStore:
    """Persist the Telegram long-poll offset to a file so a restart
    resumes past already-processed updates."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> int | None:
        try:
            return int(self._path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def write(self, offset: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(offset))
