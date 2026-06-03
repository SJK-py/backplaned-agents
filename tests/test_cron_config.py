"""Phase 4 — cron (queries, claim, scheduler firing, management) + config.

Live suite DB; stub LLM / dispatcher / telegram. No router.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bp_agents.agents.chatbot.cron import CronScheduler
from bp_agents.agents.config.agent import run_config
from bp_agents.agents.orchestrator.agent import run_orchestrator_cron_message
from bp_agents.common.payloads import MessagePayload
from bp_agents.cron_manage import run_cron_management
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


def test_scheduler_webapp_session_nudges_telegram(suite_db_url: str) -> None:
    """A cron whose target session is a webapp session (no Telegram chat_id)
    persists the result there AND sends a pointer nudge to the user's
    Telegram mapping ([cron.md] §6)."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                # Webapp session: channel='webapp', no chat_id.
                await queries.create_session_info(
                    conn, session_id="ses_web", user_id="usr_a", channel="webapp",
                )
                # The user is also reachable on Telegram.
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg1", user_id="usr_a"
                )
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_web",
                    cron_expression="* * * * *", cron_message="digest",
                    report="case_by_case",
                )
            tg = _Telegram()
            disp = _CronDispatcher(content="Web digest body", report=True)
            sched = _scheduler(pool, _settings(suite_db_url), disp, tg)

            assert await sched.tick() == 1
            # Pointer nudge to Telegram — NOT the full content.
            assert len(tg.sent) == 1
            chat_id, text = tg.sent[0]
            assert chat_id == "tg1"
            assert "web app" in text and "Web digest body" not in text
            # Canonical result still landed in the webapp session.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_web", agent_id="orchestrator"
                )
            assert rows and rows[-1].message == "Web digest body"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_scheduler_webapp_session_no_mapping_persists_only(suite_db_url: str) -> None:
    """No out-of-band channel (webapp session, no Telegram mapping): the row
    is the record, nothing is sent — no crash, no spurious send."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id="ses_web", user_id="usr_a", channel="webapp",
                )
                await queries.create_cron_job(
                    conn, cron_id="c1", user_id="usr_a", session_id="ses_web",
                    cron_expression="* * * * *", cron_message="digest",
                    report="case_by_case",
                )
            tg = _Telegram()
            disp = _CronDispatcher(content="Web digest body", report=True)
            sched = _scheduler(pool, _settings(suite_db_url), disp, tg)

            assert await sched.tick() == 1
            assert tg.sent == []  # no reachable channel
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_web", agent_id="orchestrator"
                )
                execs = await conn.fetch("SELECT * FROM cron_executions")
            assert rows and rows[-1].message == "Web digest body"
            assert len(execs) == 1 and execs[0]["reported"] is True
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


def test_cron_management_empty_reply_lists_jobs(suite_db_url: str) -> None:
    """If the model calls a tool and stops (no prose), the management loop
    falls back to listing jobs rather than a bare 'Done.'."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[ToolCall(
                    id="c", name="add_cron",
                    args={"cron_expression": "0 8 * * *", "cron_message": "standup"},
                )]),
                LlmResponse(text=""),  # empty final turn
            ])
            out = await run_cron_management(
                _Ctx(llm), MessagePayload(prompt="standup at 8"), pool=pool, preset="lite"
            )
            assert "0 8 * * *" in out.content and "standup" in out.content
            assert "Done." not in out.content
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_config_empty_reply_shows_settings(suite_db_url: str) -> None:
    """An empty model turn falls back to the current settings, not 'Done.'."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_user_config(
                    conn, user_id="usr_a", timezone="Asia/Seoul"
                )
            llm = _ScriptLlm([LlmResponse(text="")])  # model says nothing
            out = await run_config(
                _Ctx(llm), MessagePayload(prompt="show settings"),
                pool=pool, settings=_settings(suite_db_url),
            )
            assert "timezone: Asia/Seoul" in out.content
            assert out.content != "Done."
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


def test_config_system_prompt_has_language_directive() -> None:
    """No language → no directive; a language → an explicit 'reply in it'."""
    from bp_agents.agents.config.agent import _system_prompt

    assert "preferred language" not in _system_prompt({})
    p = _system_prompt({}, language="ko")
    assert "preferred language" in p
    assert "ko" in p


def test_config_reply_uses_user_language(suite_db_url: str) -> None:
    """run_config threads the user's `language` into the system prompt, so
    /config (which bypasses the orchestrator) still replies in their language."""
    captured: dict = {}

    class _CapturingLlm(_ScriptLlm):
        async def generate(self, messages, **kw):
            captured["system"] = messages[0].content
            return await super().generate(messages, **kw)

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _reset(pool)
            async with pool.acquire() as conn:
                await queries.create_user_config(
                    conn, user_id="usr_a", language="ko"
                )
            llm = _CapturingLlm([LlmResponse(text="설정을 보여드릴게요.")])
            await run_config(
                _Ctx(llm), MessagePayload(prompt="show settings"),
                pool=pool, settings=_settings(suite_db_url),
            )
            assert "ko" in captured["system"]
            assert "preferred language" in captured["system"]
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


