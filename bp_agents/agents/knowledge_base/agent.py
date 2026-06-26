"""knowledge_base agent — per-user document store + retrieval.

`store` ingests a file-store document (chunk → embed → dedup → persist);
`retrieve` embeds a query and returns the nearest chunks; `list` /
`remove` manage the document set. Embeddings use the user's embedding
preset; the per-user LanceDB resolves from the authoritative `user_id`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bp_agents.agents.knowledge_base.chunking import chunk_markdown
from bp_agents.common import text_output
from bp_agents.common.payloads import MAX_PAGE, KbBrowse, KbDelete
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.lance import connect
from bp_agents.lance.knowledge import KnowledgeStore
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_AGENT_ID = "knowledge_base"
MD_CONVERTER_AGENT_ID = "md_converter"

# Extensions ingested as text directly; anything else routes through
# md_converter.convert first ([agents.md]).
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".rst", ""}

_META_SYSTEM = (
    "Generate concise catalog metadata for the document excerpt below. "
    'Return ONLY JSON: {"title": "<short title>", "tags": ["<tag>", ...], '
    '"description": "<one-sentence summary>"}.'
)


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


class KbModify(BaseModel):
    title: str
    collection: str | None = None
    target_collection: str | None = None
    target_title: str | None = None
    tags: list[str] | None = None
    description: str | None = None


agent = Agent(
    info=AgentInfo(
        agent_id=KNOWLEDGE_BASE_AGENT_ID,
        description=(
            "The user's personal document knowledge base — search, store, "
            "list, modify, and remove documents."
        ),
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


async def _presets(ctx: TaskContext, settings: SuiteSettings) -> tuple[str, str]:
    """(embedding_preset, lite_preset) for this user."""
    if _pool is None:
        return settings.default_preset_embedding, settings.default_preset_lite
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    if cfg is None:
        return settings.default_preset_embedding, settings.default_preset_lite
    return cfg.preset_embedding, cfg.preset_lite


async def _embedding_preset(ctx: TaskContext, settings: SuiteSettings) -> str:
    embed, _lite = await _presets(ctx, settings)
    return embed


async def _store_for(ctx: TaskContext, settings: SuiteSettings) -> KnowledgeStore:
    db = await connect(settings.lance_root, ctx.user_id)
    return KnowledgeStore(db, embedding_dim=settings.embedding_dim)


async def _to_markdown(ctx: TaskContext, name: str) -> str:
    """Markdown text for ingest — decoded directly for text files, else
    converted via `md_converter.convert` (full content as a `.md` stash
    file, [agents.md])."""
    if PurePosixPath(name).suffix.lower() in _TEXT_EXTS:
        data = await ctx.files.read_bytes(name)
        return data.decode("utf-8", errors="replace")
    result = await ctx.peers.spawn(
        MD_CONVERTER_AGENT_ID, {"name": name, "output_type": "file"}, mode="convert",
    )
    out = result.output if result else None
    files = list(out.files) if out and out.files else []
    if files:
        md = await ctx.files.read_bytes(files[0])
        return md.decode("utf-8", errors="replace")
    return (out.content if out else "") or ""


def _parse_meta(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        text = text.removeprefix("json").strip().strip("`").strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("kb_meta_parse_failed", extra={"event": "kb_meta_parse_failed"})
        return {}


async def _generate_metadata(
    ctx: TaskContext, text: str, *, preset: str, settings: SuiteSettings
) -> dict:
    """LLM-generate {title, tags, description} from the document's head +
    tail window ([agents.md])."""
    head = text[: settings.kb_meta_head_chars]
    tail = (
        text[-settings.kb_meta_tail_chars :]
        if settings.kb_meta_tail_chars and len(text) > settings.kb_meta_head_chars
        else ""
    )
    excerpt = f"{head}\n\n…\n\n{tail}" if tail else head
    resp = await ctx.llm.generate(
        [Message(role="system", content=_META_SYSTEM),
         Message(role="user", content=excerpt)],
        preset=preset,
    )
    return _parse_meta(resp.text)


async def _embed_chunks(
    ctx: TaskContext, chunks: list[str], *, preset: str, batch_size: int
) -> list[list[float]]:
    """Embed `chunks` in bounded batches, returning vectors in chunk order.

    Embedding the whole document in one `ctx.llm.embed(chunks)` call sends every
    chunk to the provider at once — which blows the provider's per-request
    input/token limit (and the ~1 MiB WS frame cap, made worse by chunk
    overlap) for a large document, failing the entire `store`. Batching caps
    each request; batches run sequentially to keep the embedding load steady."""
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        vectors.extend(await ctx.llm.embed(batch, preset=preset))
    return vectors


async def run_kb_store(
    ctx: TaskContext,
    payload: KbStore,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
    preset: str | None = None,
    lite_preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    if preset is None or lite_preset is None:
        embed_default, lite_default = await _presets(ctx, settings)
        preset = preset or embed_default
        lite_preset = lite_preset or lite_default

    # Content-addressed dedup over the ORIGINAL file bytes (so re-storing
    # the same source — even a non-text one — dedups before conversion).
    data = await ctx.files.read_bytes(payload.name)
    sha256 = hashlib.sha256(data).hexdigest()
    if await store.find_by_sha256(sha256) is not None:
        return text_output(f"'{payload.name}' is already in the knowledge base.")

    if PurePosixPath(payload.name).suffix.lower() in _TEXT_EXTS:
        text = data.decode("utf-8", errors="replace")
    else:
        text = await _to_markdown(ctx, payload.name)
    chunks = chunk_markdown(
        text, max_len=settings.kb_max_chunk_len,
        min_len=settings.kb_min_chunk_len, overlap=settings.kb_overlap_len,
    )
    if not chunks:
        return text_output(f"'{payload.name}' is empty; nothing stored.")

    # LLM-generate any metadata the caller omitted ([agents.md]).
    title, tags, description = payload.title, payload.tags, payload.description
    if title is None or tags is None or description is None:
        gen = await _generate_metadata(ctx, text, preset=lite_preset, settings=settings)
        if title is None:
            title = gen.get("title")
        if tags is None and isinstance(gen.get("tags"), list):
            tags = [str(t) for t in gen["tags"]]
        if description is None:
            description = gen.get("description")
    title = title or PurePosixPath(payload.name).stem

    vectors = await _embed_chunks(
        ctx, chunks, preset=preset, batch_size=settings.kb_embed_batch_size
    )
    await store.store_document(
        collection=payload.collection, title=title,
        tags=tags or [], description=description or "",
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
        query=payload.query, query_vector=qv, search_type=payload.search_type,
        collection=payload.collection, title=payload.title,
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


async def run_kb_modify(
    ctx: TaskContext,
    payload: KbModify,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    n = await store.modify_document(
        title=payload.title, collection=payload.collection,
        target_collection=payload.target_collection,
        target_title=payload.target_title, tags=payload.tags,
        description=payload.description,
    )
    return text_output(
        f"Modified {n} document(s) titled '{payload.title}'."
        if n else f"No document titled '{payload.title}' found."
    )


@agent.handler(
    mode="store",
    description="Save a document (text or an uploaded file) to the user's "
    "knowledge base for later retrieval.",
)
async def store_mode(ctx: TaskContext, payload: KbStore) -> AgentOutput:
    return await run_kb_store(ctx, payload, settings=_settings)


@agent.handler(
    mode="modify",
    description="Re-file, retitle, or re-tag an existing knowledge-base document.",
)
async def modify_mode(ctx: TaskContext, payload: KbModify) -> AgentOutput:
    return await run_kb_modify(ctx, payload, settings=_settings)


@agent.handler(
    mode="retrieve",
    description="Search the user's knowledge base and return the most "
    "relevant document chunks for a query.",
)
async def retrieve_mode(ctx: TaskContext, payload: KbRetrieve) -> AgentOutput:
    return await run_kb_retrieve(ctx, payload, settings=_settings)


@agent.handler(
    mode="list",
    description="List the documents in the user's knowledge base.",
)
async def list_mode(ctx: TaskContext, payload: KbList) -> AgentOutput:
    return await run_kb_list(ctx, payload, settings=_settings)


@agent.handler(
    mode="remove",
    description="Delete a document from the user's knowledge base by id.",
)
async def remove_mode(ctx: TaskContext, payload: KbRemove) -> AgentOutput:
    return await run_kb_remove(ctx, payload, settings=_settings)


# ---------------------------------------------------------------------------
# Webapp Knowledge base page — browse / delete (tool:false, JSON)
# ---------------------------------------------------------------------------


def _doc_item(d: dict) -> dict:
    return {
        "doc_id": d.get("doc_id", ""),
        "title": d["title"],
        "collection": d["collection"],
        "tags": d.get("tags") or [],
        "description": d.get("description", ""),
        "created_at": d.get("created_at", ""),
        "updated_at": d.get("updated_at", ""),
    }


async def run_kb_browse(
    ctx: TaskContext,
    payload: KbBrowse,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    docs = await store.list_documents(
        collection=payload.collection, tag=payload.tag, query=payload.query
    )
    # Recency-sorted (most recently updated first).
    docs.sort(
        key=lambda d: d.get("updated_at") or d.get("created_at") or "", reverse=True
    )
    start = max(0, payload.start)
    end = max(start, min(payload.end, start + MAX_PAGE))
    items = [_doc_item(d) for d in docs[start:end]]
    return text_output(json.dumps({"items": items, "total": len(docs)}))


async def run_kb_delete(
    ctx: TaskContext,
    payload: KbDelete,
    *,
    settings: SuiteSettings,
    store: KnowledgeStore | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    n = await store.remove_document(title=payload.title, collection=payload.collection)
    return text_output(json.dumps({"deleted": n}))


@agent.handler(
    mode="browse", tool=False,
    description="List documents for the Knowledge base page (JSON; "
    "recency-sorted, title/collection/tag-filterable, paged).",
)
async def browse_mode(ctx: TaskContext, payload: KbBrowse) -> AgentOutput:
    return await run_kb_browse(ctx, payload, settings=_settings)


@agent.handler(
    mode="delete", tool=False,
    description="Delete a knowledge-base document by title (+ collection) "
    "from the Knowledge base page.",
)
async def delete_mode(ctx: TaskContext, payload: KbDelete) -> AgentOutput:
    return await run_kb_delete(ctx, payload, settings=_settings)


if __name__ == "__main__":
    agent.run()
