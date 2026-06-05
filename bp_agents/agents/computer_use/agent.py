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
inspect, edit, build, and test; report concrete results.

The sandbox workspace and the shared file stash are separate places — only \
the stash is visible to the caller. To return a file, get it into the shared \
stash and include its reference in your reply (`<name>`, or `persist/<name>` \
for the persistent stash); the caller can then deliver or use it by \
reference. You can't send files to the user yourself.\
"""
_DELEGATION_SYSTEM = """\
You are the coding/computer specialist. Use the sandbox to get the user's \
task done end-to-end.

You work across TWO separate file areas, and they are not the same place:
- the sandbox WORKSPACE — where `call_sandbox` runs commands and reads/writes \
files; and
- the shared file STASH — the only place the user and other agents can see, \
and what `send_file` delivers from.
A file produced in the sandbox is NOT in the stash, and a stash file isn't in \
the sandbox until you put it there. To hand the user a file, first get it into \
the shared stash, then `send_file` that name and write your reply in the same \
turn (a file is never sent on its own).\
"""


async def _tools(
    ctx: TaskContext, settings: SuiteSettings, timezone: str
) -> LocalToolset:
    return LocalToolset([make_current_time_tool(timezone)])


_CONFIG = L1Config(
    agent_id=COMPUTER_USE_AGENT_ID,
    subagent_system=_SUBAGENT_SYSTEM,
    delegation_system=_DELEGATION_SYSTEM,
    preset_field="preset_balanced",
    local_tools=_tools,
    file_tools="full",
)


agent = Agent(
    info=AgentInfo(
        agent_id=COMPUTER_USE_AGENT_ID,
        description="Coding/computer specialist that drives a sandboxed bash environment.",
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


@agent.handler(
    mode="subagent",
    description="Carry out a coding/computer task in the sandbox (inspect, "
    "edit, build, run, test) and report concrete results.",
)
async def subagent(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_subagent(ctx, payload, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(
    mode="on_delegation", tool=False,
    description="First turn after the orchestrator delegates a coding "
    "conversation (delegation lifecycle; not a tool).",
)
async def on_delegation(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(
        ctx, config=_CONFIG, pool=_pool, settings=_settings, first_turn=True
    )


@agent.handler(
    mode="delegated_message", tool=False,
    description="A user turn while computer_use holds the delegated "
    "conversation (delegation lifecycle; not a tool).",
)
async def delegated_message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(
        ctx, config=_CONFIG, pool=_pool, settings=_settings, first_turn=False
    )


if __name__ == "__main__":
    agent.run()
