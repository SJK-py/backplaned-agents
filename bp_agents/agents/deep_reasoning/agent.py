"""deep_reasoning agent — l1 planning/reasoning specialist (Phase 3)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bp_agents.agents.deep_reasoning.plan import run_plan
from bp_agents.agents.l1_common import L1Config, run_delegated_turn, run_subagent
from bp_agents.common import (
    FILE_DELIVERY_NOTE,
    LocalToolset,
    make_current_time_tool,
)
from bp_agents.common.payloads import MessagePayload
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext, ToolCall, ToolSpec

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

DEEP_REASONING_AGENT_ID = "deep_reasoning"

_SUBAGENT_SYSTEM = """\
You are a careful reasoning specialist. Think step by step, break the \
problem down, and produce a clear, well-structured answer.\
"""
_DELEGATION_SYSTEM = f"""\
You specialise in planning and multi-step reasoning. Work the problem \
methodically with the user. For a genuinely multi-step task — one that \
needs several distinct sub-tasks carried out and combined — call \
`plan_mode` with the objective (and optional initial steps) to build and \
execute an explicit plan. For anything you can answer in one pass, just \
answer directly.

{FILE_DELIVERY_NOTE}\
"""

PLAN_MODE_TOOL = "plan_mode"
_PLAN_MODE_SPEC = ToolSpec(
    name=PLAN_MODE_TOOL,
    description=(
        "Enter structured planning for a complex, multi-step task. Provide the "
        "overall objective and an optional initial list of steps; you then "
        "manage and execute the plan step by step and report the result. Use "
        "only for genuinely multi-step work — answer directly otherwise."
    ),
    parameters={
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "The overall goal the plan should accomplish.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional initial steps; you can revise them as you go.",
            },
        },
        "required": ["objective"],
    },
)


async def _enter_plan(
    ctx: TaskContext, tool_call: ToolCall, pool: asyncpg.Pool, settings: SuiteSettings
) -> AgentOutput:
    args = tool_call.args or {}
    objective = str(args.get("objective") or "").strip()
    steps = [s for s in (args.get("steps") or []) if isinstance(s, str)]
    return await run_plan(
        ctx, objective=objective, initial_steps=steps, pool=pool, settings=settings
    )


async def _tools(
    ctx: TaskContext, settings: SuiteSettings, timezone: str
) -> LocalToolset:
    return LocalToolset([make_current_time_tool(timezone)])


_CONFIG = L1Config(
    agent_id=DEEP_REASONING_AGENT_ID,
    subagent_system=_SUBAGENT_SYSTEM,
    delegation_system=_DELEGATION_SYSTEM,
    preset_field="preset_pro",
    local_tools=_tools,
    file_tools="full",
    extra_terminal=[_PLAN_MODE_SPEC],
    on_extra_terminal=_enter_plan,
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


@agent.handler(
    mode="subagent",
    description="Work a hard, multi-step reasoning or planning sub-problem "
    "and return a structured, worked-through answer.",
)
async def subagent(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_subagent(ctx, payload, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(
    mode="on_delegation", tool=False,
    description="First turn after the orchestrator delegates a reasoning "
    "conversation; may enter plan_mode (delegation lifecycle; not a tool).",
)
async def on_delegation(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(
        ctx, config=_CONFIG, pool=_pool, settings=_settings, first_turn=True
    )


@agent.handler(
    mode="delegated_message", tool=False,
    description="A user turn while deep_reasoning holds the delegated "
    "conversation (delegation lifecycle; not a tool).",
)
async def delegated_message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(
        ctx, config=_CONFIG, pool=_pool, settings=_settings, first_turn=False
    )


if __name__ == "__main__":
    agent.run()
