"""deep_reasoning plan_mode — the in-process planning sub-loop and the
l1_common terminal-tool seam that enters it."""

from __future__ import annotations

import asyncio

from bp_agents.agents.deep_reasoning.plan import (
    _ADD,
    _EXECUTE,
    _QUIT,
    run_plan,
)
from bp_agents.agents.l1_common import L1Config, run_delegated_turn
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, LLMData, TaskStatus
from bp_sdk import LlmResponse, ToolCall, ToolSpec


def _settings(url: str, **kw) -> SuiteSettings:
    return SuiteSettings(database_url=url, **kw)


class _ScriptLlm:
    """Returns scripted responses in order; a bare 'done' once exhausted."""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._r = list(responses)
        self.calls = 0

    async def generate(self, messages, **kw) -> LlmResponse:
        self.calls += 1
        return self._r.pop(0) if self._r else LlmResponse(text="done")


class _Progress:
    async def emit(self, *a, **k) -> None:
        return None


def _child(content: str, *, ok: bool = True, files=None) -> ResultFrame:
    return ResultFrame(
        agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
        task_id="c", status=TaskStatus.SUCCEEDED if ok else TaskStatus.FAILED,
        status_code=200 if ok else 500,
        output=AgentOutput(content=content, files=files or []),
    )


class _PlanPeers:
    def __init__(self, results=None) -> None:
        self.spawns: list[tuple] = []
        self._results = list(results or [])

    async def spawn(self, dest, payload, *, mode=None, wait=True, timeout_s=None, **kw):
        self.spawns.append((dest, payload, mode))
        return self._results.pop(0) if self._results else _child("ok")

    def visible(self, *, for_user_level=None):
        return {}


class _Files:
    def __init__(self, names=()) -> None:
        self._names = list(names)

    async def list(self, *, persistent=False, query=None):
        return [] if persistent else list(self._names)


class _Ctx:
    def __init__(self, llm, peers, *, files=None, user_id="usr_a", session_id="ses_1"):
        self.llm = llm
        self.peers = peers
        self.progress = _Progress()
        self.files = files
        self.user_id = user_id
        self.session_id = session_id
        self.user_level = "tier0"
        self.delegating_agent_id = None


def _tc(tool: str, **args) -> ToolCall:
    return ToolCall(id=tool, name=tool, args=args)


def _exec(**args) -> LlmResponse:
    return LlmResponse(text="", tool_calls=[_tc(_EXECUTE, relevant_context="", **args)])


def test_plan_executes_then_reports(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            llm = _ScriptLlm([
                _exec(),  # decision 1 → execute step 1
                _exec(),  # decision 2 → execute step 2
                LlmResponse(text="Combined answer."),  # plan-exhausted final loop
            ])
            peers = _PlanPeers([_child("result A"), _child("result B")])
            ctx = _Ctx(llm, peers, files=_Files())
            out = await run_plan(
                ctx, objective="do A then B", initial_steps=["step A", "step B"],
                pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "Combined answer."
            # Two steps executed via orchestrator(subagent).
            assert [s[2] for s in peers.spawns] == ["subagent", "subagent"]
            assert all(s[0] == "orchestrator" for s in peers.spawns)
            assert [s[1].prompt for s in peers.spawns] == ["step A", "step B"]
            assert all(isinstance(s[1], LLMData) for s in peers.spawns)
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_plan_quit_short_circuits(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[_tc(_QUIT, result_content="done early")]),
            ])
            peers = _PlanPeers()
            out = await run_plan(
                ctx=_Ctx(llm, peers, files=_Files()), objective="o",
                initial_steps=["x"], pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "done early"
            assert peers.spawns == []  # never executed a step
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_plan_add_step_then_execute(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[
                    _tc(_ADD, add_after_num=0, contents="only step")]),  # build plan
                _exec(),                                                  # execute it
                LlmResponse(text="wrapped up"),                           # finalize
            ])
            peers = _PlanPeers([_child("step done")])
            out = await run_plan(
                ctx=_Ctx(llm, peers, files=_Files()), objective="o",
                initial_steps=[], pool=pool, settings=_settings(suite_db_url),
            )
            assert out.content == "wrapped up"
            assert len(peers.spawns) == 1
            assert peers.spawns[0][1].prompt == "only step"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_plan_iter_budget_terminates(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            # Model keeps trying to add steps; the iter budget must stop it.
            class _Loop:
                calls = 0

                async def generate(self, messages, **kw):
                    _Loop.calls += 1
                    return LlmResponse(text="", tool_calls=[
                        _tc(_ADD, add_after_num=0, contents="another")])

            out = await run_plan(
                ctx=_Ctx(_Loop(), _PlanPeers(), files=_Files()), objective="o",
                initial_steps=[], pool=pool,
                settings=_settings(suite_db_url, plan_max_iters=3),
            )
            assert "step budget" in out.content
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_plan_send_file_delivers(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            # Final loop: the model marks a stash file then writes the answer.
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[
                    _tc("send_file", name="report.md")]),  # dispatched (non-terminal)
                LlmResponse(text="Here is your report."),   # final answer
            ])
            out = await run_plan(
                ctx=_Ctx(llm, _PlanPeers(), files=_Files(names=["report.md"])),
                objective="o", initial_steps=[], pool=pool,
                settings=_settings(suite_db_url),
            )
            assert out.content == "Here is your report."
            assert out.files == ["report.md"]
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- l1_common seam: a delegated turn routes an extra-terminal tool call ---


_SENTINEL = AgentOutput(content="handler ran")


def test_delegated_turn_invokes_extra_terminal(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE TABLE session_history, session_info, user_config, "
                    "suite_platform_mappings RESTART IDENTITY"
                )
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a", channel="chatbot_telegram"
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="deep_reasoning",
                    role="user", message="plan something complex",
                )

            seen: list[ToolCall] = []

            async def _handler(ctx, tool_call, pool_, settings_) -> AgentOutput:
                seen.append(tool_call)
                return _SENTINEL

            spec = ToolSpec(name="plan_mode", description="enter planning",
                            parameters={"type": "object", "properties": {}})
            cfg = L1Config(
                agent_id="deep_reasoning", subagent_system="s", delegation_system="d",
                extra_terminal=[spec], on_extra_terminal=_handler,
            )
            llm = _ScriptLlm([
                LlmResponse(text="", tool_calls=[_tc("plan_mode")]),
            ])
            out = await run_delegated_turn(
                _Ctx(llm, _PlanPeers(), files=_Files()), config=cfg, pool=pool,
                settings=_settings(suite_db_url), first_turn=True,
            )
            assert out is _SENTINEL
            assert len(seen) == 1 and seen[0].name == "plan_mode"
        finally:
            await pool.close()

    asyncio.run(_drive())
