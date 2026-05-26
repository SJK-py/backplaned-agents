"""Verbose mode — the channel renders one message per LoopProgress frame."""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway, _render_progress
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ProgressFrame, ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id, text) -> None:
        self.sent.append(text)


class _ProgressDispatcher:
    """await_root_result drives on_progress with two LoopProgress frames."""

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        return "t"

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):
        if on_progress is not None:
            for lp in (
                {"kind": "thinking", "round": 1},
                {"kind": "tool_call", "tool": "call_memory", "round": 1},
            ):
                await on_progress(ProgressFrame(
                    agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
                    task_id="t", event=lp["kind"], metadata={LOOP_PROGRESS_KEY: lp},
                ))
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id="t", status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="final answer"),
        )


async def _seed(pool, *, verbose_default: bool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="telegram", chat_id="tg1", user_id="usr_a"
        )
        await queries.create_user_config(
            conn, user_id="usr_a", default_session_id="ses_1",
            verbose_default=verbose_default,
        )
        await queries.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="chatbot_telegram",
            chat_id="tg1",
        )


def _gw(pool):
    return ChatbotGateway(
        dispatcher=_ProgressDispatcher(), pool=pool, telegram=_FakeTelegram()
    )


def test_one_shot_verbose_renders_progress(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, verbose_default=False)
            gw = _gw(pool)
            await gw.handle_update("tg1", "/v do the thing")
            sent = gw._telegram.sent
            assert any("Thinking" in s for s in sent)
            # The `call_` peer-tool prefix is stripped and the [Tool] label
            # leads the tool_call line.
            assert any("memory" in s and "[Tool]" in s for s in sent)
            assert sent[-1] == "final answer"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_non_verbose_suppresses_progress(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, verbose_default=False)
            gw = _gw(pool)
            await gw.handle_update("tg1", "do the thing")
            assert gw._telegram.sent == ["final answer"]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_verbose_default_renders_progress(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool, verbose_default=True)
            gw = _gw(pool)
            await gw.handle_update("tg1", "do the thing")
            assert len(gw._telegram.sent) == 3  # 2 progress + reply
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_render_progress_formats() -> None:
    # thinking: heartbeat vs reasoning detail (parenthesized, ellipsis lead)
    assert _render_progress({"kind": "thinking"}) == "Thinking…"
    assert _render_progress(
        {"kind": "thinking", "detail": "so I check the docs"}
    ) == "(…so I check the docs)"
    # an already-truncated detail (leading …) isn't double-ellipsised
    assert _render_progress(
        {"kind": "thinking", "detail": "…the tail of a long thought"}
    ) == "(…the tail of a long thought)"
    # tool_call / tool_result: [Tool]/[Result] labels, call_ stripped, parens
    assert _render_progress(
        {"kind": "tool_call", "tool": "call_knowledge_base", "detail": "looking it up"}
    ) == "[Tool] knowledge_base (looking it up)"
    assert _render_progress(
        {"kind": "tool_result", "tool": "call_knowledge_base"}
    ) == "[Result] knowledge_base"
    # a non-peer local tool keeps its name as-is
    assert _render_progress(
        {"kind": "tool_call", "tool": "current_time"}
    ) == "[Tool] current_time"
