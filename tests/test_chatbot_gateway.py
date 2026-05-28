"""chatbot gateway — inbound message engine against a live suite DB.

Fakes the Telegram client and the SDK root-dispatcher; uses a real
`bp_suite` database. Covers identity resolution, the user-turn write +
dispatch + relay, the unmapped/`/help`/failure paths, and per-session
serialization.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from bp_agents.agents.chatbot.gateway import (
    BOT_COMMANDS,
    HELP_TEXT,
    REGISTER_PROMPT,
    ChatbotGateway,
)
from bp_agents.agents.chatbot.telegram import HttpTelegramClient
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class _FakeDispatcher:
    def __init__(self, *, reply: str = "hi from orch", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.spawns: list[tuple] = []

    async def spawn_root_for_user(
        self, dest, payload, *, user_id, session_id, mode=None, **kw
    ) -> str:
        self.spawns.append((dest, payload.prompt, user_id, session_id, mode))
        if self.fail:
            raise RuntimeError("admit failed")
        return f"tsk:{payload.prompt}"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=self.reply),
        )


async def _seed(pool, *, chat_id="tg1", user_id="usr_a", session_id="ses_1") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="telegram", chat_id=chat_id, user_id=user_id
        )
        await queries.create_user_config(
            conn, user_id=user_id, default_session_id=session_id
        )
        await queries.create_session_info(
            conn, session_id=session_id, user_id=user_id, channel="chatbot_telegram"
        )


def test_gateway_dispatches_and_relays(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher(reply="the answer")
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg1", "what's up?")

            # Injected to the orchestrator on behalf of the user.
            assert disp.spawns == [
                ("orchestrator", "what's up?", "usr_a", "ses_1", "message")
            ]
            # Reply relayed.
            assert tg.sent == [("tg1", "the answer")]
            # User turn written verbatim to the orchestrator thread.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
            assert [(r.role, r.message) for r in rows] == [("user", "what's up?")]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_unmapped_chat_gets_register_prompt(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher()
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg_unknown", "hello")
            assert tg.sent == [("tg_unknown", REGISTER_PROMPT)]
            assert disp.spawns == []
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_help_command(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher()
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg1", "/help")
            assert tg.sent == [("tg1", HELP_TEXT)]
            assert disp.spawns == []
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_dispatch_failure_is_surfaced(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher(fail=True)
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg1", "boom please")
            assert len(tg.sent) == 1
            assert "went wrong" in tg.sent[0][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_serializes_per_session(suite_db_url: str) -> None:
    """Two concurrent turns on one session must not interleave —
    spawn/result for one completes before the next begins."""

    class _OrderingDispatcher:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def spawn_root_for_user(
            self, dest, payload, *, user_id, session_id, mode=None, **kw
        ) -> str:
            self.events.append(f"spawn:{payload.prompt}")
            return f"tsk:{payload.prompt}"

        async def await_root_result(self, task_id, *, timeout_s=None, **kw):
            await asyncio.sleep(0.05)  # hold the session "busy"
            prompt = task_id.split(":", 1)[1]
            self.events.append(f"result:{prompt}")
            return ResultFrame(
                agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
                task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
                output=AgentOutput(content="ok"),
            )

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _OrderingDispatcher()
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=_FakeTelegram())

            await asyncio.gather(
                gw.handle_update("tg1", "a"),
                gw.handle_update("tg1", "b"),
            )
            # No interleave: each spawn is immediately followed by its
            # own result (whichever turn won the lock first).
            assert disp.events[0].startswith("spawn:")
            first = disp.events[0].split(":", 1)[1]
            assert disp.events[1] == f"result:{first}"
            assert disp.events[2].startswith("spawn:")
            second = disp.events[2].split(":", 1)[1]
            assert disp.events[3] == f"result:{second}"
            assert {first, second} == {"a", "b"}
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- command registration (setMyCommands) -------------------------------


def test_help_text_lists_every_command() -> None:
    # HELP_TEXT is derived from BOT_COMMANDS, so each stays in lockstep.
    for name, desc in BOT_COMMANDS:
        assert f"/{name}" in HELP_TEXT
        assert desc in HELP_TEXT


def test_set_my_commands_posts_normalized_payload() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async def _drive() -> None:
        client = HttpTelegramClient("TOKEN", base_url="https://api.telegram.org")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        # Leading slash + mixed case must be normalized away.
        await client.set_my_commands([("/Help", "show help"), ("v", "verbose")])
        await client.aclose()

    asyncio.run(_drive())
    assert captured["url"].endswith("/botTOKEN/setMyCommands")
    assert captured["body"]["commands"] == [
        {"command": "help", "description": "show help"},
        {"command": "v", "description": "verbose"},
    ]
