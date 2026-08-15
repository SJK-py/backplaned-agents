"""bp_agents.common.thread — a stateful agent's own conversation thread.

Every agent that holds a conversation (the orchestrator, each l1 delegate)
runs the same three-beat turn against the router's session store
([../../docs/design/router-managed-session-store.md]):

    turn = await open_turn(ctx, agent_id, user_text=payload.prompt)
    turn = await maybe_fold(ctx, turn, system=…, limit_tokens=…)
    ...run the loop...
    await close_turn(ctx, turn, messages=…, assistant_text=…)

The store makes writing another agent's thread **unrepresentable** — an
`Append` carries no owner field; the router stamps the task's active
executor — so everything an agent needs from elsewhere arrives as a
hand-over item it materialises under its own authorship. `open_turn` is
where that happens, and `_materialise` is the entire vocabulary:

    seed     the channel's /delegate summary → a hidden `user` row
    recap    what a delegate did → a hidden `user` row (external input, so
             the model can't narrate it as its own work)
    retire   an episode is over → a floor on this thread, nothing rendered

**Summarization is the owner's, at the start of its own turn.** It has to
be the owner's, because only the owner can move its own floor. Doing it at
the start rather than the end is what makes it worth doing: the fold shrinks
*this* turn's context, which is the reason to fold at all. `maybe_fold` also
holds the ordering that matters — write the summary and move the floor in
ONE batch, or a crash between them either re-folds those turns into the
summary next pass (double-counting them) or retires turns whose content
never made the summary.

**One caveat about appending after a hand-off.** The router flips a task's
active executor to the delegate *before* delivering it, so an agent stops
being able to append to its own thread the moment `ctx.peers.delegate(...)`
is called. Everything an agent wants recorded must be written first. That is
a property of the store's authority model, not an ordering nicety.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bp_agents.common.output import estimate_context_tokens
from bp_sdk import Message

if TYPE_CHECKING:
    from bp_protocol.frames import HandoverItem, SessionMessage
    from bp_sdk import TaskContext
    from bp_sdk.history import SessionBatch

logger = logging.getLogger(__name__)

# The roles that make up a reloaded context. Tool rows live on the same
# thread but are excluded here — they exist for `recall_tool_history`, not
# for every subsequent turn's prompt.
CONTEXT_ROLES = ["user", "assistant"]
TOOL_ROLES = ["tool_call", "tool_result"]

# Thread-state key holding this thread's rolling summary. Thread state is
# owner-writable only, which is exactly the guarantee a summary needs.
SUMMARY_KEY = "summary"

SEED_ITEM = "seed"
RECAP_ITEM = "recap"
RETIRE_ITEM = "retire"

# Summarization tuning ([sessions.md] §3): fold the oldest ~70% of the active
# window once a thread crosses the soft limit, but only when there's a
# meaningful number of turns to compress.
_SUMMARIZE_FRACTION = 0.7
_MIN_ROWS_TO_SUMMARIZE = 6
HISTORY_SUMMARIZER_AGENT_ID = "history_summarizer"


@dataclass
class ThreadTurn:
    """This agent's thread as of the start of a turn."""

    agent_id: str
    rows: list[SessionMessage] = field(default_factory=list)
    summary: str | None = None
    # The token `close_turn` asserts on, so an append that raced another
    # writer is refused rather than silently interleaved.
    last_message_id: int = 0
    floor_id: int = 0

    def context(self) -> list[Message]:
        """The reloaded rows as loop messages, oldest first."""
        return [Message(role=r.role, content=r.content) for r in self.rows]

    def last_user_text(self) -> str | None:
        return next(
            (r.content for r in reversed(self.rows) if r.role == "user"), None
        )


