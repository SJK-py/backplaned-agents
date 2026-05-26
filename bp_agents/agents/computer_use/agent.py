"""computer_use agent — l1 coding/computer specialist (Phase 3)."""

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

COMPUTER_USE_AGENT_ID = "computer_use"

_SUBAGENT_SYSTEM = """\
You handle coding and computer tasks. You have a sandbox you drive via \
the `call_sandbox` tool (bash + file workspace bridges). Run commands to \
inspect, edit, build, and test; report concrete results.\
"""
_DELEGATION_SYSTEM = """\
You are the coding/computer specialist. Use the sandbox to get the user's \
task done end-to-end.\
"""


async def _tools(ctx: TaskContext, settings: SuiteSettings) -> LocalToolset:
    return LocalToolset([make_current_time_tool(settings.default_timezone)])


_CONFIG = L1Config(
    agent_id=COMPUTER_USE_AGENT_ID,
    subagent_system=_SUBAGENT_SYSTEM,
    delegation_system=_DELEGATION_SYSTEM,
    preset_field="preset_balanced",
    local_tools=_tools,
)


agent = Agent(
    info=AgentInfo(
        agent_id=COMPUTER_USE_AGENT_ID,
        description="Coding and computer tasks via the sandbox.",
        groups=["l1"],
        capabilities=[
            "llm.generation.text", "assistant.coding", "assistant.computer",
            "file.full", "computer.bash", "computer.network", "session.history",
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
