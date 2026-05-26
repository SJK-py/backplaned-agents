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

import logging
from typing import TYPE_CHECKING

from bp_agents.agents.orchestrator.prompts import GENERAL_INSTRUCTION
from bp_agents.common import (
    LocalToolset,
    compose_system_prompt,
    estimate_context_tokens,
    make_current_time_tool,
    run_llm_loop,
    text_output,
    user_config_note,
)
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT_ID = "orchestrator"


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

    local_tools = LocalToolset([make_current_time_tool(timezone)])
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset, local_tools=local_tools
    )

    async with pool.acquire() as conn:
        await queries.append_history(
            conn,
            session_id=ctx.session_id,
            agent_id=ORCHESTRATOR_AGENT_ID,
            role="assistant",
            message=resp.text,
        )

    return text_output(resp.text, context_tokens=context_tokens)


@agent.handler(mode="message", tool=False)
async def message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None, "orchestrator pool not initialised (on_startup)"
    return await run_orchestrator_message(
        ctx, payload, pool=_pool, settings=_settings
    )


if __name__ == "__main__":
    agent.run()