def _materialise(batch: SessionBatch, item: HandoverItem) -> None:
    """Turn one hand-over item into this thread's own content.

    An unknown kind is dropped with a log rather than guessed at: the queue
    carries a suite vocabulary the router does not interpret, so a kind this
    agent doesn't know means suite version skew — not a message to
    improvise with."""
    payload = item.payload or {}
    if item.item_kind == RETIRE_ITEM:
        through = payload.get("through_id")
        if isinstance(through, int) and through > 0:
            batch.set_floor(through)
        return
    if item.item_kind in (SEED_ITEM, RECAP_ITEM):
        text = str(payload.get("text") or "").strip()
        if text:
            # Hidden `user`: the content came from outside this agent, and
            # `user` keeps the model from narrating it as its own work;
            # hidden keeps it out of the rendered transcript.
            batch.append("user", text, hidden=True)
        return
    logger.warning(
        "handover_kind_unknown",
        extra={"event": "handover_kind_unknown", "item_kind": item.item_kind},
    )


async def open_turn(
    ctx: TaskContext,
    agent_id: str,
    *,
    user_text: str | None = None,
    limit: int = 500,
) -> ThreadTurn:
    """Drain the hand-over queue, record the user's turn, and read back this
    thread's active window.

    Two round trips, always: what the queue holds decides what the second
    batch writes, and there is no way to learn that without asking. The
    ordering inside the second batch is the point — materialise, then the
    user row, then read — so what comes back is exactly what the model will
    see, in the order it was written.

    `user_text` is the current turn's message, taken from the task payload.
    The channel does not write it: the payload already carries the text, and
    the agent appending it under its own authorship is the whole property
    the store exists to guarantee."""
    drain = ctx.history.batch()
    items_handle = drain.consume_handovers()
    await drain.send()

    batch = ctx.history.batch()
    for item in items_handle.items:
        _materialise(batch, item)
    if user_text and user_text.strip():
        batch.append("user", user_text)
    state_handle = batch.get_state(SUMMARY_KEY)
    read_handle = batch.read(roles=CONTEXT_ROLES, limit=limit)
    stat_handle = batch.stat()
    await batch.send()

    summary_value = state_handle.state.get(SUMMARY_KEY)
    stat = stat_handle.stat
    return ThreadTurn(
        agent_id=agent_id,
        rows=read_handle.messages,
        summary=summary_value.value if summary_value else None,
        last_message_id=stat.last_message_id if stat else 0,
        floor_id=stat.floor_id if stat else 0,
    )


async def maybe_fold(
    ctx: TaskContext,
    turn: ThreadTurn,
    *,
    system: str,
    limit_tokens: int,
) -> ThreadTurn:
    """Fold the oldest ~70% of this thread into its rolling summary when the
    built context is over the user's soft limit.

    Best-effort in both directions: too few rows to be worth compressing, a
    summarizer that fails, or an empty summary all leave the turn untouched
    and the turn runs on the context it has. A failed fold must never cost
    the user their answer.

    The apply is one batch — `SetState(summary)` then `SetFloor(cutoff)` —
    because they are only correct together."""
    messages = [Message(role="system", content=system), *turn.context()]
    if estimate_context_tokens(messages) <= limit_tokens:
        return turn
    if len(turn.rows) < _MIN_ROWS_TO_SUMMARIZE:
        return turn

    cutoff_idx = max(1, int(len(turn.rows) * _SUMMARIZE_FRACTION))
    up_to = turn.rows[cutoff_idx - 1].id

    from bp_agents.agents.history_summarizer import SummarizeThread  # noqa: PLC0415

    try:
        result = await ctx.peers.spawn(
            HISTORY_SUMMARIZER_AGENT_ID,
            SummarizeThread(agent_id=turn.agent_id, up_to=up_to),
            mode="summarize_thread",
        )
    except Exception:  # noqa: BLE001 — a fold is never worth failing a turn
        logger.warning(
            "thread_fold_failed",
            extra={"event": "thread_fold_failed", "bp.agent_id": turn.agent_id},
            exc_info=True,
        )
        return turn
    new_summary = (result.output.content if result.output else "") or ""
    if not new_summary.strip():
        return turn

    batch = ctx.history.batch()
    batch.set_state(SUMMARY_KEY, new_summary)
    batch.set_floor(up_to)
    await batch.send()

    kept = [r for r in turn.rows if r.id > up_to]
    logger.info(
        "thread_folded",
        extra={
            "event": "thread_folded",
            "bp.agent_id": turn.agent_id,
            "folded": len(turn.rows) - len(kept),
            "kept": len(kept),
        },
    )
    return ThreadTurn(
        agent_id=turn.agent_id,
        rows=kept,
        summary=new_summary,
        last_message_id=turn.last_message_id,
        floor_id=up_to,
    )


