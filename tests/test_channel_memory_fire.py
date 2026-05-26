"""Channel fires memory.add post-turn (fire-and-forget, outside the lock)."""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.common.payloads import MemAdd
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

    async def send_message(self, *, chat_id, text) -> None:
        self.sent.append((chat_id, text))


class _Dispatcher:
    def __init__(self) -> None:
        self.spawns: list[tuple[str, object, str | None]] = []

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        self.spawns.append((dest, payload, mode))
        return f"tsk:{dest}"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="the reply"),
        )


def test_channel_fires_memory_add(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
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
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram",
                )

            disp = _Dispatcher()
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=_FakeTelegram(),
                fire_memory=True,
            )
            await gw.handle_update("tg1", "remember I like cats")
            # Drain the detached memory.add task.
            await asyncio.gather(*gw._memory_tasks)

            mem = [s for s in disp.spawns if s[0] == "memory"]
            assert len(mem) == 1
            dest, payload, mode = mem[0]
            assert mode == "add"
            assert isinstance(payload, MemAdd)
            assert payload.user_prompt == "remember I like cats"
            assert payload.assistant_response == "the reply"
        finally:
            await pool.close()

    asyncio.run(_drive())
