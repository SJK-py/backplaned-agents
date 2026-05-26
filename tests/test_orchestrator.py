"""orchestrator `message` turn — core logic against a live suite DB.

Stubs `ctx.llm` (so no router / provider is needed) and uses a real
`bp_suite` database for the history read/write. Assumes the suite schema
is applied; truncates between tests.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.orchestrator import ORCHESTRATOR_AGENT_ID, run_orchestrator_message
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_sdk import LlmResponse, Message


class _StubLlm:
    def __init__(self, text: str) -> None:
        self.text = text
        self.captured: list[Message] | None = None

    async def generate(self, messages, **kw) -> LlmResponse:
        self.captured = list(messages)
        return LlmResponse(text=self.text, tool_calls=[])


class _StubPeers:
    def visible(self, *, for_user_level=None):
        return {}


class _StubProgress:
    async def emit(self, *a, **k) -> None:
        return None


class _StubCtx:
    def __init__(self, user_id: str, session_id: str, llm) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.user_level = "tier0"
        self.llm = llm
        self.peers = _StubPeers()
        self.progress = _StubProgress()


async def _truncate(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )


def test_orchestrator_message_uses_history_and_persists_reply(
    suite_db_url: str,
) -> None:
    async def _drive() -> None:
        settings = SuiteSettings(database_url=suite_db_url)
        pool = await open_pool(settings)
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                await queries.create_user_config(
                    conn, user_id="usr_a", full_name="Ada", timezone="UTC",
                )
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram",
                )
                await queries.update_session_info(
                    conn, "ses_1", history_summary="prior summary text",
                )
                # The channel writes the user turn before dispatch.
                await queries.append_history(
                    conn, session_id="ses_1", agent_id=ORCHESTRATOR_AGENT_ID,
                    role="user", message="hi there",
                )

            llm = _StubLlm("hello back")
            ctx = _StubCtx("usr_a", "ses_1", llm)
            out = await run_orchestrator_message(
                ctx,  # type: ignore[arg-type]
                MessagePayload(prompt="hi there"),
                pool=pool,
                settings=settings,
            )

            assert out.content == "hello back"
            assert out.metadata["context_tokens"] > 0

            # System prompt carried the user-config note + rolling summary.
            assert llm.captured is not None
            system = llm.captured[0]
            assert system.role == "system"
            assert "Ada" in system.content
            assert "prior summary text" in system.content

            # The pre-written user turn was used, not duplicated.
            user_msgs = [m for m in llm.captured if m.role == "user"]
            assert [m.content for m in user_msgs] == ["hi there"]

            # The assistant turn was persisted to the orchestrator thread.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id=ORCHESTRATOR_AGENT_ID
                )
            assert [r.role for r in rows] == ["user", "assistant"]
            assert rows[-1].message == "hello back"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_message_falls_back_to_payload_when_no_user_row(
    suite_db_url: str,
) -> None:
    async def _drive() -> None:
        settings = SuiteSettings(database_url=suite_db_url)
        pool = await open_pool(settings)
        try:
            await _truncate(pool)
            # No user_config, no session_info, no pre-written user row.
            llm = _StubLlm("ok")
            ctx = _StubCtx("usr_x", "ses_x", llm)
            out = await run_orchestrator_message(
                ctx,  # type: ignore[arg-type]
                MessagePayload(prompt="fresh question"),
                pool=pool,
                settings=settings,
            )
            assert out.content == "ok"
            assert llm.captured is not None
            user_msgs = [m for m in llm.captured if m.role == "user"]
            assert [m.content for m in user_msgs] == ["fresh question"]
        finally:
            await pool.close()

    asyncio.run(_drive())
