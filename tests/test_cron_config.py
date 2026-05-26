"""Phase 4 — cron (queries, claim, scheduler firing, management) + config.

Live suite DB; stub LLM / dispatcher / telegram. No router.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bp_agents.agents.chatbot.cron import CronScheduler, run_cron_management
from bp_agents.agents.config.agent import run_config
from bp_agents.agents.orchestrator.agent import run_orchestrator_cron_message
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus
from bp_sdk import LlmResponse, ToolCall  # noqa: E402


class _ScriptLlm:
    def __init__(self, responses) -> None:
        self._responses = list(responses)

    async def generate(self, messages, **kw) -> LlmResponse:
        return self._responses.pop(0) if self._responses else LlmResponse(text="ok")


class _Progress:
    async def emit(self, *a, **k) -> None:
        return None


class _Peers:
    def visible(self, *, for_user_level=None):
        return {}


class _Ctx:
    def __init__(self, llm, *, user_id="usr_a", session_id="ses_1") -> None:
        self.llm = llm
        self.user_id = user_id
        self.session_id = session_id
        self.progress = _Progress()
        self.peers = _Peers()


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, *, chat_id, text) -> None:
        self.sent.append((chat_id, text))


class _CronDispatcher:
    def __init__(self, *, content: str, report: bool) -> None:
        self.content = content
        self.report = report
        self.spawns: list[tuple] = []

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        self.spawns.append((dest, mode, payload))
        return "t"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id="t", status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=self.content, metadata={
                "report": self.report, "reason": "because"}),
        )


def _settings(url: str) -> SuiteSettings:
    return SuiteSettings(database_url=url)


async def _reset(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings, cron_jobs, cron_executions RESTART IDENTITY"
        )


# --------------------------------------------------------------------------- #
# cron queries + atomic claim
# --------------------------------------------------------------------------- #


def test_cron_create_list_claim(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            now = datetime.now(UTC)
            due = now
            async with pool.acquire() as conn:
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_1",
                    cron_expression="* * * * *", cron_message="ping",
                )
                jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
                assert len(jobs) == 1
                # First claim wins; a second claim for the same due window loses.
                assert await queries.claim_cron_job(conn, cron_id="c1", due=due, now=now)
                assert not await queries.claim_cron_job(conn, cron_id="c1", due=due, now=now)
        finally:
            await pool.close()

    asyncio.run(_drive())


# --------------------------------------------------------------------------- #
# scheduler firing + apply
# --------------------------------------------------------------------------- #


def _scheduler(pool, settings, disp, tg):
    locks: dict[str, asyncio.Lock] = {}
    return CronScheduler(
        dispatcher=disp, pool=pool, settings=settings, telegram=tg,
        session_lock=lambda sid: locks.setdefault(sid, asyncio.Lock()),
    )


def test_scheduler_fires_and_reports(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram", chat_id="tg1",
                )
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_1",
                    cron_expression="* * * * *", cron_message="news?",
                    report="case_by_case",
                )
            tg = _Telegram()
            disp = _CronDispatcher(content="Big news!", report=True)
            sched = _scheduler(pool, _settings(suite_db_url), disp, tg)

            fired = await sched.tick()
            assert fired == 1
            # Dispatched orchestrator(cron_message).
            assert disp.spawns[0][1] == "cron_message"
            # Reported → relayed + assistant row + execution logged.
            assert tg.sent == [("tg1", "Big news!")]
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                execs = await conn.fetch("SELECT * FROM cron_executions")
            assert rows and rows[-1].message == "Big news!"
            assert len(execs) == 1 and execs[0]["reported"] is True

            # A second tick in the same minute does not re-fire (claim guard).
            assert await sched.tick() == 0
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_scheduler_no_report_logs_only(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram", chat_id="tg1",
                )
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_1",
                    cron_expression="* * * * *", cron_message="quiet check",
                    report="case_by_case",
                )
            tg = _Telegram()
            disp = _CronDispatcher(content="nothing notable", report=False)
            sched = _scheduler(pool, _settings(suite_db_url), disp, tg)
            await sched.tick()
            assert tg.sent == []  # not reported
            async with pool.acquire() as conn:
                execs = await conn.fetch("SELECT * FROM cron_executions")
            assert len(execs) == 1 and execs[0]["reported"] is False
        finally:
            await pool.close()

    asyncio.run(_drive())


# --------------------------------------------------------------------------- #
# management loops + orchestrator cron_message
# --------------------------------------------------------------------------- #


def test_cron_management_adds_job(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[ToolCall(
                    id="c", name="add_cron",
                    args={"cron_expression": "0 9 * * *", "cron_message": "morning digest"},
                )]),
                LlmResponse(text="Scheduled your morning digest."),
            ])
            ctx = _Ctx(llm)
            out = await run_cron_management(ctx, MessagePayload(prompt="digest at 9"), pool=pool, preset="lite")
            assert "morning digest" in out.content.lower() or "scheduled" in out.content.lower()
            async with pool.acquire() as conn:
                jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
            assert len(jobs) == 1 and jobs[0].cron_expression == "0 9 * * *"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_config_set_persists(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_user_config(conn, user_id="usr_a")
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[ToolCall(
                    id="c", name="set_config",
                    args={"field": "timezone", "value": "Asia/Tokyo"},
                )]),
                LlmResponse(text="Updated your timezone."),
            ])
            ctx = _Ctx(llm)
            await run_config(ctx, MessagePayload(prompt="set tz tokyo"), pool=pool, settings=_settings(suite_db_url))
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
            assert cfg.timezone == "Asia/Tokyo"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_orchestrator_cron_message_structured(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            llm = _ScriptLlm([
                LlmResponse(text="here is the digest"),       # the loop
                LlmResponse(text='{"report": true, "reason": "important"}'),  # decision
            ])
            ctx = _Ctx(llm)
            out = await run_orchestrator_cron_message(
                ctx, MessagePayload(prompt="daily digest"),
                pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "here is the digest"
            assert out.metadata["report"] is True
            assert out.metadata["reason"] == "important"
        finally:
            await pool.close()

    asyncio.run(_drive())


