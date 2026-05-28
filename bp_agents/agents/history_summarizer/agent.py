"""history_summarizer agent — rolling conversation summarization.

Read-only over `session_history`. Builds a transcript of the cutoff
window, folds in the previous summary, and returns an updated summary.
The channel applies it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bp_agents.common import text_output
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.db.models import SessionHistoryRow

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


class SummarizeIncumbent(BaseModel):
    agent_id: str
    up_to: int
    """Cutoff `session_history.id`; rows with `id <= up_to` are folded."""
    previous_summary: str | None = None


class SummarizeAll(BaseModel):
    agent_id: str
    summarize_after: int | None = None
    """When set, only rows with `id > summarize_after` are summarized."""


agent = Agent(
    info=AgentInfo(
        agent_id=HISTORY_SUMMARIZER_AGENT_ID,
        description="Rolling conversation summarizer (read-only).",
        groups=["l3"],
        capabilities=["llm.generation.text", "summarize.history", "session.history"],
        hidden=True,
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None


@agent.on_startup
async def _startup() -> None:
    global _pool  # noqa: PLW0603 — startup-wired handle
    _pool = await open_pool(_settings)


@agent.on_shutdown
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


def _transcript(rows: list[SessionHistoryRow]) -> str:
    return "\n".join(f"{r.role}: {r.message}" for r in rows)


async def _summarize(
    ctx: TaskContext,
    *,
    rows: list[SessionHistoryRow],
    previous_summary: str | None,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    if not rows:
        # Nothing to fold — preserve the existing summary unchanged.
        return text_output(previous_summary or "")

    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_lite if cfg else settings.default_preset_lite

    user_parts: list[str] = []
    if previous_summary:
        user_parts.append(f"## Previous summary\n{previous_summary}")
    user_parts.append(f"## Conversation\n{_transcript(rows)}")
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content="\n\n".join(user_parts)),
    ]
    resp = await ctx.llm.generate(messages, preset=preset)
    return text_output(resp.text)


async def run_summarize_incumbent(
    ctx: TaskContext,
    payload: SummarizeIncumbent,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    async with pool.acquire() as conn:
        rows = await queries.reload_incumbent(
            conn, session_id=ctx.session_id, agent_id=payload.agent_id,
            up_to_id=payload.up_to,
        )
    return await _summarize(
        ctx, rows=rows, previous_summary=payload.previous_summary,
        pool=pool, settings=settings,
    )


async def run_summarize_all(
    ctx: TaskContext,
    payload: SummarizeAll,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    async with pool.acquire() as conn:
        rows = await queries.reload_incumbent(
            conn, session_id=ctx.session_id, agent_id=payload.agent_id
        )
    if payload.summarize_after is not None:
        rows = [r for r in rows if r.id > payload.summarize_after]
    return await _summarize(
        ctx, rows=rows, previous_summary=None, pool=pool, settings=settings
    )


@agent.handler(
    mode="summarize_incumbent", tool=False,
    description="Fold the oldest part of a thread's incumbent history into "
    "the rolling summary and demote those turns (channel-driven).",
)
async def summarize_incumbent(
    ctx: TaskContext, payload: SummarizeIncumbent
) -> AgentOutput:
    assert _pool is not None
    return await run_summarize_incumbent(
        ctx, payload, pool=_pool, settings=_settings
    )


@agent.handler(
    mode="summarize_all", tool=False,
    description="Produce a fresh full-thread summary from all incumbent "
    "turns (channel-driven).",
)
async def summarize_all(ctx: TaskContext, payload: SummarizeAll) -> AgentOutput:
    assert _pool is not None
    return await run_summarize_all(ctx, payload, pool=_pool, settings=_settings)


if __name__ == "__main__":
    agent.run()
