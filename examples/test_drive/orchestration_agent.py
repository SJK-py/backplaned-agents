"""Test-drive: orchestration agent demonstrating the unified
mode-dispatch model.

The classic conversation-orchestrator pattern: a channel agent
(webapp BFF, Telegram bot, etc.) forwards user input here, and
this agent decides what to do with it.

There are THREE registered modes — one unified registry, keyed by
explicit mode name (the payload model's class name by default):

  * `UserMessage` — data-plane. A conventional user prompt the
    agent runs through its LLM loop. `tool=True` (default), so it
    IS advertised by `build_tools` as an LLM-callable tool.
  * `ClearHistory`, `SetPersona` — control-plane. Registered with
    `@agent.handler(tool=False)`, which lists their mode in
    `AgentInfo.non_tool_modes`: still normal modes the router
    validates and dispatches, just NOT advertised to tool-using
    models, so an LLM picking `orchestration_agent` can never
    accidentally invoke "clear history".

There is no separate control/delegation registry and no
`is_control` flag — the caller names the mode explicitly. Routing
is an O(1) mode lookup, never a structural guess at the payload
shape.

Channel agent's typical caller-side wiring:

    if user_text.startswith("/clear"):
        await ctx.peers.spawn(
            "orchestration_agent", ClearHistory(), mode="ClearHistory",
        )
    elif user_text.startswith("/persona "):
        await ctx.peers.spawn(
            "orchestration_agent",
            SetPersona(persona=user_text[9:]),
            mode="SetPersona",
        )
    else:
        # Sole data-plane caller: mode=None resolves to UserMessage
        # (it's the only tool-visible mode), but naming it is clearer.
        await ctx.peers.spawn(
            "orchestration_agent", UserMessage(prompt=user_text),
            mode="UserMessage",
        )

Run after the router is up:

    AGENT_INVITATION_TOKEN=<token> \\
    AGENT_ROUTER_URL=ws://127.0.0.1:8000/v1/agent \\
    AGENT_STATE_DIR=/tmp/orchestration-agent-state \\
        python examples/test_drive/orchestration_agent.py
"""

from __future__ import annotations

from pydantic import BaseModel

from bp_protocol.types import AgentInfo, AgentOutput, TaskStatus
from bp_sdk import Agent, TaskContext

# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class UserMessage(BaseModel):
    """Data-plane: a normal user prompt. Goes through the LLM loop."""

    prompt: str


class ClearHistory(BaseModel):
    """Control-plane: wipe conversation state for the current session.

    `keep_metadata=True` preserves persona / config selections; set
    False for a full reset on session-end."""

    keep_metadata: bool = True


class SetPersona(BaseModel):
    """Control-plane: set the conversation persona on the session."""

    persona: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


agent = Agent(
    info=AgentInfo(
        agent_id="orchestration_agent",
        description=(
            "Test-drive orchestration agent — receives user messages "
            "on the data-plane and channel-side commands on the "
            "control-plane."
        ),
        groups=["test_drive"],
        capabilities=["conversation.orchestrate"],
    ),
)


# ---------------------------------------------------------------------------
# Data-plane: normal user message → spawn into the gemini_agent
# ---------------------------------------------------------------------------


@agent.handler
async def on_message(ctx: TaskContext, payload: UserMessage) -> AgentOutput:
    """Conventional agent loop. In a real deployment this would
    rebuild the conversation from session history, call the LLM
    with the user's prompt, and return the generated reply. Here
    we forward to `gemini_agent` for brevity; the round-trip is
    the same.

    `accepts_schema` is auto-derived as
    `{"UserMessage": <schema>, "ClearHistory": <schema>,
    "SetPersona": <schema>}`; `non_tool_modes` is
    `["ClearHistory", "SetPersona"]`, so `build_tools` advertises
    only the `UserMessage` mode as an LLM-callable tool."""
    ctx.log.info(
        "orchestration.message",
        extra={"event": "orchestration.message", "bp.session_id": ctx.session_id},
    )
    ctx.progress.status("dispatching")
    # spawn(wait=True) always returns a ResultFrame — branch on
    # `.status` (a TaskStatus), not a defensive hasattr.
    result = await ctx.peers.spawn("gemini_agent", payload, timeout_s=30.0)
    if result.status != TaskStatus.SUCCEEDED:
        ctx.metric("orchestration_downstream_failures", 1.0)
        return AgentOutput(
            content=f"gemini_agent returned status {result.status.value}",
        )
    text = (result.output.content if result.output else "") or ""
    return AgentOutput(content=text)


# ---------------------------------------------------------------------------
# Control-plane: slash commands / button clicks
# ---------------------------------------------------------------------------


@agent.handler(tool=False)
async def on_clear_history(
    ctx: TaskContext, payload: ClearHistory
) -> AgentOutput:
    """Channel-side request to wipe the conversation. In a real
    deployment this would walk session-scoped task history and
    drop it; here we just stamp session metadata so the action is
    visible to subsequent reads.

    `tool=False` lists mode `"ClearHistory"` in `non_tool_modes` —
    reachable via `peers.spawn(..., mode="ClearHistory")` but a
    sibling agent's LLM picking `orchestration_agent` as a tool
    sees only the `UserMessage` mode."""
    metadata_update = {
        "history_cleared_at": ctx.task_id,
        "history_keep_metadata": payload.keep_metadata,
    }
    ctx.log.info(
        "control.clear_history",
        extra={
            "event": "control.clear_history",
            "bp.session_id": ctx.session_id,
            "keep_metadata": payload.keep_metadata,
        },
    )
    # In a real implementation: await some session-scoped helper to
    # drop the conversation rows; if you have F4's
    # update_session_metadata wired into your handler, stamp the
    # event there too.
    return AgentOutput(
        content="history cleared",
        metadata=metadata_update,
    )


@agent.handler(tool=False)
async def on_set_persona(
    ctx: TaskContext, payload: SetPersona
) -> AgentOutput:
    """Channel-side request to change the conversation persona —
    e.g. user picks 'concise' from a dropdown."""
    ctx.log.info(
        "control.set_persona",
        extra={
            "event": "control.set_persona",
            "bp.session_id": ctx.session_id,
            "persona": payload.persona,
        },
    )
    return AgentOutput(
        content=f"persona set to {payload.persona!r}",
        metadata={"persona": payload.persona},
    )


if __name__ == "__main__":
    agent.run()
