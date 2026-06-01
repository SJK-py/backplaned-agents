"""bp_agents.agents.l1_common — shared l1 specialist machinery.

Every l1 agent (computer_use, research, deep_reasoning) exposes the same
three modes ([agents.md]):

  - `subagent` (LLMData, tool-visible) — stateless tool face; no history.
  - `on_delegation` (LLMData, tool=false) — first delegated turn.
  - `delegated_message` ({prompt}, tool=false) — subsequent delegated turns.

`on_delegation` / `delegated_message` share one core: reload the agent's
own thread (the orchestrator wrote the `delegate_prompt` seed row + the
channel writes each user turn), run the loop, and append the assistant
turn. Per-agent behaviour (system prompt, local tools, preset) is
supplied via `L1Config`.

Delegation is a **persistent** episode, so `end_delegation` is offered
**only on subsequent turns** (`delegated_message`), never on the first
(`on_delegation`). The first turn always does substantive work and
returns its own Result on the hand-off task `T`; the channel observes
that result came from the delegate and pins `delegated_to`. Letting the
first turn hand back would re-delegate `T` to the orchestrator — which
the router correctly rejects as a cycle, since `T` originated there.
One-shot work belongs in the stateless `subagent` mode (a peer-tool
call), not a delegation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bp_agents.common import (
    LocalToolset,
    compose_system_prompt,
    estimate_context_tokens,
    make_send_file_tool,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.db import queries
from bp_protocol.types import AgentOutput, LLMData
from bp_sdk import Message, TaskContext, ToolCall, ToolSpec

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
conversation to. Carry out the user's request using your tools. To give \
the user an actual file, call `send_file` with its stash name — it is \
delivered as an attachment alongside your reply. `send_file` only QUEUES \
the file: you must still write your text reply in the same turn, not \
`end_delegation`. A file is never sent on its own. Files live in a shared \
stash; when you're given a file name, you can call `read_file` to see its \
contents, or pass a name along to hand a file to another agent — because \
the stash is shared, the name is enough.\
"""

# Appended only on subsequent turns, where the hand-back tool is offered.
_HANDBACK_NOTE = """\
When the user clearly wants something outside your remit — call \
`end_delegation` to hand control back to the main assistant.\
"""

LocalToolsFactory = Callable[
    [TaskContext, "SuiteSettings", str], Awaitable[LocalToolset | None]
]
# Handler for an agent-specific terminal tool (e.g. deep_reasoning's
# `plan_mode`): given the firing tool call, produce the turn's result.
ExtraTerminalHandler = Callable[
    [TaskContext, ToolCall, "asyncpg.Pool", "SuiteSettings"],
    Awaitable[AgentOutput],
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
    # Agent-specific terminal tools offered on delegated turns (both first
    # and subsequent). When the model calls one, `on_extra_terminal`
    # produces the turn result instead of the normal assistant reply —
    # used by deep_reasoning's `plan_mode`.
    extra_terminal: list[ToolSpec] = field(default_factory=list)
    on_extra_terminal: ExtraTerminalHandler | None = None


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


_SUBAGENT_ROLE = """\
You are running as a subagent: another agent has called you as a tool to \
carry out the task below. Your reply is returned to that calling agent — it \
is NOT sent to the user, who will not see it. Produce a complete, \
self-contained result the caller can use directly; don't address the user \
or assume they see your output.\
"""


def compose_subagent_system(base: str, payload: LLMData) -> str:
    """System prompt for a stateless subagent call: the agent's own role
    (`base`), the shared subagent framing (output goes to the CALLER, not
    the user), then the caller-supplied context and instruction under
    explicit headers. Context/instruction are skipped when absent."""
    system = f"{base}\n\n{_SUBAGENT_ROLE}"
    if payload.context:
        system += f"\n\n## Context from the calling agent\n{payload.context}"
    if payload.agent_instruction:
        system += f"\n\n## Instruction from the calling agent\n{payload.agent_instruction}"
    return system


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
    messages = [
        Message(role="system", content=compose_subagent_system(config.subagent_system, payload)),
        Message(role="user", content=payload.prompt),
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
    first_turn: bool,
) -> AgentOutput:
    """First (`on_delegation`, `first_turn=True`) and subsequent
    (`delegated_message`) delegated turns. Reload this agent's thread, run
    the loop, and append the assistant turn.

    `end_delegation` is offered only when `first_turn` is False. On the
    first turn the delegate must do work and terminate the hand-off task
    `T` itself; handing back there would re-delegate `T` to the
    orchestrator (`T`'s originator) and the router rejects that as a
    cycle. Subsequent turns run on fresh tasks spawned straight to this
    agent, so handing back to the orchestrator is cycle-free."""
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
        rows = await queries.reload_incumbent(
            conn, session_id=ctx.session_id, agent_id=config.agent_id
        )
        info = await queries.get_session_info(conn, ctx.session_id)

    guidance = _GENERAL_DELEGATION if first_turn else _GENERAL_DELEGATION + _HANDBACK_NOTE
    system = compose_system_prompt(
        f"{guidance}\n\n{config.delegation_system}",
        config_note=user_config_note(cfg) if cfg else "",
        summary=info.delegate_summary if info else None,
    )
    messages: list[Message] = [Message(role="system", content=system)]
    messages.extend(Message(role=r.role, content=r.message) for r in rows)
    context_tokens = estimate_context_tokens(messages)

    timezone = cfg.timezone if cfg else settings.default_timezone
    # A delegate talks to the user directly, so it can deliver files via
    # `send_file` (recorded into `outbound` → AgentOutput.files).
    outbound: list[str] = []
    local = await _local_tools(ctx, settings, config, timezone) or LocalToolset()
    local.add(make_send_file_tool(outbound))

    # Terminal tools: end_delegation (subsequent turns only) + any
    # agent-specific ones (e.g. plan_mode), offered on every turn.
    extra_specs = list(config.extra_terminal)
    terminal = {t.name for t in extra_specs}
    if not first_turn:
        extra_specs.append(END_DELEGATION_SPEC)
        terminal.add(END_DELEGATION_TOOL)
    resp = await run_llm_loop(
        ctx, messages=messages,
        preset=_preset(cfg, settings, config.preset_field), local_tools=local,
        extra_tools=extra_specs or None, terminal_tools=terminal or None,
        file_tools=config.file_tools, detail_chars=settings.verbose_detail_chars,
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

    if config.on_extra_terminal is not None:
        extra_names = {t.name for t in config.extra_terminal}
        extra_call = next(
            (tc for tc in resp.tool_calls if tc.name in extra_names), None
        )
        if extra_call is not None:
            return await config.on_extra_terminal(ctx, extra_call, pool, settings)

    async with pool.acquire() as conn:
        await queries.append_history(
            conn, session_id=ctx.session_id, agent_id=config.agent_id,
            role="assistant", message=resp.text,
        )
    return text_output(resp.text, files=outbound, context_tokens=context_tokens)
