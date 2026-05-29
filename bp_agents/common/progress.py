"""bp_agents.common.progress — structured loop progress.

A `LoopProgress` rides in `ProgressFrame.metadata` ([data-model.md] §3);
the channel's verbose `on_progress` renders one message per frame
([channel.md] §5). `run_llm_loop` emits these as it iterates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from bp_sdk import TaskContext

# The metadata key the channel reads a LoopProgress payload off of.
LOOP_PROGRESS_KEY = "loop_progress"

LoopProgressKind = Literal[
    "thinking", "tool_call", "tool_result", "message", "status"
]


class LoopProgress(BaseModel):
    """One structured progress event from an agent's loop."""

    kind: LoopProgressKind
    round: int | None = None
    """1-indexed loop iteration, when applicable."""
    tool: str | None = None
    """Tool name, for `tool_call` / `tool_result`."""
    detail: str | None = None
    """Short human-readable detail (rendered verbatim in verbose mode)."""


async def emit_loop_progress(
    ctx: TaskContext,
    *,
    kind: LoopProgressKind,
    round: int | None = None,  # noqa: A002 — mirrors the LoopProgress field
    tool: str | None = None,
    detail: str | None = None,
) -> None:
    """Emit a structured `LoopProgress` frame. Best-effort (the SDK's
    `ProgressEmitter.emit` swallows transport errors); the structured
    payload lands under `metadata[LOOP_PROGRESS_KEY]`, with `detail`
    also set as the frame's `content` for non-structured consumers."""
    lp = LoopProgress(kind=kind, round=round, tool=tool, detail=detail)
    await ctx.progress.emit(
        kind, content=detail or "", **{LOOP_PROGRESS_KEY: lp.model_dump()}
    )


# metadata key carrying the ORIGINAL producing agent_id through forwarding.
# A forwarded frame's own `agent_id` is the relay (the parent), so the channel
# reads this to tag the line with the specialist that actually ran the step.
PROGRESS_PRODUCER_KEY = "progress_producer"

# Subagent progress kinds bubbled up to the user — the *actions*, not the
# subagent's `thinking` heartbeats (the parent's umbrella `tool_call` already
# signals "the specialist is working").
_FORWARDED_KINDS = frozenset({"tool_call", "tool_result"})


async def relay_subagent_progress(ctx: TaskContext, child_frame: Any) -> None:
    """Re-emit a subagent's `ProgressFrame` on the PARENT's task so it bubbles
    up to the root subscriber (the channel) in verbose mode.

    Forwards only action frames (`tool_call` / `tool_result`); preserves the
    original producing agent in `metadata[PROGRESS_PRODUCER_KEY]` so the
    channel tags it (the re-emitted frame's `agent_id` is the relay). The
    producer is taken from the child's existing marker when present, so the
    original specialist survives arbitrary nesting. Best-effort — the emit
    swallows transport errors."""
    meta = getattr(child_frame, "metadata", None) or {}
    lp = meta.get(LOOP_PROGRESS_KEY)
    if not lp or lp.get("kind") not in _FORWARDED_KINDS:
        return
    producer = meta.get(PROGRESS_PRODUCER_KEY) or getattr(child_frame, "agent_id", None)
    await ctx.progress.emit(
        getattr(child_frame, "event", "") or "tool_call",
        content=getattr(child_frame, "content", "") or "",
        **{LOOP_PROGRESS_KEY: lp, PROGRESS_PRODUCER_KEY: producer},
    )
