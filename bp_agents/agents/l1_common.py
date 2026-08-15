"""bp_agents.agents.l1_common — shared l1 specialist machinery.

Every l1 agent (computer_use, research, deep_reasoning) exposes the same
three modes ([agents.md]):

  - `subagent` (LLMData, tool-visible) — stateless tool face; no history.
  - `on_delegation` (LLMData, tool=false) — first delegated turn.
  - `delegated_message` ({prompt}, tool=false) — subsequent delegated turns.

`on_delegation` / `delegated_message` share one core: open this agent's own
thread in the router session store — draining any hand-over the channel or
the orchestrator left for it, and recording the user's message from the task
payload — run the loop, and close the turn. Per-agent behaviour (system
prompt, local tools, slot) is supplied via `L1Config`.

Nothing here writes another agent's thread, and nothing else writes this
one: the orchestrator's hand-off arrives as `LLMData` the delegate composes
its own seed from, and the channel's `/delegate` switch arrives as a
hand-over item. Both are materialised by `common.thread.open_turn` under
this agent's authorship.

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

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bp_agents import slots
from bp_agents.common import (
    INCOMING_FILE_NOTE,
    LocalToolset,
    close_turn,
    compose_system_prompt,
    context_tokens_of,
    make_recall_tool_history_tool,
    make_send_file_tool,
    maybe_fold,
    open_turn,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.user_prefs import load_prefs
from bp_protocol.types import AgentOutput, LLMData
from bp_sdk import Message, TaskContext, ToolCall, ToolSpec

if TYPE_CHECKING:
    from bp_agents.settings import SuiteSettings

ORCHESTRATOR_AGENT_ID = "orchestrator"

END_DELEGATION_TOOL = "end_delegation"
END_DELEGATION_SPEC = ToolSpec(
    name=END_DELEGATION_TOOL,
    description=(
        "Hand the conversation back to the main assistant. Call this when the "
        "user's request falls outside your remit and the main assistant should "
        "take over — NOT to report finished work (keep handling the "
        "conversation while it stays on-topic). Provide a short summary of what "
        "was done while delegated."
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
                    "Optional. A specific follow-up request for the main "
                    "assistant. If omitted, the user's current message is "
                    "forwarded automatically — so you usually don't need to "
                    "set this."
                ),
            },
        },
        "required": ["delegation_summary", "exit_reason"],
    },
)

# Shared framing every delegate gets on every turn. Deliberately generic: no
# file instructions (file tools are a per-agent capability — each agent's own
# delegation_system covers them) and no `end_delegation` (the first turn isn't
# offered it; the hand-back note below is appended only on subsequent turns).
_GENERAL_DELEGATION = """\
You are operating as a specialist the main assistant delegated this \
conversation to. Carry out the user's request using your tools.\
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
    [TaskContext, ToolCall, "SuiteSettings"],
    Awaitable[AgentOutput],
]


@dataclass
class L1Config:
    agent_id: str
    subagent_system: str
    delegation_system: str
    # Router preset SLOT this agent's turns run on (`bp_agents.slots`). The
    # user's preference for the slot is resolved router-side against their
    # tier gate — the agent neither reads nor stores a preset name.
    slot: str = slots.BALANCED
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
    settings: SuiteSettings,
) -> AgentOutput:
    """Stateless tool-face execution — no history read/write.

    "Stateless" means no THREAD: the user-scoped preference read below is
    not conversation, and it is what tells this call the user's timezone."""
    prefs = await load_prefs(ctx, settings)
    messages = [
        Message(role="system", content=compose_subagent_system(config.subagent_system, payload)),
        Message(role="user", content=payload.prompt),
    ]
    local = await _local_tools(ctx, settings, config, prefs.timezone)
    resp = await run_llm_loop(
        ctx, messages=messages,
        slot=config.slot, local_tools=local,
        file_tools=config.file_tools,
        multimodal_preset=settings.default_preset_multimodal or None,
        text_only_presets=settings.text_only_presets,
    )
    return text_output(resp.text)


