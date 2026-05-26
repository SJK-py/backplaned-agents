"""bp_agents.agents.l1_common — shared l1 specialist machinery.

Every l1 agent (computer_use, research, deep_reasoning) exposes the same
three modes ([agents.md]):

  - `subagent` (LLMData, tool-visible) — stateless tool face; no history.
  - `on_delegation` (LLMData, tool=false) — first delegated turn.
  - `delegated_message` ({prompt}, tool=false) — subsequent delegated turns.

`on_delegation` / `delegated_message` share one core: reload the agent's
own thread (the orchestrator wrote the `delegate_prompt` seed row + the
channel writes each user turn), run the loop with the `end_delegation`
terminal tool, and either hand back (delegate the task to the
orchestrator) or append the assistant turn. Per-agent behaviour (system
prompt, local tools, preset) is supplied via `L1Config`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bp_agents.common import (
    LocalToolset,
    compose_system_prompt,
    estimate_context_tokens,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.db import queries
from bp_protocol.types import AgentOutput, LLMData
from bp_sdk import Message, TaskContext, ToolSpec

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.settings import SuiteSettings

ORCHESTRATOR_AGENT_ID = "orchestrator"

END_DELEGATION_TOOL = "end_delegation"
END_DELEGATION_SPEC = ToolSpec(
    name=END_DELEGATION_TOOL,
    description=(
        "Hand the conversation back to the main assistant. Call this when "
        "the delegated task is complete or the user wants to do something "
        "else. Provide a short summary of what was accomplished."
    ),
    parameters={
        "type": "object",
        "properties": {
            "delegation_summary": {
                "type": "string",
                "description": "Short recap of what was done while delegated.",
            },
            "exit_reason": {
                "type": "string",
                "description": "Why control is being handed back.",
            },
            "user_prompt": {
                "type": "string",
                "description": (
                    "Optional: a follow-up request to pass to the main "
                    "assistant to act on immediately."
                ),
            },
        },
        "required": ["delegation_summary", "exit_reason"],
    },
)

_GENERAL_DELEGATION = """\
You are operating as a specialist the main assistant delegated this \
conversation to. Carry out the user's request using your tools. When the \
task is done — or the user clearly wants something outside your remit — \
call `end_delegation` to hand control back to the main assistant.\
"""

LocalToolsFactory = Callable[
    [TaskContext, "SuiteSettings", str], Awaitable[LocalToolset | None]
]


@dataclass
class L1Config:
    agent_id: str
    subagent_system: str
    delegation_system: str
    preset_field: str = "preset_balanced"
    local_tools: LocalToolsFactory | None = None
    # SDK file-tools bundle ("read_only" / "full") for file-capable l1s;
    # None disables file tools. `read_file` feeds a file to the model
    # multimodally on the next turn.
    file_tools: str | None = None


def _preset(cfg, settings: SuiteSettings, field: str) -> str:
    if cfg is not None:
        return getattr(cfg, field)
    return getattr(settings, f"default_{field}")


async def _local_tools(
    ctx: TaskContext, settings: SuiteSettings, config: L1Config, timezone: str
) -> LocalToolset | None:
    return (
        await config.local_tools(ctx, settings, timezone)
        if config.local_tools else None
    )


def _llmdata_user(payload: LLMData) -> str:
    parts: list[str] = []
    if payload.context:
        parts.append(f"## Context\n{payload.context}")
    parts.append(payload.prompt)
    return "\n\n".join(parts)


async def run_subagent(
    ctx: TaskContext,
    payload: LLMData,
    *,
    config: L1Config,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """Stateless tool-face execution — no history read/write."""
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    system = config.subagent_system
    if payload.agent_instruction:
        system = f"{system}\n\n{payload.agent_instruction}"
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=_llmdata_user(payload)),
    ]
    timezone = cfg.timezone if cfg else settings.default_timezone
    local = await _local_tools(ctx, settings, config, timezone)
    resp = await run_llm_loop(
        ctx, messages=messages,
        preset=_preset(cfg, settings, config.preset_field), local_tools=local,
        file_tools=config.file_tools,
    )
    return text_output(resp.text)


async def run_delegated_turn(
    ctx: TaskContext,
    *,
    config: L1Config,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """First (`on_delegation`) and subsequent (`delegated_message`)
    delegated turns. Reload this agent's thread, run the loop with
    `end_delegation`, and either hand back to the orchestrator or append
    the assistant turn + return."""
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
        rows = await queries.reload_incumbent(
            conn, session_id=ctx.session_id, agent_id=config.agent_id
        )
        info = await queries.get_session_info(conn, ctx.session_id)

    system = compose_system_prompt(
        f"{_GENERAL_DELEGATION}\n\n{config.delegation_system}",
        config_note=user_config_note(cfg) if cfg else "",
        summary=info.delegate_summary if info else None,
    )
    messages: list[Message] = [Message(role="system", content=system)]
    messages.extend(Message(role=r.role, content=r.message) for r in rows)
    context_tokens = estimate_context_tokens(messages)

    timezone = cfg.timezone if cfg else settings.default_timezone
    local = await _local_tools(ctx, settings, config, timezone)
    resp = await run_llm_loop(
        ctx, messages=messages,
        preset=_preset(cfg, settings, config.preset_field), local_tools=local,
        extra_tools=[END_DELEGATION_SPEC], terminal_tools={END_DELEGATION_TOOL},
        file_tools=config.file_tools,
    )

    end_call = next(
        (tc for tc in resp.tool_calls if tc.name == END_DELEGATION_TOOL), None
    )
    if end_call is not None:
        # Hand back: delegate the task to the orchestrator. The router
        # drops THIS agent's (now non-active) Result; the orchestrator's
        # end_delegation produces the terminal Result.
        await ctx.peers.delegate(
            ORCHESTRATOR_AGENT_ID, dict(end_call.args or {}), mode="end_delegation"
        )
        return AgentOutput()

    async with pool.acquire() as conn:
        await queries.append_history(
            conn, session_id=ctx.session_id, agent_id=config.agent_id,
            role="assistant", message=resp.text,
        )
    return text_output(resp.text, context_tokens=context_tokens)
