"""history_summarizer core + the channel's summarization trigger.

Summarizer core: stubbed `ctx.llm` + live suite DB. Channel trigger:
`ChatbotGateway._maybe_summarize` with a fake dispatcher (returns a
summary) + live suite DB — asserts the cutoff, the applied summary, and
the incumbent demotion.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.agents.history_summarizer import (
    HISTORY_SUMMARIZER_AGENT_ID,
    SummarizeIncumbent,
    run_summarize_incumbent,
)
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus
from bp_sdk import LlmResponse, Message  # noqa: E402  (after first-party per ruff)


class _StubLlm:
    def __init__(self, text: str) -> None:
        self.text = text
        self.captured: list[Message] | None = None

    async def generate(self, messages, **kw) -> LlmResponse:
        self.captured = list(messages)
        return LlmResponse(text=self.text, tool_calls=[])


class _StubCtx:
    def __init__(self, user_id: str, session_id: str, llm) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.llm = llm


class _FakeTelegram:
    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id, text) -> None:
        return None


class _SummDispatcher:
    def __init__(self, summary: str = "ROLLED-UP SUMMARY") -> None:
        self.summary = summary
        self.spawns: list[tuple] = []

    async def spawn_root_for_user(
        self, dest, payload, *, user_id, session_id, mode=None, **kw
    ) -> str:
        self.spawns.append((dest, payload, mode))
        return "tsk_s"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id=HISTORY_SUMMARIZER_AGENT_ID, trace_id="0" * 32,
            span_id="0" * 16, task_id=task_id, status=TaskStatus.SUCCEEDED,
            status_code=200, output=AgentOutput(content=self.summary),
        )


async def _truncate(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )


async def _seed_thread(pool, *, n: int, agent_id="orchestrator") -> list[int]:
    ids: list[int] = []
    async with pool.acquire() as conn:
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            ids.append(await queries.append_history(
                conn, session_id="ses_1", agent_id=agent_id,
                role=role, message=f"turn {i}",
            ))
    return ids


def test_summarizer_core_folds_previous_and_transcript(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            ids = await _seed_thread(pool, n=4)
            llm = _StubLlm("NEW SUMMARY")
            ctx = _StubCtx("usr_a", "ses_1", llm)
            out = await run_summarize_incumbent(
                ctx,  # type: ignore[arg-type]
                SummarizeIncumbent(
                    agent_id="orchestrator", up_to=ids[-1],
                    previous_summary="earlier stuff",
                ),
                pool=pool, settings=SuiteSettings(database_url=suite_db_url),
            )
            assert out.content == "NEW SUMMARY"
            user_msg = llm.captured[1]
            assert "earlier stuff" in user_msg.content
            assert "turn 0" in user_msg.content and "turn 3" in user_msg.content
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_channel_summarizes_over_limit(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram",
                )
                await queries.create_user_config(
                    conn, user_id="usr_a", max_context_token_limit=100,
                )
            ids = await _seed_thread(pool, n=10)

            disp = _SummDispatcher(summary="ROLLED-UP")
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=_FakeTelegram()
            )
            # context_tokens (500) > limit (100), 10 rows ≥ min.
            await gw._core.maybe_summarize("ses_1", "orchestrator", 500)

            # Summarizer spawned with the 70% cutoff.
            assert len(disp.spawns) == 1
            dest, payload, mode = disp.spawns[0]
            assert dest == HISTORY_SUMMARIZER_AGENT_ID
            assert mode == "summarize_incumbent"
            cutoff_idx = int(10 * 0.7)  # 7
            assert payload.up_to == ids[cutoff_idx - 1]

            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
                remaining = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
            assert info.history_summary == "ROLLED-UP"
            # The folded oldest 7 were demoted; 3 remain incumbent.
            assert len(remaining) == 3
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_channel_skips_summary_under_limit_or_too_few(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram",
                )
                await queries.create_user_config(
                    conn, user_id="usr_a", max_context_token_limit=100,
                )
            await _seed_thread(pool, n=10)
            disp = _SummDispatcher()
            gw = ChatbotGateway(
                dispatcher=disp, pool=pool, telegram=_FakeTelegram()
            )
            # Under the limit → no summarize.
            await gw._core.maybe_summarize("ses_1", "orchestrator", 50)
            assert disp.spawns == []
            # None context_tokens → no summarize.
            await gw._core.maybe_summarize("ses_1", "orchestrator", None)
            assert disp.spawns == []
        finally:
            await pool.close()

    asyncio.run(_drive())
