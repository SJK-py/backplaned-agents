"""Channel file I/O — inbound save + outbound relay (fakes + live suite DB).

Fakes the Telegram client (download_file / send_document) and the
credentials file store; uses a real bp_suite for the history rows.
"""

from __future__ import annotations

import asyncio

import pytest

from bp_agents.agents.chatbot.gateway import ChatbotGateway, _detect_mime
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


class _FakeTelegram:
    def __init__(self, *, downloads: dict[str, bytes] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.docs: list[tuple[str, str, bytes]] = []
        self._downloads = downloads or {}

    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id, text) -> None:
        self.sent.append((chat_id, text))

    async def download_file(self, file_id: str) -> bytes:
        return self._downloads[file_id]

    async def send_document(self, *, chat_id, filename, data) -> None:
        self.docs.append((chat_id, filename, data))


class _FakeCreds:
    def __init__(self, *, resolves: dict[str, str] | None = None,
                 blobs: dict[str, bytes] | None = None) -> None:
        self.stored: list[tuple[str, str, bytes]] = []
        self.mime_types: list[str | None] = []
        self._resolves = resolves or {}
        self._blobs = blobs or {}

    async def store_named_file(self, *, user_id, session_id, filename, data, mime_type=None) -> str:
        self.stored.append((session_id, filename, data))
        self.mime_types.append(mime_type)
        return filename

    async def resolve_named_file(self, *, user_id, session_id, name) -> str | None:
        return self._resolves.get(name)

    async def fetch_file(self, *, user_id, file_id) -> bytes:
        return self._blobs[file_id]


class _Dispatcher:
    def __init__(self, *, files: list[str] | None = None) -> None:
        self._files = files or []
        self.prompts: list[str] = []

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        self.prompts.append(payload.prompt)
        return "t"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id="t", status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="here you go", files=self._files),
        )


async def _seed(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="telegram", chat_id="tg1", user_id="usr_a"
        )
        await queries.create_user_config(
            conn, user_id="usr_a", default_session_id="ses_1"
        )
        await queries.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="chatbot_telegram",
            chat_id="tg1",
        )


@pytest.mark.parametrize(
    ("data", "filename", "expected"),
    [
        # Magic bytes win, regardless of (or despite) the filename.
        (b"\xff\xd8\xff\xe0junk", "AgAC123.jpg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "AgAC123.jpg", "image/png"),
        (b"GIF89a....", "x", "image/gif"),
        (b"%PDF-1.7\n%...", "x", "application/pdf"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "x", "image/webp"),
        # No magic → fall back to the filename extension.
        (b"not a known signature", "photo.png", "image/png"),
        (b"not a known signature", "doc.pdf", "application/pdf"),
        # Neither magic nor a known extension → octet-stream.
        (b"\x00\x01\x02\x03", "mystery", "application/octet-stream"),
    ],
)
def test_detect_mime(data: bytes, filename: str, expected: str) -> None:
    assert _detect_mime(data, filename) == expected


def test_inbound_file_saved_and_recorded(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram(downloads={"fid1": b"PDF BYTES"})
            creds = _FakeCreds()
            gw = ChatbotGateway(
                dispatcher=_Dispatcher(), pool=pool, telegram=tg, credentials=creds
            )
            await gw.handle_update("tg1", "look at this", [("fid1", "report.pdf")])

            # Stored to the session stash.
            assert creds.stored == [("ses_1", "report.pdf", b"PDF BYTES")]
            # MIME resolved from the .pdf extension (bytes don't carry the
            # %PDF magic here) and forwarded to the store.
            assert creds.mime_types == ["application/pdf"]
            # History has the (T,T) file row + the user text turn.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
            msgs = [r.message for r in rows]
            assert "user-attached file saved as report.pdf" in msgs
            assert "look at this" in msgs
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_outbound_file_relayed(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            creds = _FakeCreds(resolves={"chart.png": "file_9"}, blobs={"file_9": b"PNG"})
            disp = _Dispatcher(files=["chart.png"])
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=tg, credentials=creds
            )
            await gw.handle_update("tg1", "make me a chart")
            # Text reply + the produced file sent as a document.
            assert tg.sent == [("tg1", "here you go")]
            assert tg.docs == [("tg1", "chart.png", b"PNG")]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_file_only_message_dispatches(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram(downloads={"fid1": b"DATA"})
            creds = _FakeCreds()
            disp = _Dispatcher()
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=tg, credentials=creds
            )
            await gw.handle_update("tg1", "", [("fid1", "photo.jpg")])
            # Dispatched with a synthetic prompt; the file row is in history.
            assert disp.prompts and "file" in disp.prompts[0].lower()
            assert creds.stored[0][1] == "photo.jpg"
        finally:
            await pool.close()

    asyncio.run(_drive())
