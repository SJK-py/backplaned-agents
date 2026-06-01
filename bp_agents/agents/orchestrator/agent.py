"""orchestrator agent — `message` mode (Phase 1).

The channel dispatches a user turn here as a root task
(`spawn_root_for_user`). The orchestrator rebuilds its context from
session history, runs the shared LLM loop, appends its assistant turn,
and returns an `AgentOutput` carrying `context_tokens` for the channel's
summarization check.

Write ownership ([sessions.md] §2): the **channel** is the sole writer
of `user` turns and writes the inbound turn *before* dispatching, so the
incumbent reload already contains it. The orchestrator writes only its
own `assistant` turn.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bp_agents.agents.l1_common import compose_subagent_system
from bp_agents.agents.orchestrator.prompts import CRON_INSTRUCTION, GENERAL_INSTRUCTION
from bp_agents.common import (
    LocalToolset,
    compose_system_prompt,
    estimate_context_tokens,
    make_current_time_tool,
    make_send_file_tool,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, Message, TaskContext, ToolSpec
from bp_sdk.peers import PeerCallError

if TYPE_CHECKING:
    import asyncpg

    from bp_sdk import ToolCall

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT_ID = "orchestrator"
_HAND_OFF_TOOL = "hand_off"


def _l1_destinations(ctx: TaskContext) -> list[str]:
    """Visible l1 specialist agent_ids the orchestrator may hand off to."""
    return [
        aid
        for aid, entry in ctx.peers.visible().items()
        if "l1" in (entry.get("groups") or [])
    ]


def _hand_off_spec(destinations: list[str]) -> ToolSpec:
    return ToolSpec(
        name=_HAND_OFF_TOOL,
        description=(
            "Hand this conversation off to a specialist for a SUSTAINED, "
            "multi-turn effort: the specialist takes over and talks to the "
            "user directly across several turns, then hands control back "
            "when finished. Use only when the work clearly spans more than "
            "one exchange. For a one-shot task you can resolve in a single "
            "step, call that specialist's own tool (`call_<specialist>`) "
            "and continue the conversation yourself — do NOT hand off."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "enum": destinations},
                "instruction": {
                    "type": "string",
                    "description": "What the specialist should accomplish.",
                },
                "context": {
                    "type": "string",
                    "description": "Relevant background for the specialist.",
                },
            },
            "required": ["agent_id", "instruction"],
        },
    )


agent = Agent(
    info=AgentInfo(
        agent_id=ORCHESTRATOR_AGENT_ID,
        description="Personal assistant; runs the main conversation loop.",
        groups=["l0"],
        capabilities=[
            "agent.orchestration",
            "agent.delegation",
            "llm.generation.text",
            "assistant.personal",
            "assistant.general",
            "file.full",
            "session.history",
        ],
        hidden=True,
    ),
)

# Suite resources, wired on startup. Module-level so the handler can
# reach them; the testable core takes them as explicit args instead.
_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None


@agent.on_startup
async def _startup() -> None:
    global _pool  # noqa: PLW0603 — module-level handle wired once at startup
    _pool = await open_pool(_settings)


@agent.on_shutdown
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


async def run_orchestrator_message(
    ctx: TaskContext,
    payload: MessagePayload,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """Core of the `message` turn — testable without the SDK run loop.

    Reload the orchestrator thread's incumbent history, build the system
    prompt from user-config + rolling summary, run the tool-calling loop,
    persist the assistant turn, and return the result with measured
    `context_tokens`.
    """
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
        rows = await queries.reload_incumbent(
            conn, session_id=ctx.session_id, agent_id=ORCHESTRATOR_AGENT_ID
        )
        info = await queries.get_session_info(conn, ctx.session_id)

    summary = info.history_summary if info else None
    preset = cfg.preset_balanced if cfg else settings.default_preset_balanced
    timezone = cfg.timezone if cfg else settings.default_timezone
    config_note = user_config_note(cfg) if cfg else ""

    system_prompt = compose_system_prompt(
        GENERAL_INSTRUCTION, config_note=config_note, summary=summary
    )
    messages: list[Message] = [Message(role="system", content=system_prompt)]
    messages.extend(Message(role=r.role, content=r.message) for r in rows)
    # The channel writes the user turn before dispatch, so it's normally
    # the last reloaded row. Fall back to the payload when it isn't (the
    # turn raced behind us, or this orchestrator was invoked directly) so
    # the current input is never dropped.
    if not rows or rows[-1].role != "user":
        messages.append(Message(role="user", content=payload.prompt))

    # Measure the built context before the loop appends assistant/tool
    # turns — this is the channel's summarization signal ([sessions.md] §3).
    context_tokens = estimate_context_tokens(messages)

    outbound: list[str] = []
    local_tools = LocalToolset(
        [make_current_time_tool(timezone), make_send_file_tool(outbound)]
    )
    destinations = _l1_destinations(ctx)
    extra = [_hand_off_spec(destinations)] if destinations else []
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset, local_tools=local_tools,
        extra_tools=extra, terminal_tools={_HAND_OFF_TOOL} if extra else None,
        file_tools="full", detail_chars=settings.verbose_detail_chars,
    )

    hand_off = next(
        (tc for tc in resp.tool_calls if tc.name == _HAND_OFF_TOOL), None
    )
    if hand_off is not None and hand_off.args.get("agent_id") in destinations:
        try:
            await _do_hand_off(ctx, pool, payload.prompt, hand_off.args)
        except PeerCallError as exc:
            # F1 ([delegation.md] §4): the delegate admit failed (rejected /
            # ack-timeout / disconnected), so the task was NOT reassigned and
            # this orchestrator is still the active executor. Don't let the
            # raise surface as a generic dispatch error — answer the turn
            # directly and produce a real fallback Result.
            logger.warning(
                "hand_off_failed_fallback",
                extra={"event": "hand_off_failed", "dest": hand_off.args.get("agent_id"),
                       "reason": str(exc)},
            )
            return await _run_hand_off_fallback(
                ctx, pool, messages, hand_off, preset=preset,
                local_tools=local_tools, outbound=outbound,
                context_tokens=context_tokens,
            )
        # The delegate produces the terminal Result; the router drops
        # this (now non-active) agent's Result.
        return AgentOutput()

    async with pool.acquire() as conn:
        await queries.append_history(
            conn,
            session_id=ctx.session_id,
            agent_id=ORCHESTRATOR_AGENT_ID,
            role="assistant",
            message=resp.text,
        )

    return text_output(resp.text, files=outbound, context_tokens=context_tokens)


async def _do_hand_off(
    ctx: TaskContext, pool: asyncpg.Pool, user_prompt: str, args: dict
) -> None:
    """Phase 1 of delegation ([delegation.md]): write the `delegate_prompt`
    seed row into the delegate's thread, then reassign the task via
    `delegate(mode=on_delegation)`."""
    dest = args["agent_id"]
    instruction = args.get("instruction", "")
    context = args.get("context") or ""
    seed = f"## Delegated task\n{instruction}"
    if context:
        seed += f"\n\n## Context\n{context}"
    seed += f"\n\n## User request\n{user_prompt}"
    async with pool.acquire() as conn:
        seed_id = await queries.append_history(
            conn, session_id=ctx.session_id, agent_id=dest,
            role="user", message=seed, incumbent=True,
        )
    try:
        await ctx.peers.delegate(
            dest,
            LLMData(prompt=user_prompt, agent_instruction=instruction, context=context),
            mode="on_delegation",
        )
    except PeerCallError:
        # The reassignment never happened, so the seed row we just wrote is
        # an orphan incumbent on a thread that won't run. Retire it before
        # propagating so the delegate thread isn't polluted (F1 fallback
        # routes the turn back through the orchestrator).
        async with pool.acquire() as conn:
            await queries.demote_incumbent_through(
                conn, session_id=ctx.session_id, agent_id=dest, up_to_id=seed_id
            )
        raise

    # Reassignment succeeded. Close the orchestrator's open user turn with a
    # hidden `assistant` marker that the work was DELEGATED — not done by the
    # orchestrator. This keeps the reloaded thread alternating AND frames the
    # specialist's later results (a hidden `user` recap on hand-back) as
    # external input, so the model can't narrate them as its own work.
    async with pool.acquire() as conn:
        await queries.append_history(
            conn, session_id=ctx.session_id, agent_id=ORCHESTRATOR_AGENT_ID,
            role="assistant", message=f"Delegated to {dest}.",
            incumbent=True, hidden=True,
        )


async def _run_hand_off_fallback(
    ctx: TaskContext,
    pool: asyncpg.Pool,
    messages: list[Message],
    hand_off: ToolCall,
    *,
    preset: str | None,
    local_tools: LocalToolset,
    outbound: list[str],
    context_tokens: int,
) -> AgentOutput:
    """F1 fallback: the elected hand-off couldn't be admitted, so answer the
    turn directly. The loop left a dangling `hand_off` tool_call (it's a
    terminal tool, so no tool-response was appended); satisfy it with an
    error response, then re-run the loop with no hand-off tool so the model
    answers inline instead of trying to delegate again. Keep the same local
    tools (incl. `send_file`) so the fallback can still deliver files."""
    messages.append(Message.tool_response(
        tool_call_id=hand_off.id,
        name=_HAND_OFF_TOOL,
        response="The specialist is unavailable right now. Answer the user directly.",
    ))
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset, local_tools=local_tools,
        file_tools="full",
    )
    async with pool.acquire() as conn:
        await queries.append_history(
            conn,
            session_id=ctx.session_id,
            agent_id=ORCHESTRATOR_AGENT_ID,
            role="assistant",
            message=resp.text,
        )
    return text_output(resp.text, files=outbound, context_tokens=context_tokens)


async def run_orchestrator_subagent(
    ctx: TaskContext,
    payload: LLMData,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """Generic subagent execution (e.g. deep_reasoning's execute_step).
    Stateless — no session history; full toolset."""
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_balanced if cfg else settings.default_preset_balanced
    timezone = cfg.timezone if cfg else settings.default_timezone
    messages = [
        Message(
            role="system",
            content=compose_subagent_system(GENERAL_INSTRUCTION, payload),
        ),
        Message(role="user", content=payload.prompt),
    ]
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset,
        local_tools=LocalToolset([make_current_time_tool(timezone)]),
        file_tools="full",
    )
    return text_output(resp.text)


async def run_orchestrator_end_delegation(
    ctx: TaskContext,
    payload: dict,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """Phase 3 of delegation: the hand-back target. Append a recap to the
    main thread, retire the delegate episode, and optionally continue the
    loop on a follow-up prompt."""
    delegate = ctx.delegating_agent_id or "a specialist"
    summary = payload.get("delegation_summary", "")
    reason = payload.get("exit_reason", "")
    user_prompt = payload.get("user_prompt")
    recap = f"[Returned from {delegate}] {summary} (reason: {reason})"
    async with pool.acquire() as conn:
        # The recap is `user`-role (hidden): the specialist's results return as
        # EXTERNAL input, not the orchestrator's own work — so the model can't
        # later claim them as such. A hidden `assistant` ack then closes this
        # turn so the reloaded thread alternates (the pre-delegation user turn
        # was already closed by the hand-off marker).
        await queries.append_history(
            conn, session_id=ctx.session_id, agent_id=ORCHESTRATOR_AGENT_ID,
            role="user", message=recap, incumbent=True, hidden=True,
        )
        await queries.append_history(
            conn, session_id=ctx.session_id, agent_id=ORCHESTRATOR_AGENT_ID,
            role="assistant", message="Acknowledged.", incumbent=True, hidden=True,
        )
        # Retire the whole delegate episode (incl. the seed row).
        if ctx.delegating_agent_id:
            await queries.demote_thread(
                conn, session_id=ctx.session_id, agent_id=ctx.delegating_agent_id
            )
    if user_prompt:
        async with pool.acquire() as conn:
            await queries.append_history(
                conn, session_id=ctx.session_id, agent_id=ORCHESTRATOR_AGENT_ID,
                role="user", message=user_prompt,
            )
        return await run_orchestrator_message(
            ctx, MessagePayload(prompt=user_prompt), pool=pool, settings=settings
        )
    return text_output("")


@agent.handler(
    mode="message", tool=False,
    description="A live user turn from the channel — the main assistant "
    "loop (may hand off to a specialist).",
)
async def message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None, "orchestrator pool not initialised (on_startup)"
    return await run_orchestrator_message(
        ctx, payload, pool=_pool, settings=_settings
    )


_REPORT_DECISION = (
    "You just ran a scheduled task and produced the message below. Decide "
    "whether it is worth notifying the user right now. Return ONLY JSON: "
    '{"report": true|false, "reason": "<short reason>"}.'
)


async def run_orchestrator_cron_message(
    ctx: TaskContext,
    payload: MessagePayload,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """Scheduled run ([cron.md] §2): a FRESH context (cron instruction +
    user-config, no session history), full toolset, never delegates.
    Returns the message + a `{report, reason}` decision in metadata."""
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_balanced if cfg else settings.default_preset_balanced
    timezone = cfg.timezone if cfg else settings.default_timezone
    system = compose_system_prompt(
        CRON_INSTRUCTION, config_note=user_config_note(cfg) if cfg else "",
    )
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=payload.prompt),
    ]
    # Full toolset, but NO hand_off — a cron never delegates ([cron.md] §2).
    outbound: list[str] = []
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset,
        local_tools=LocalToolset(
            [make_current_time_tool(timezone), make_send_file_tool(outbound)]
        ),
        file_tools="full",
    )

    # Decide whether to report (the channel's apply step uses this for
    # case_by_case jobs).
    report, reason = True, ""
    try:
        decision = await ctx.llm.generate(
            [
                Message(role="system", content=_REPORT_DECISION),
                Message(role="user", content=resp.text or "(no output)"),
            ],
            preset=(cfg.preset_lite if cfg else settings.default_preset_lite),
        )
        parsed = json.loads(decision.text.strip().removeprefix("```json").strip("`").strip())
        report = bool(parsed.get("report", True))
        reason = str(parsed.get("reason", ""))
    except (json.JSONDecodeError, ValueError, KeyError, AttributeError):
        logger.debug("cron_report_decision_parse_failed", exc_info=True)

    return text_output(resp.text, files=outbound, report=report, reason=reason)


@agent.handler(
    mode="subagent", tool=False,
    description="Stateless one-shot execution with the full toolset and no "
    "session history (e.g. a deep_reasoning plan step).",
)
async def subagent(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_orchestrator_subagent(
        ctx, payload, pool=_pool, settings=_settings
    )


@agent.handler(
    mode="end_delegation", tool=False,
    description="Hand-back target: a specialist returns the conversation to "
    "the orchestrator (delegation lifecycle).",
)
async def end_delegation(ctx: TaskContext, payload: dict) -> AgentOutput:
    assert _pool is not None
    return await run_orchestrator_end_delegation(
        ctx, payload, pool=_pool, settings=_settings
    )


@agent.handler(
    mode="cron_message", tool=False,
    description="A scheduled run on the user's behalf — fresh context, never "
    "delegates; returns the message plus a {report, reason} decision.",
)
async def cron_message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_orchestrator_cron_message(
        ctx, payload, pool=_pool, settings=_settings
    )


if __name__ == "__main__":
    agent.run()