def _tool_ops(batch: SessionBatch, messages: list[Message]) -> int:
    """Append this turn's tool exchanges — two hidden rows each, a
    `tool_call` carrying `{name, args}` and its `tool_result` — so a later
    turn can re-read them with `recall_tool_history`. They are never part of
    a reloaded context; `CONTEXT_ROLES` is what keeps them out."""
    from bp_agents.common.tool_history import (  # noqa: PLC0415
        extract_tool_exchanges,
        storable_result,
    )

    exchanges = extract_tool_exchanges(messages)
    for ex in exchanges:
        batch.append(
            "tool_call",
            json.dumps({"name": ex.name, "args": ex.args}, default=str),
            hidden=True,
        )
        batch.append("tool_result", storable_result(ex), hidden=True)
    return len(exchanges)


async def close_turn(
    ctx: TaskContext,
    turn: ThreadTurn,
    *,
    messages: list[Message],
    assistant_text: str,
    extra: list[tuple[str, str]] | None = None,
    assert_thread: bool = True,
) -> list[int]:
    """Persist the turn: its tool exchanges, then the assistant row. Returns
    the ids of the rows `extra` appended, newest last.

    One batch, guarded by `AssertThread` on the id this turn opened at — a
    write that raced another writer is refused `thread_conflict` rather than
    silently interleaved. The channel's turn lease should make that
    impossible; the assertion is what turns "should" into "did". Pass
    `assert_thread=False` on a second write within the same turn, where the
    opening id is deliberately stale.

    `extra` appends further hidden `(role, text)` rows after the assistant
    row — the hand-off marker the orchestrator needs written *before* it
    delegates itself out of being the active executor."""
    batch = ctx.history.batch()
    if assert_thread and turn.last_message_id:
        batch.assert_thread(turn.last_message_id)
    _tool_ops(batch, messages)
    batch.append("assistant", assistant_text or "")
    handles = [
        batch.append(role, text, hidden=True) for role, text in extra or []
    ]
    await batch.send()
    return [h.message_id for h in handles if h.message_id is not None]


async def redact_rows(ctx: TaskContext, *message_ids: int) -> None:
    """Blank rows on the caller's own thread, keeping their ids so floors and
    cursors stay valid. Used where a row turns out to be untrue after the
    fact — the hand-off marker for a delegation that was refused."""
    ids = [i for i in message_ids if i]
    if not ids:
        return
    batch = ctx.history.batch()
    batch.redact(*ids)
    await batch.send()


async def append_rows(
    ctx: TaskContext, rows: list[tuple[str, str]], *, hidden: bool = True
) -> None:
    """Append `(role, text)` rows to the caller's own thread in one batch.

    For the paths that write without having opened a turn — the hand-off
    marker, the `end_delegation` recap — where there is no read to assert
    against."""
    batch = ctx.history.batch()
    for role, text in rows:
        if text:
            batch.append(role, text, hidden=hidden)
    await batch.send()


async def context_tokens_of(system: str, turn: ThreadTurn) -> int:
    """The turn's measured context size, for the metadata a frontend logs."""
    return estimate_context_tokens(
        [Message(role="system", content=system), *turn.context()]
    )