async def run_delegated_turn(
    ctx: TaskContext,
    *,
    config: L1Config,
    settings: SuiteSettings,
    first_turn: bool,
    seed: LLMData | None = None,
    user_text: str | None = None,
) -> AgentOutput:
    """First (`on_delegation`, `first_turn=True`) and subsequent
    (`delegated_message`) delegated turns. Open this agent's thread, run the
    loop, and close the turn.

    `seed` is the `LLMData` an orchestrator hand-off carries. The delegate
    composes its own opening row from it rather than being handed one: the
    orchestrator cannot write here, and composing at this end also removes
    the orphan-seed rollback the old shape needed — a seed written before a
    reassignment that can fail.

    `end_delegation` is offered only when `first_turn` is False. On the first
    turn the delegate must do work and terminate the hand-off task `T`
    itself; handing back there would re-delegate `T` to the orchestrator
    (`T`'s originator) and the router rejects that as a cycle. Subsequent
    turns run on fresh tasks spawned straight to this agent, so handing back
    to the orchestrator is cycle-free."""
    # Two independent round trips into the router's store — the user's
    # settings (user scope) and this thread's window (session scope). A batch
    # carries one scope, so they cannot be merged; running them concurrently
    # keeps the preference read off the turn's critical path.
    prefs, turn = await asyncio.gather(
        load_prefs(ctx, settings),
        open_turn(
            ctx, config.agent_id,
            user_text=_seed_text(seed) if seed is not None else user_text,
        ),
    )

    # System prompt = shared harness framing + the agent's own instruction +
    # (subsequent turns, file-capable agents only) the incoming-file mechanic.
    # INCOMING_FILE_NOTE goes AFTER delegation_system so it sits next to that
    # agent's own file-delivery guidance — all file handling reads as one
    # block. The incoming note is subsequent-turns-only: the first turn is
    # seeded by the orchestrator's hand-off, so the user can't attach to the
    # delegate yet. _HANDBACK_NOTE likewise only when end_delegation is offered.
    parts = [_GENERAL_DELEGATION]
    if not first_turn:
        parts.append(_HANDBACK_NOTE)
    parts.append(config.delegation_system)
    if not first_turn and config.file_tools:
        parts.append(INCOMING_FILE_NOTE)
    base_system = "\n\n".join(parts)
    config_note = user_config_note(prefs)

    turn = await maybe_fold(
        ctx, turn,
        system=compose_system_prompt(
            base_system, config_note=config_note, summary=turn.summary
        ),
        limit_tokens=prefs.max_context_token_limit,
    )
    system = compose_system_prompt(
        base_system, config_note=config_note, summary=turn.summary
    )
    messages: list[Message] = [Message(role="system", content=system)]
    messages.extend(turn.context())
    context_tokens = await context_tokens_of(system, turn)

    # A delegate talks to the user directly, so it can deliver files via
    # `send_file` (recorded into `outbound` → AgentOutput.files).
    outbound: list[str] = []
    local = await _local_tools(ctx, settings, config, prefs.timezone) or LocalToolset()
    local.add(make_send_file_tool(outbound))
    local.add(make_recall_tool_history_tool(agent_id=config.agent_id))

    # Terminal tools: end_delegation (subsequent turns only) + any
    # agent-specific ones (e.g. plan_mode), offered on every turn.
    extra_specs = list(config.extra_terminal)
    terminal = {t.name for t in extra_specs}
    if not first_turn:
        extra_specs.append(END_DELEGATION_SPEC)
        terminal.add(END_DELEGATION_TOOL)
    resp = await run_llm_loop(
        ctx, messages=messages,
        slot=config.slot, local_tools=local,
        extra_tools=extra_specs or None, terminal_tools=terminal or None,
        file_tools=config.file_tools,
        multimodal_preset=settings.default_preset_multimodal or None,
        text_only_presets=settings.text_only_presets,
        detail_chars=settings.verbose_detail_chars,
    )

    end_call = next(
        (tc for tc in resp.tool_calls if tc.name == END_DELEGATION_TOOL), None
    )
    if end_call is not None:
        # Hand back: delegate the task to the orchestrator. The router
        # drops THIS agent's (now non-active) Result; the orchestrator's
        # end_delegation produces the terminal Result.
        args = dict(end_call.args or {})
        # end_delegation fires when the user's current message falls OUTSIDE
        # this specialist's remit — so that message is unanswered. Forward it
        # as `user_prompt` so the orchestrator actually answers it; without a
        # prompt its hand-back handler returns an empty Result and the user
        # sees "(no response)". A prompt the model deliberately set wins.
        if not (args.get("user_prompt") or "").strip():
            last_user = turn.last_user_text()
            if last_user:
                args["user_prompt"] = last_user
        # Safeguard: end_delegation is a hand-off, not a way to report work,
        # so queuing a file then handing back is rare and not the intended
        # path. But if a specialist did produce a file before realising the
        # rest is out of remit, carry it through rather than silently dropping
        # the attachment — the orchestrator delivers it.
        if outbound:
            args["files"] = list(outbound)
        # Persist BEFORE delegating: `ctx.peers.delegate` flips the task's
        # active executor to the orchestrator, and this agent stops being
        # able to write its own thread the moment it does.
        await close_turn(
            ctx, turn, messages=messages,
            assistant_text=resp.text or "(handed back to the main assistant)",
        )
        await ctx.peers.delegate(
            ORCHESTRATOR_AGENT_ID, args, mode="end_delegation"
        )
        return AgentOutput()

    if config.on_extra_terminal is not None:
        extra_names = {t.name for t in config.extra_terminal}
        extra_call = next(
            (tc for tc in resp.tool_calls if tc.name in extra_names), None
        )
        if extra_call is not None:
            return await config.on_extra_terminal(ctx, extra_call, settings)

    await close_turn(
        ctx, turn, messages=messages, assistant_text=resp.text
    )
    return text_output(resp.text, files=outbound, context_tokens=context_tokens)


def _seed_text(seed: LLMData) -> str:
    """The delegate's own opening row, composed from the hand-off payload.

    The orchestrator used to write this row into the delegate's thread and
    roll it back if the reassignment failed. Composing it here removes that
    window entirely — there is nothing to roll back, because nothing is
    written until the delegate is running."""
    text = f"## Delegated task\n{seed.agent_instruction or seed.prompt}"
    if seed.context:
        text += f"\n\n## Context\n{seed.context}"
    if seed.prompt:
        text += f"\n\n## User request\n{seed.prompt}"
    return text
