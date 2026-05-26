"""knowledge_base agent — store/retrieve/list/remove + chunker.

Injects a real KnowledgeStore (LanceDB on a tmp dir) and stubs
`ctx.files` + `ctx.llm.embed` (keyword-based fake vectors), so the agent
logic is exercised without a router/provider.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.knowledge_base import (
    run_kb_list,
    run_kb_remove,
    run_kb_retrieve,
    run_kb_store,
)
from bp_agents.agents.knowledge_base.agent import KbList, KbRemove, KbRetrieve, KbStore
from bp_agents.agents.knowledge_base.chunking import chunk_markdown
from bp_agents.lance import connect
from bp_agents.lance.knowledge import KnowledgeStore
from bp_agents.settings import SuiteSettings

_DIM = 8


def _kw_vec(text: str) -> list[float]:
    v = [0.0] * _DIM
    t = text.lower()
    v[0 if "cat" in t else 1 if "dog" in t else 2] = 1.0
    return v


class _StubFiles:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    async def read_bytes(self, name: str) -> bytes:
        return self._blobs[name]


class _StubLlm:
    async def embed(self, texts, *, preset=None):
        return [_kw_vec(t) for t in texts]


class _StubCtx:
    def __init__(self, user_id: str, files, llm) -> None:
        self.user_id = user_id
        self.files = files
        self.llm = llm


def _settings() -> SuiteSettings:
    return SuiteSettings(
        embedding_dim=_DIM, kb_max_chunk_len=60, kb_min_chunk_len=10,
        kb_overlap_len=5,
    )


def test_chunk_markdown_bounds() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("short") == ["short"]
    text = "\n\n".join(f"paragraph number {i} with some words" for i in range(8))
    chunks = chunk_markdown(text, max_len=60, min_len=10, overlap=5)
    assert len(chunks) > 1
    assert all(len(c) <= 60 + 5 for c in chunks)


def test_kb_store_retrieve_list_remove(tmp_path) -> None:
    async def _drive() -> None:
        store = KnowledgeStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        settings = _settings()
        ctx = _StubCtx(
            "usr_a",
            _StubFiles({
                "cats.md": b"cats purr and nap in the sun",
                "dogs.md": b"dogs bark and fetch the ball",
            }),
            _StubLlm(),
        )

        out = await run_kb_store(
            ctx, KbStore(name="cats.md", tags=["animals"]),
            settings=settings, store=store, preset="emb",
        )
        assert "Stored 'cats'" in out.content
        await run_kb_store(
            ctx, KbStore(name="dogs.md", tags=["animals"]),
            settings=settings, store=store, preset="emb",
        )

        # Dedup: storing the same bytes again is a no-op.
        again = await run_kb_store(
            ctx, KbStore(name="cats.md"),
            settings=settings, store=store, preset="emb",
        )
        assert "already in" in again.content

        # Retrieve by query keyword → the cat document.
        hit = await run_kb_retrieve(
            ctx, KbRetrieve(query="tell me about cats", count=1),
            settings=settings, store=store, preset="emb",
        )
        assert "cats" in hit.content.lower()

        # List shows both.
        listed = await run_kb_list(ctx, KbList(), settings=settings, store=store)
        assert "cats" in listed.content and "dogs" in listed.content

        # Remove the cat doc.
        removed = await run_kb_remove(
            ctx, KbRemove(title="cats"), settings=settings, store=store
        )
        assert "Removed 1" in removed.content
        listed2 = await run_kb_list(ctx, KbList(), settings=settings, store=store)
        assert "cats" not in listed2.content

    asyncio.run(_drive())
