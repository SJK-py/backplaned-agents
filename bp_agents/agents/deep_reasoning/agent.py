"""deep_reasoning agent — l1 planning/reasoning specialist (Phase 3)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bp_agents.agents.l1_common import L1Config, run_delegated_turn, run_subagent
from bp_agents.common import LocalToolset, make_current_time_tool
from bp_agents.common.payloads import MessagePayload
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

DEEP_REASONING_AGENT_ID = "deep_reasoning"

_SUBAGENT_SYSTEM = """\
You are a careful reasoning specialist. Think step by step, break the \
problem down, and produce a clear, well-structured answer.\
"""
_DELEGATION_SYSTEM = """\
You specialise in planning and multi-step reasoning. Work the problem \
methodically with the user.\
"""


async def _tools(ctx: TaskContext, settings: SuiteSettings) -> LocalToolset:
    return LocalToolset([make_current_time_tool(settings.default_timezone)])


_CONFIG = L1Config(
    agent_id=DEEP_REASONING_AGENT_ID,
    subagent_system=_SUBAGENT_SYSTEM,
    delegation_system=_DELEGATION_SYSTEM,
    preset_field="preset_pro",
    local_tools=_tools,
)


agent = Agent(
    info=AgentInfo(
        agent_id=DEEP_REASONING_AGENT_ID,
        description="Planning and multi-step reasoning specialist.",
        groups=["l1"],
        capabilities=[
            "agent.orchestration", "llm.generation.text", "llm.multimodal.image",
            "assistant.planning", "assistant.reasoning", "file.full",
            "session.history",
        ],
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None


@agent.on_startup
async def _startup() -> None:
    global _pool  # noqa: PLW0603 — startup-wired handle
    _pool = await open_pool(_settings)


@agent.on_shutdown
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


@agent.handler(mode="subagent")
async def subagent(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_subagent(ctx, payload, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(mode="on_delegation", tool=False)
async def on_delegation(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(ctx, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(mode="delegated_message", tool=False)
async def delegated_message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(ctx, config=_CONFIG, pool=_pool, settings=_settings)


if __name__ == "__main__":
    agent.run()
