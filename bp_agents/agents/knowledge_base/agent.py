"""knowledge_base agent — per-user document store + retrieval.

`store` ingests a file-store document (chunk → embed → dedup → persist);
`retrieve` embeds a query and returns the nearest chunks; `list` /
`remove` manage the document set. Embeddings use the user's embedding
preset; the per-user LanceDB resolves from the authoritative `user_id`.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bp_agents.agents.knowledge_base.chunking import chunk_markdown
from bp_agents.common import text_output
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.lance import connect
from bp_agents.lance.knowledge import KnowledgeStore
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_AGENT_ID = "knowledge_base"


class KbStore(BaseModel):
    name: str
    collection: str = "default"
    title: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    overwrite: str = "append_count"


class KbRetrieve(BaseModel):
    query: str
    collection: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    search_type: str = "hybrid"
    count: int = 3


class KbList(BaseModel):
    query: str | None = None
    collection: str | None = None
    tag: str | None = None


class KbRemove(BaseModel):
    title: str
    collection: str | None = None


agent = Agent(
    info=AgentInfo(
        agent_id=KNOWLEDGE_BASE_AGENT_ID,
        description="Per-user document knowledge base (store + retrieval).",
        groups=["l3"],
        capabilities=[
            "database.manage", "database.retrieval", "file.full",
            "document.convert",
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


async def _embedding_preset(ctx: TaskContext, settings: SuiteSettings) -> str:
    if _pool is None:
        return settings.default_preset_embedding
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    return cfg.preset_embedding if cfg else settings.default_preset_embedding


async def _store_for(ctx: TaskContext, settings: SuiteSettings) -> KnowledgeStore:
    db = await connect(settings.lance_root, ctx.user_id)
    return KnowledgeStore(db, embedding_dim=settings.embedding_dim)


async def run_kb_store(
    ctx: TaskContext,
    payload: KbStore,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
    preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    preset = preset or await _embedding_preset(ctx, settings)

    data = await ctx.files.read_bytes(payload.name)
    sha256 = hashlib.sha256(data).hexdigest()
    if await store.find_by_sha256(sha256) is not None:
        return text_output(f"'{payload.name}' is already in the knowledge base.")

    # Phase 2 ingests Markdown/text directly; non-text conversion routes
    # through md_converter (wired once that agent lands).
    text = data.decode("utf-8", errors="replace")
    chunks = chunk_markdown(
        text, max_len=settings.kb_max_chunk_len,
        min_len=settings.kb_min_chunk_len, overlap=settings.kb_overlap_len,
    )
    if not chunks:
        return text_output(f"'{payload.name}' is empty; nothing stored.")

    vectors = await ctx.llm.embed(chunks, preset=preset)
    title = payload.title or PurePosixPath(payload.name).stem
    await store.store_document(
        collection=payload.collection, title=title,
        tags=payload.tags or [], description=payload.description or "",
        sha256=sha256, source_name=payload.name,
        chunks=[(i, c, v) for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))],
    )
    return text_output(
        f"Stored '{title}' in collection '{payload.collection}' "
        f"({len(chunks)} chunks)."
    )


async def run_kb_retrieve(
    ctx: TaskContext,
    payload: KbRetrieve,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
    preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    preset = preset or await _embedding_preset(ctx, settings)
    qv = (await ctx.llm.embed([payload.query], preset=preset))[0]
    hits = await store.retrieve(
        query_vector=qv, collection=payload.collection, title=payload.title,
        tags=payload.tags, count=payload.count,
    )
    if not hits:
        return text_output("No matching documents found.")
    blocks = [
        f"### {h['title']} (chunk {h['chunk_index']})\n{h['content']}"
        for h in hits
    ]
    return text_output("\n\n".join(blocks))


async def run_kb_list(
    ctx: TaskContext,
    payload: KbList,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    docs = await store.list_documents(
        collection=payload.collection, tag=payload.tag, query=payload.query
    )
    if not docs:
        return text_output("The knowledge base is empty.")
    lines = [
        f"- {d['title']} [{d['collection']}]"
        + (f" tags={d['tags']}" if d.get("tags") else "")
        + (f" — {d['description']}" if d.get("description") else "")
        for d in docs
    ]
    return text_output("\n".join(lines))


async def run_kb_remove(
    ctx: TaskContext,
    payload: KbRemove,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    n = await store.remove_document(title=payload.title, collection=payload.collection)
    return text_output(
        f"Removed {n} document(s) titled '{payload.title}'."
        if n else f"No document titled '{payload.title}' found."
    )


@agent.handler(mode="store")
async def store_mode(ctx: TaskContext, payload: KbStore) -> AgentOutput:
    return await run_kb_store(ctx, payload, settings=_settings)


@agent.handler(mode="retrieve")
async def retrieve_mode(ctx: TaskContext, payload: KbRetrieve) -> AgentOutput:
    return await run_kb_retrieve(ctx, payload, settings=_settings)


@agent.handler(mode="list")
async def list_mode(ctx: TaskContext, payload: KbList) -> AgentOutput:
    return await run_kb_list(ctx, payload, settings=_settings)


@agent.handler(mode="remove")
async def remove_mode(ctx: TaskContext, payload: KbRemove) -> AgentOutput:
    return await run_kb_remove(ctx, payload, settings=_settings)


if __name__ == "__main__":
    agent.run()
