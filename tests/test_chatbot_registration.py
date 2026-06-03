"""chatbot registration/approval — reconcile core + slash commands.

Reconcile is tested against a live suite DB; the /register, /new, /stop
commands are driven through ChatbotGateway with a fake credentials client
and a fake Telegram.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bp_agents.agents.chatbot.approval import reconcile_serviced_sessions
from bp_agents.agents.chatbot.credentials import ServicedSession
from bp_agents.agents.chatbot.gateway import (
    _ALREADY_REGISTERED,
    _REGISTER_SUBMITTED,
    REGISTER_PROMPT,
    ChatbotGateway,
)
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
    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        return "tsk_1"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="ok"),
        )


class _FakeCredentials:
    def __init__(self, *, new_session: str = "ses_new") -> None:
        self.registrations: list[tuple] = []
        self.opened: list[tuple] = []
        self.cancels: list[tuple] = []
        self.reset_mints: list[str] = []
        self._new_session = new_session

    async def submit_registration(
        self, *, channel, external_id, requested_email=None, metadata=None
    ) -> str:
        self.registrations.append((channel, external_id, requested_email))
        return "reg_1"

    async def list_serviced_sessions(self, *, channel=None, since=None):
        return []

    async def open_session(self, *, user_id, metadata=None) -> str:
        self.opened.append((user_id, metadata))
        return self._new_session

    async def cancel_task(self, *, user_id, task_id) -> None:
        self.cancels.append((user_id, task_id))

    async def mint_password_reset_token(self, *, user_id) -> str:
        self.reset_mints.append(user_id)
        return "rst_token_xyz"


async def _truncate(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )


def test_reconcile_writes_identity_and_is_idempotent(suite_db_url: str) -> None:
    async def _drive() -> None:
        settings = SuiteSettings(database_url=suite_db_url)
        pool = await open_pool(settings)
        try:
            await _truncate(pool)
            rec = ServicedSession(
                user_id="usr_a", session_id="ses_1", external_id="tg1",
                channel="chatbot_telegram", opened_at=datetime.now(UTC),
            )
            n = await reconcile_serviced_sessions(pool, [rec], settings=settings)
            assert n == 1

            async with pool.acquire() as conn:
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg1"
                ) == "usr_a"
                cfg = await queries.get_user_config(conn, "usr_a")
                assert cfg is not None and cfg.default_session_id == "ses_1"
                # No default_language passed → the en default (Telegram path).
                assert cfg.language == "en"
                assert await queries.get_session_info(conn, "ses_1") is not None

            # Idempotent: a re-poll maps nothing new.
            assert await reconcile_serviced_sessions(
                pool, [rec], settings=settings
            ) == 0

            # A later /new moved the default; reconcile must NOT clobber it.
            async with pool.acquire() as conn:
                await queries.set_default_session_id(
                    conn, user_id="usr_a", session_id="ses_2"
                )
            await reconcile_serviced_sessions(pool, [rec], settings=settings)
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
            assert cfg.default_session_id == "ses_2"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_reconcile_seeds_tier_presets_from_settings(suite_db_url: str) -> None:
    """A first-time user_config row picks up the operator's configured
    per-tier preset defaults (`SUITE_DEFAULT_PRESET_*`)."""
    async def _drive() -> None:
        settings = SuiteSettings(
            database_url=suite_db_url,
            default_preset_pro="claude-opus",
            default_preset_balanced="claude",
            default_preset_lite="gemini-lite",
            default_preset_embedding="text-embedding-3-small",
        )
        pool = await open_pool(settings)
        try:
            await _truncate(pool)
            rec = ServicedSession(
                user_id="usr_a", session_id="ses_1", external_id="tg1",
                channel="chatbot_telegram", opened_at=datetime.now(UTC),
            )
            await reconcile_serviced_sessions(pool, [rec], settings=settings)
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
            assert cfg.preset_pro == "claude-opus"
            assert cfg.preset_balanced == "claude"
            assert cfg.preset_lite == "gemini-lite"
            assert cfg.preset_embedding == "text-embedding-3-small"
        finally:
            await pool.close()

    asyncio.run(_drive())


def _gateway(pool, *, creds=None, tg=None):
    return ChatbotGateway(
        dispatcher=_FakeDispatcher(),
        pool=pool,
        telegram=tg or _FakeTelegram(),
        credentials=creds,
    )


def test_register_submits_for_unmapped_chat(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            tg = _FakeTelegram()
            creds = _FakeCredentials()
            gw = _gateway(pool, creds=creds, tg=tg)

            await gw.handle_update("tg1", "/register me@example.com")
            assert creds.registrations == [
                ("chatbot_telegram", "tg1", "me@example.com")
            ]
            assert tg.sent == [("tg1", _REGISTER_SUBMITTED)]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_register_is_noop_when_already_mapped(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg1", user_id="usr_a"
                )
            tg = _FakeTelegram()
            creds = _FakeCredentials()
            gw = _gateway(pool, creds=creds, tg=tg)

            await gw.handle_update("tg1", "/register")
            assert creds.registrations == []
            assert tg.sent == [("tg1", _ALREADY_REGISTERED)]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_new_opens_session_and_moves_default(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg1", user_id="usr_a"
                )
                await queries.create_user_config(
                    conn, user_id="usr_a", default_session_id="ses_old"
                )
            tg = _FakeTelegram()
            creds = _FakeCredentials(new_session="ses_new")
            gw = _gateway(pool, creds=creds, tg=tg)

            await gw.handle_update("tg1", "/new")
            assert creds.opened == [
                ("usr_a", {"kind": "chatbot_telegram", "external_id": "tg1"})
            ]
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
                assert cfg.default_session_id == "ses_new"
                assert await queries.get_session_info(conn, "ses_new") is not None
            assert tg.sent == [("tg1", "Started a new conversation.")]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_password_mints_token_for_mapped_user(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg1", user_id="usr_a"
                )
            tg = _FakeTelegram()
            creds = _FakeCredentials()
            gw = _gateway(pool, creds=creds, tg=tg)

            await gw.handle_update("tg1", "/password")
            assert creds.reset_mints == ["usr_a"]
            assert len(tg.sent) == 1
            assert "rst_token_xyz" in tg.sent[0][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_password_prompts_unmapped_chat_to_register(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            tg = _FakeTelegram()
            creds = _FakeCredentials()
            gw = _gateway(pool, creds=creds, tg=tg)
            await gw.handle_update("tg1", "/password")
            assert creds.reset_mints == []
            assert tg.sent == [("tg1", REGISTER_PROMPT)]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_stop_with_nothing_running(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            tg = _FakeTelegram()
            gw = _gateway(pool, creds=_FakeCredentials(), tg=tg)
            await gw.handle_update("tg1", "/stop")
            assert tg.sent == [("tg1", "Nothing is running right now.")]
        finally:
            await pool.close()

    asyncio.run(_drive())
