"""bp_agents.common.tool_history — recall a turn's tool exchanges on demand.

The live loop (`run_llm_loop`) keeps each turn's `tool_call`/`tool_result`
sequence in memory and feeds it to the model, but a reloaded context carries
only `CONTEXT_ROLES` (`user` / `assistant`) to stay bounded ([sessions.md]
§2.1). That makes detail from a *prior* turn's tool result unrecoverable
unless the model carried it into its self-contained answer.

Two halves close that gap ([agent-tool-history-recall.md]):

  - the write side lives in `bp_agents.common.thread.close_turn`, which
    appends each exchange as two hidden rows on the agent's own thread —
    part of the same batch as the assistant row, so a turn is persisted
    atomically or not at all.
  - `make_recall_tool_history_tool` — a local tool the model calls to page
    back through its own thread's past exchanges (`count` + `skip`), with
    hard caps so recall can never re-bloat context.

Recall reads with `include_retired=True`: a summarization fold moves the
thread's floor past old turns, and their tool detail is *exactly* what the
model needs recall for once the prose has been compressed away.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bp_agents.common.tools import LocalTool
from bp_sdk import ToolSpec

if TYPE_CHECKING:
    from bp_protocol.frames import SessionMessage
    from bp_sdk import Message, TaskContext

# Caps — the reason count/skip recall is safe. Worst case returned text is
# bounded by `min(count, MAX_RECALL) * PER_RESULT_CHARS`, capped again by
# `TOTAL_RECALL_CHARS`.
MAX_RECALL = 10
PER_RESULT_CHARS = 2_000
PER_ARGS_CHARS = 300
TOTAL_RECALL_CHARS = 8_000

# The recall tool is itself a tool, so its own exchange gets persisted like
# any other. Persisting the FULL digest would echo results the model already
# pulled — a later recall would re-surface them, duplicated and bloated. So a
# recall exchange is stored as a short marker (`_recall_marker`) instead of
# its rendered output: the breadcrumb survives, the duplication doesn't.
RECALL_TOOL_NAME = "recall_tool_history"
_RECALL_LABEL_RE = re.compile(r"\[\d+ exchanges? back\]")


@dataclass
class ToolExchange:
    """One dispatched tool call + its result, lifted from a finished
    loop's `messages` for persistence."""

    name: str
    args: dict[str, Any]
    result: str


def _result_text(content: str | list[dict[str, Any]]) -> str:
    """Flatten a tool-response `content` into storable text. A plain
    string passes through; a multimodal list keeps its text parts and
    notes each `file_ref` by name (the bytes stay in the stash, re-openable
    with `read_file`) so a recalled file result is a usable pointer, not a
    dead serialization."""
    if isinstance(content, str):
        return content
    pieces: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            pieces.append(part["text"])
        ref = part.get("file_ref")
        if isinstance(ref, dict) and ref.get("name"):
            pieces.append(f"[file result: {ref['name']} — re-open with read_file]")
    return "\n".join(p for p in pieces if p)


def extract_tool_exchanges(messages: list[Message]) -> list[ToolExchange]:
    """Pull the dispatched (call → result) exchanges out of a finished
    loop's `messages`, in order.

    Only the CURRENT turn's exchanges are present: reloaded history rows
    are plain text `assistant`/`user` messages (no `function_call` parts)
    and carry no `role="tool"` rows, so scanning the whole list is safe.
    A terminal tool (`hand_off` / `end_delegation`) is never dispatched and
    has no `tool` message, so it naturally falls out — every extracted
    exchange has a real result."""
    # tool_call_id -> (name, args), harvested from assistant function_call parts.
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for msg in messages:
        if msg.role != "assistant" or not isinstance(msg.content, list):
            continue
        for part in msg.content:
            if not isinstance(part, dict):
                continue
            fc = part.get("function_call")
            if isinstance(fc, dict) and fc.get("id"):
                calls[fc["id"]] = (fc.get("name") or "", fc.get("args") or {})

    exchanges: list[ToolExchange] = []
    for msg in messages:
        if msg.role != "tool" or not msg.tool_call_id:
            continue
        name, args = calls.get(msg.tool_call_id, (msg.name or "", {}))
        exchanges.append(
            ToolExchange(
                name=name or msg.name or "tool",
                args=args,
                result=_result_text(msg.content),
            )
        )
    return exchanges


def _recall_marker(result: str) -> str:
    """The stored result for a `recall_tool_history` exchange — a short
    breadcrumb instead of the rendered digest, so a later recall doesn't
    re-surface (duplicated, bloated) the results this call already pulled.
    Counts the entries actually returned from the digest's `[N back]`
    labels."""
    n = len(_RECALL_LABEL_RE.findall(result))
    if n == 0:
        return "(recalled earlier tool history — nothing matched.)"
    return (
        f"(recalled {n} earlier tool exchange{'s' if n != 1 else ''} — "
        "content was shown in that turn's context, not re-stored here.)"
    )


def storable_result(ex: ToolExchange) -> str:
    """What goes in the `tool_result` row: the recall tool's own output is
    replaced by a marker (see `_recall_marker`); every other tool stores
    its real result."""
    if ex.name == RECALL_TOOL_NAME:
        return _recall_marker(ex.result)
    return ex.result


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} more chars)"


