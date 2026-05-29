"""Telegram outbound chunking + blank-skip (the sendMessage 400 fix).

Telegram 400s a message over 4096 chars or one that's empty; LLM replies
routinely exceed the cap. `HttpTelegramClient.send_message` splits long text
and skips blank — exercised here via an httpx MockTransport (no network).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from bp_agents.agents.chatbot.telegram import (
    _CHUNK_CHARS,
    TELEGRAM_MAX_CHARS,
    HttpTelegramClient,
    _split_for_telegram,
)

# ---------------------------------------------------------------------------
# _split_for_telegram (pure)
# ---------------------------------------------------------------------------


def test_split_short_text_is_single_chunk() -> None:
    assert _split_for_telegram("hello") == ["hello"]
    assert _split_for_telegram("x" * _CHUNK_CHARS) == ["x" * _CHUNK_CHARS]


def test_split_long_text_chunks_under_limit_preserving_content() -> None:
    text = "paragraph line\n" * 1000  # ~15k chars, newline boundaries
    chunks = _split_for_telegram(text)
    assert len(chunks) > 1
    assert all(len(c) <= _CHUNK_CHARS for c in chunks)
    # Only boundary newlines are dropped — non-newline content is intact.
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_hard_cuts_a_boundaryless_run_without_dropping() -> None:
    text = "z" * (_CHUNK_CHARS * 2 + 100)  # no spaces/newlines to break on
    chunks = _split_for_telegram(text)
    assert len(chunks) == 3
    assert all(len(c) <= _CHUNK_CHARS for c in chunks)
    assert "".join(chunks) == text  # hard cut drops nothing


# ---------------------------------------------------------------------------
# send_message (httpx MockTransport)
# ---------------------------------------------------------------------------


async def _send(text: str, sent: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    c = HttpTelegramClient("tok")
    await c._client.aclose()  # drop the real network client
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await c.send_message(chat_id="1", text=text)
    finally:
        await c._client.aclose()


def test_send_message_chunks_long_reply() -> None:
    sent: list[str] = []
    asyncio.run(_send("y" * (TELEGRAM_MAX_CHARS + 500), sent))
    assert len(sent) >= 2
    assert all(len(s) <= TELEGRAM_MAX_CHARS for s in sent)
    assert "".join(sent) == "y" * (TELEGRAM_MAX_CHARS + 500)


def test_send_message_skips_blank() -> None:
    sent: list[str] = []
    asyncio.run(_send("   \n  ", sent))
    assert sent == []  # blank text → no API call (would otherwise 400)
