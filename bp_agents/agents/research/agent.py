"""research agent — l1 web/RAG/document specialist (Phase 3)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bp_agents.agents.l1_common import L1Config, run_delegated_turn, run_subagent
from bp_agents.agents.research.web import make_web_tools
from bp_agents.common import LocalToolset, make_current_time_tool
from bp_agents.common.payloads import MessagePayload
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

RESEARCH_AGENT_ID = "research"

# Shared search discipline — web tools are a means, not a reflex. Keeps the
# loop from burning rounds on redundant searches (and hitting the limit with
# nothing synthesised).
_SEARCH_DISCIPLINE = """\
Search deliberately, not reflexively:
- If you can already answer from the conversation, the knowledge base, or \
your own knowledge, do so WITHOUT searching.
- Search only for facts that are current, niche, or that you're unsure of. \
Use focused queries; one or two good searches usually suffice.
- Fetch a page only when a snippet is not enough, and only the most relevant \
results — don't fetch every hit.
- Stop as soon as you can answer. Then synthesise a concise, sourced reply \
(cite URLs); do not keep searching for more.\
"""

_SUBAGENT_SYSTEM = f"""\
You are a research specialist. You can search + fetch the web and use the \
knowledge base (call_knowledge_base_*) to store and recall documents.

{_SEARCH_DISCIPLINE}\
"""
_DELEGATION_SYSTEM = f"""\
You specialise in web + document research: find, read, and synthesise \
information for the user, and store genuinely useful material in the \
knowledge base.

{_SEARCH_DISCIPLINE}\
"""


async def _tools(
    ctx: TaskContext, settings: SuiteSettings, timezone: str
) -> LocalToolset:
    return LocalToolset(
        [make_current_time_tool(timezone), *make_web_tools(settings)]
    )


_CONFIG = L1Config(
    agent_id=RESEARCH_AGENT_ID,
    subagent_system=_SUBAGENT_SYSTEM,
    delegation_system=_DELEGATION_SYSTEM,
    preset_field="preset_balanced",
    local_tools=_tools,
    file_tools="full",
)


agent = Agent(
    info=AgentInfo(
        agent_id=RESEARCH_AGENT_ID,
        description="Web and knowledge-base research specialist.",
        groups=["l1"],
        capabilities=[
            "llm.generation.text", "assistant.rag", "assistant.web",
            "assistant.document", "file.full", "database.manage",
            "database.retrieval", "document.convert", "session.history",
            "web.fetch",
        ],
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


@agent.handler(
    mode="subagent",
    description="Research a question end-to-end (web search, page fetch, "
    "knowledge-base retrieval) and return a sourced Markdown answer.",
)
async def subagent(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_subagent(ctx, payload, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(
    mode="on_delegation", tool=False,
    description="First turn after the orchestrator delegates a research "
    "conversation (delegation lifecycle; not a tool).",
)
async def on_delegation(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(
        ctx, config=_CONFIG, pool=_pool, settings=_settings, first_turn=True
    )


@agent.handler(
    mode="delegated_message", tool=False,
    description="A user turn while research holds the delegated conversation "
    "(delegation lifecycle; not a tool).",
)
async def delegated_message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(
        ctx, config=_CONFIG, pool=_pool, settings=_settings, first_turn=False
    )


if __name__ == "__main__":
    agent.run()