def _render_call(call_message: str) -> str:
    """`name(args)` from a stored `tool_call` row, args compacted +
    truncated. Falls back to the raw message if it isn't the expected
    JSON."""
    try:
        payload = json.loads(call_message)
        name = payload.get("name") or "tool"
        args = payload.get("args") or {}
    except (json.JSONDecodeError, AttributeError):
        return call_message
    args_str = json.dumps(args, default=str, ensure_ascii=False) if args else ""
    return f"{name}({_truncate(args_str, PER_ARGS_CHARS)})"


def render_recall(
    exchanges: list[tuple[SessionMessage, SessionMessage]], *, skip: int
) -> str:
    """Render a recalled page (ascending, newest-last) into a compact text
    digest. Each entry is labelled by its absolute distance back from now
    (`skip`-aware) so the model can compute the next `skip`. Per-result and
    total-size caps keep the digest from re-bloating context; when the
    total budget trims older entries, a leading note says how many."""
    n = len(exchanges)
    entries: list[str] = []
    for i, (call_row, result_row) in enumerate(exchanges):
        back = skip + (n - i)  # newest returned = skip+1 back
        label = f"[{back} exchange{'s' if back != 1 else ''} back]"
        head = _render_call(call_row.content)
        body = _truncate(result_row.content or "(empty result)", PER_RESULT_CHARS)
        entries.append(f"{label} {head} →\n{body}")

    # Total-budget trim: drop oldest (top) entries until under budget.
    dropped = 0
    while len(entries) > 1 and sum(len(e) for e in entries) > TOTAL_RECALL_CHARS:
        entries.pop(0)
        dropped += 1

    note = (
        f"({dropped} older exchange(s) omitted to stay within the recall "
        "size budget — narrow with a smaller count or a larger skip.)\n\n"
        if dropped
        else ""
    )
    return note + "\n\n".join(entries)


def _pair_exchanges(
    rows: list[SessionMessage], *, limit: int, skip: int
) -> list[tuple[SessionMessage, SessionMessage]]:
    """Greedy-pair a `tool_call` with the `tool_result` that follows it,
    then take the requested page.

    Pairing on whole exchanges (rather than on rows) is what keeps `skip` /
    `count` counting the unit the model reasons about, and stops a page
    splitting a call from its result. An unpaired result at the window's
    oldest edge is a boundary orphan and is dropped — the caller over-fetches
    so the requested page is still whole."""
    exchanges: list[tuple[SessionMessage, SessionMessage]] = []
    pending: SessionMessage | None = None
    for row in rows:
        if row.role == "tool_call":
            pending = row
        elif row.role == "tool_result" and pending is not None:
            exchanges.append((pending, row))
            pending = None
    if skip:
        exchanges = exchanges[: max(0, len(exchanges) - skip)]
    return exchanges[-limit:] if limit > 0 else []


def make_recall_tool_history_tool(*, agent_id: str) -> LocalTool:
    """A local tool letting an agent re-read its OWN thread's past tool
    exchanges — detail the context reload dropped. `count` exchanges
    starting `skip` back from the most recent; paging with `skip` lets the
    model walk older exchanges without re-receiving ones it has already
    seen.

    Scoping is structural rather than checked: a `Read` with no owner
    defaults to the caller's own thread, and the router derives that from
    the task's active executor. There is no parameter through which this
    could name another agent's thread or another session's."""

    async def _handler(ctx: TaskContext, args: dict[str, Any]) -> str:
        try:
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, MAX_RECALL))
        try:
            skip = int(args.get("skip", 0))
        except (TypeError, ValueError):
            skip = 0
        skip = max(0, skip)

        # Over-fetch by a small margin past `2*(skip+count)` rows so a stray
        # row on the truncation boundary can't clip the requested page.
        rows = await ctx.history.read(
            roles=["tool_call", "tool_result"],
            include_retired=True,
            limit=2 * (skip + count) + 4,
        )
        exchanges = _pair_exchanges(rows, limit=count, skip=skip)
        if not exchanges:
            if skip:
                return (
                    "No older tool history to recall — you've reached the "
                    "start of this conversation's tool calls."
                )
            return "No earlier tool calls in this conversation to recall."
        return render_recall(exchanges, skip=skip)

    return LocalTool(
        spec=ToolSpec(
            name=RECALL_TOOL_NAME,
            description=(
                "Re-read the FULL results of your own earlier tool calls in "
                "this conversation — detail that isn't kept in your visible "
                "context across turns. Use this when you need an exact value, "
                "URL, or row from a tool result you (or you on a previous "
                "turn) already obtained but no longer have in front of you. "
                "'count' is how many recent exchanges to return (newest "
                "first, max 10); 'skip' is how many of the most-recent "
                "exchanges to skip first (default 0) — start with count=1, "
                "and if the detail is further back, call again with skip=1, "
                "skip=2, … to step further back without repeating what you "
                "already saw. Each result is truncated if very large."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RECALL,
                        "description": "How many past exchanges to return "
                        "(newest first). Default 1.",
                    },
                    "skip": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "How many of the most-recent exchanges "
                        "to skip before returning. Default 0.",
                    },
                },
            },
        ),
        handler=_handler,
    )
