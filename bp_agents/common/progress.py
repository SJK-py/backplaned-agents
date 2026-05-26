"""bp_agents.common.progress — structured loop progress.

A `LoopProgress` rides in `ProgressFrame.metadata` ([data-model.md] §3);
the channel's verbose `on_progress` renders one message per frame
([channel.md] §5). `run_llm_loop` emits these as it iterates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

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
