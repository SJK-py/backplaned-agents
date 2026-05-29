"""Session auto-naming — history_summarizer `session_name` mode + the
channel fire that titles a conversation from its first message.

`_clean_title` is a pure unit; the fire path is exercised end-to-end through
the chatbot gateway with a fake dispatcher against a live suite DB.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.agents.history_summarizer import NameSession
from bp_agents.agents.history_summarizer.agent import _clean_title
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus

# ---------------------------------------------------------------------------
# _clean_title — single line, de-quoted, length-capped
# ---------------------------------------------------------------------------


def test_clean_title_strips_quotes_and_takes_first_line() -> None:
    assert _clean_title('"Trip to Japan"') == "Trip to Japan"
    assert _clean_title("Trip to Japan\nignored second line") == "Trip to Japan"
    assert _clean_title("  Plan a birthday party  ") == "Plan a birthday party"
    assert _clean_title("“Smart quotes”") == "Smart quotes"


def test_clean_title_caps_length_and_handles_empty() -> None:
    long = "word " * 40
    assert len(_clean_title(long)) <= 60
    assert _clean_title("") == ""
    assert _clean_title("   ") == ""


# ---------------------------------------------------------------------------
# Channel fire — titles the session from the first message, once
# ---------------------------------------------------------------------------


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id, text) -> None:
        self.sent.append((chat_id, text))


class _Dispatcher:
    """Returns a title for the history_summarizer task, a reply otherwise."""

    def __init__(self, *, title: str = "Cat preferences") -> None:
        self._title = title
        self.spawns: list[tuple[str, object, str | None]] = []

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        self.spawns.append((dest, payload, mode))
        return f"tsk:{dest}:{mode}"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        content = self._title if "history_summarizer" in task_id else "the reply"
        return ResultFrame(
            agent_id="x", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=content),
        )


async def _seed(pool, *, session_name: str | None = None) -> None:
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
        )
        if session_name is not None:
            await queries.update_session_info(
                conn, "ses_1", session_name=session_name
            )


def test_first_message_titles_the_session(suite_db_url: str) -> None:
    async def _drive() -> tuple[str | None, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # no name yet
            disp = _Dispatcher(title="Cat preferences")
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=_FakeTelegram(),
            )
            await gw.handle_update("tg1", "i love cats, tell me about them")
            await asyncio.gather(*gw._core._name_tasks)  # drain the detached task

            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
            name_spawns = [s for s in disp.spawns if s[2] == "session_name"]
            return info.session_name, name_spawns
        finally:
            await pool.close()

    name, name_spawns = asyncio.run(_drive())
    assert name == "Cat preferences"
    # Dispatched to the summarizer with the user's first message.
    assert len(name_spawns) == 1
    dest, payload, _ = name_spawns[0]
    assert dest == "history_summarizer"
    assert isinstance(payload, NameSession)
    assert payload.user_prompt == "i love cats, tell me about them"


def test_already_named_session_is_not_retitled(suite_db_url: str) -> None:
    async def _drive() -> tuple[str | None, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, session_name="My existing title")
            disp = _Dispatcher(title="Should not be used")
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=_FakeTelegram(),
            )
            await gw.handle_update("tg1", "another message")
            await asyncio.gather(*gw._core._name_tasks)

            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
            name_spawns = [s for s in disp.spawns if s[2] == "session_name"]
            return info.session_name, name_spawns
        finally:
            await pool.close()

    name, name_spawns = asyncio.run(_drive())
    assert name == "My existing title"  # unchanged
    assert name_spawns == []  # never dispatched a naming task
