"""history_summarizer agent — rolling conversation summarization.

Read-only over the router's session store: it reads the target thread (and
that thread's current rolling summary), folds them into one updated summary,
and returns the text. **It never applies it.** Only the thread's owner can
move its own floor, and a summary that isn't applied together with the floor
is either double-counted or lost — so the caller applies both in one batch:

  * the owner itself, at the start of an oversized turn
    (`bp_agents.common.thread.maybe_fold`);
  * the channel, which uses the text as a delegation seed or hand-back recap
    and applies nothing at all.

Reading another agent's thread is a first-class read (`owner_agent_id` is a
parameter on `Read` and `GetState`); writing one is unrepresentable. That
asymmetry is exactly what lets this agent be useful without being trusted.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from bp_agents import slots
from bp_agents.common import text_output
from bp_agents.common.thread import CONTEXT_ROLES, SUMMARY_KEY
from bp_protocol.frames import SessionMessage
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext

logger = logging.getLogger(__name__)

HISTORY_SUMMARIZER_AGENT_ID = "history_summarizer"

_SYSTEM = """\
You are a conversation summarizer. Produce a concise, faithful summary of \
the conversation below, written so an assistant can use it as background \
context for continuing the conversation. Preserve key facts, decisions, \
the user's stated preferences, and any open threads. Drop small talk and \
redundant detail. If a previous summary is provided, integrate it rather \
than repeating it. Output only the summary text.\
"""


class SummarizeThread(BaseModel):
    agent_id: str
    """Whose thread to summarize — the orchestrator's, or a delegate's."""
    up_to: int | None = None
    """Fold only messages with `id <= up_to`. `None` summarizes the whole
    active window, which is what a delegation switch wants."""


class NameSession(BaseModel):
    user_prompt: str
    """The first user message; the title is generated from it."""


_NAME_SYSTEM = """\
You name conversations. Given the user's first message, reply with a short, \
specific title (3–6 words) that captures the topic — like a chat sidebar \
label. No quotes, no trailing punctuation, no preamble. Output only the \
title.\
"""

# A generated title is a UI label, not prose — keep it short and single-line.
_NAME_MAX_LEN = 60


def _clean_title(raw: str) -> str:
    """First line, stripped of surrounding quotes/whitespace, length-capped."""
    title = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    title = title.strip("\"'“”").strip()
    if len(title) > _NAME_MAX_LEN:
        title = title[:_NAME_MAX_LEN].rstrip()
    return title


agent = Agent(
    info=AgentInfo(
        agent_id=HISTORY_SUMMARIZER_AGENT_ID,
        description="Rolling conversation summarizer (read-only).",
        groups=["l3"],
        capabilities=["llm.generation.text", "summarize.history", "session.history"],
        hidden=True,
    ),
)


def _transcript(rows: list[SessionMessage]) -> str:
    return "\n".join(f"{r.role}: {r.content}" for r in rows)


async def run_summarize_thread(
    ctx: TaskContext, payload: SummarizeThread
) -> AgentOutput:
    """Read `agent_id`'s thread and its rolling summary, return the fold."""
    batch = ctx.history.batch()
    state = batch.get_state(SUMMARY_KEY, owner=payload.agent_id)
    read = batch.read(
        owner=payload.agent_id,
        roles=CONTEXT_ROLES,
        # `before_id` is exclusive, and `up_to` is the last id to fold.
        before_id=payload.up_to + 1 if payload.up_to is not None else None,
    )
    await batch.send()

    previous_entry = state.state.get(SUMMARY_KEY)
    previous = previous_entry.value if previous_entry else None
    rows = read.messages
    if not rows:
        # Nothing to fold — preserve the existing summary unchanged rather
        # than returning empty, which the caller would store as "no summary".
        return text_output(previous or "")

    user_parts: list[str] = []
    if previous:
        user_parts.append(f"## Previous summary\n{previous}")
    user_parts.append(f"## Conversation\n{_transcript(rows)}")
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content="\n\n".join(user_parts)),
    ]
    resp = await ctx.llm.generate(messages, slot=slots.LITE)
    return text_output(resp.text)


async def run_name_session(ctx: TaskContext, payload: NameSession) -> AgentOutput:
    messages = [
        Message(role="system", content=_NAME_SYSTEM),
        Message(role="user", content=payload.user_prompt),
    ]
    resp = await ctx.llm.generate(messages, slot=slots.LITE)
    return text_output(_clean_title(resp.text))


@agent.handler(
    mode="session_name", tool=False,
    description="Generate a short conversation title from the first user "
    "message (channel-driven, lite LLM).",
)
async def session_name(ctx: TaskContext, payload: NameSession) -> AgentOutput:
    return await run_name_session(ctx, payload)


@agent.handler(
    mode="summarize_thread", tool=False,
    description="Fold a thread (optionally only up to a cutoff id) and its "
    "rolling summary into one updated summary. Returns the text; the caller "
    "applies it.",
)
async def summarize_thread(
    ctx: TaskContext, payload: SummarizeThread
) -> AgentOutput:
    return await run_summarize_thread(ctx, payload)


if __name__ == "__main__":
    agent.run()
