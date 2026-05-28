"""chatbot.telegram — Telegram Bot API client + offset persistence.

`TelegramClient` is a Protocol so the gateway can be driven with a fake
in tests; `HttpTelegramClient` is the real long-poll implementation.
The poll offset is persisted to a file in the agent state dir so a
restart doesn't reprocess the backlog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
    # (telegram_file_id, filename) for an attached document/photo, if any.
    attachments: list[tuple[str, str]] = field(default_factory=list)


class TelegramClient(Protocol):
    async def get_updates(
        self, *, offset: int | None, timeout_s: int
    ) -> list[Update]: ...

    async def send_message(self, *, chat_id: str, text: str) -> None: ...

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None: ...

    async def download_file(self, file_id: str) -> bytes: ...

    async def send_document(
        self, *, chat_id: str, filename: str, data: bytes
    ) -> None: ...

    async def set_my_commands(
        self, commands: list[tuple[str, str]]
    ) -> None: ...


class HttpTelegramClient:
    """Real Telegram Bot API client over httpx long-polling."""

    def __init__(
        self, token: str, *, base_url: str = "https://api.telegram.org"
    ) -> None:
        base = base_url.rstrip("/")
        self._base = f"{base}/bot{token}"
        self._file_base = f"{base}/file/bot{token}"
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

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None:
        # Shows "typing…" in the chat; the status auto-clears after ~5s, so
        # the gateway refreshes it while a turn is in flight.
        resp = await self._client.post(
            f"{self._base}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
        )
        resp.raise_for_status()

    async def download_file(self, file_id: str) -> bytes:
        meta = await self._client.get(f"{self._base}/getFile", params={"file_id": file_id})
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]
        blob = await self._client.get(f"{self._file_base}/{file_path}")
        blob.raise_for_status()
        return blob.content

    async def send_document(
        self, *, chat_id: str, filename: str, data: bytes
    ) -> None:
        resp = await self._client.post(
            f"{self._base}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": (filename, data)},
        )
        resp.raise_for_status()

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        # Advertise the command list so it shows in Telegram's "/" menu.
        # `command` must be lowercase, no leading slash (Bot API rule).
        resp = await self._client.post(
            f"{self._base}/setMyCommands",
            json={"commands": [
                {"command": name.lstrip("/").lower(), "description": desc}
                for name, desc in commands
            ]},
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_update(raw: dict[str, Any]) -> Update | None:
    """Extract a text/file message update; skip the rest (edited
    messages, callbacks, channel posts)."""
    update_id = raw.get("update_id")
    message = raw.get("message")
    if update_id is None or not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    text = message.get("text") or message.get("caption") or ""
    attachments: list[tuple[str, str]] = []
    doc = message.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        attachments.append((doc["file_id"], doc.get("file_name") or doc["file_id"]))
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        # Largest size is last.
        biggest = photos[-1]
        if biggest.get("file_id"):
            attachments.append((biggest["file_id"], f"{biggest['file_id']}.jpg"))
    if not text and not attachments:
        return None
    return Update(
        update_id=int(update_id), chat_id=str(chat_id), text=str(text),
        attachments=attachments,
    )


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
