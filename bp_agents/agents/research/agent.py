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

_SUBAGENT_SYSTEM = """\
You are a research specialist. Use web search + fetch to find current \
information, and the knowledge base (call_knowledge_base_*) to store and \
recall documents. Cite sources (URLs).\
"""
_DELEGATION_SYSTEM = """\
You specialise in web + document research. Find, read, and synthesise \
information for the user; store useful material in the knowledge base.\
"""


async def _tools(ctx: TaskContext, settings: SuiteSettings) -> LocalToolset:
    return LocalToolset(
        [make_current_time_tool(settings.default_timezone), *make_web_tools(settings)]
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
        description="Web, RAG, and document research; owns the knowledge base.",
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


@agent.handler(mode="subagent")
async def subagent(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_subagent(ctx, payload, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(mode="on_delegation", tool=False)
async def on_delegation(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(ctx, config=_CONFIG, pool=_pool, settings=_settings)


@agent.handler(mode="delegated_message", tool=False)
async def delegated_message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_delegated_turn(ctx, config=_CONFIG, pool=_pool, settings=_settings)


if __name__ == "__main__":
    agent.run()
