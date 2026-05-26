"""knowledge_base agent — store/retrieve/list/remove + chunker.

Injects a real KnowledgeStore (LanceDB on a tmp dir) and stubs
`ctx.files` + `ctx.llm.embed` (keyword-based fake vectors), so the agent
logic is exercised without a router/provider.
"""

from __future__ import annotations

import asyncio
import json

from bp_agents.agents.knowledge_base import (
    run_kb_list,
    run_kb_remove,
    run_kb_retrieve,
    run_kb_store,
)
from bp_agents.agents.knowledge_base.agent import (
    KbList,
    KbModify,
    KbRemove,
    KbRetrieve,
    KbStore,
    run_kb_modify,
)
from bp_agents.agents.knowledge_base.chunking import chunk_markdown
from bp_agents.lance import connect
from bp_agents.lance.knowledge import KnowledgeStore
from bp_agents.settings import SuiteSettings
from bp_protocol.types import AgentOutput
from bp_sdk import LlmResponse

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
    def __init__(self, meta: dict | None = None) -> None:
        # Default: supply description only, so a missing title still falls
        # back to the filename stem (keeps the round-trip assertions stable).
        self._meta = meta if meta is not None else {"description": "a note"}

    async def embed(self, texts, *, preset=None):
        return [_kw_vec(t) for t in texts]

    async def generate(self, messages, *, preset=None, **kw):
        return LlmResponse(text=json.dumps(self._meta))


class _StubPeers:
    """Stands in for md_converter — returns a converted `.md` stash file."""

    def __init__(self, files) -> None:
        self._files = files

    async def spawn(self, dest, payload, *, mode=None, **kw):
        # Pretend md_converter wrote "<stem>.md" with markdown content.
        self._files._blobs["converted.md"] = b"converted markdown body about cats"
        return type("R", (), {"output": AgentOutput(content="ok", files=["converted.md"])})()


class _StubCtx:
    def __init__(self, user_id: str, files, llm, peers=None) -> None:
        self.user_id = user_id
        self.files = files
        self.llm = llm
        self.peers = peers


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


def test_chunk_markdown_prefers_header_boundaries() -> None:
    text = (
        "# Intro\n" + "alpha " * 40 + "\n\n"
        "# Methods\n" + "beta " * 40 + "\n\n"
        "# Results\n" + "gamma " * 40
    )
    chunks = chunk_markdown(text, max_len=300, min_len=80, overlap=20)
    assert len(chunks) >= 3
    assert all(len(c) <= 300 for c in chunks)
    # The header boundaries were used as split points — each section heading
    # survives intact in the output.
    joined = "\n".join(chunks)
    assert "# Intro" in joined and "# Methods" in joined and "# Results" in joined


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


def test_kb_store_generates_missing_metadata(tmp_path) -> None:
    async def _drive() -> None:
        store = KnowledgeStore(await connect(tmp_path, "usr_m"), embedding_dim=_DIM)
        settings = _settings()
        ctx = _StubCtx(
            "usr_m", _StubFiles({"cats.md": b"cats purr and nap"}),
            _StubLlm(meta={
                "title": "All About Cats", "tags": ["feline"],
                "description": "a feline primer",
            }),
        )
        await run_kb_store(
            ctx, KbStore(name="cats.md"),  # no title/tags/description
            settings=settings, store=store, preset="emb", lite_preset="lite",
        )
        listed = await run_kb_list(ctx, KbList(), settings=settings, store=store)
        assert "All About Cats" in listed.content
        assert "feline" in listed.content

    asyncio.run(_drive())


def test_kb_modify_updates_metadata(tmp_path) -> None:
    async def _drive() -> None:
        store = KnowledgeStore(await connect(tmp_path, "usr_mod"), embedding_dim=_DIM)
        settings = _settings()
        ctx = _StubCtx("usr_mod", _StubFiles({"d.md": b"cats draft body"}), _StubLlm())
        await run_kb_store(
            ctx, KbStore(name="d.md", title="Draft", tags=["wip"], description="x"),
            settings=settings, store=store, preset="emb", lite_preset="lite",
        )
        out = await run_kb_modify(
            ctx, KbModify(title="Draft", target_title="Report",
                          target_collection="final", tags=["done"]),
            settings=settings, store=store,
        )
        assert "Modified 1" in out.content
        listed = await run_kb_list(ctx, KbList(), settings=settings, store=store)
        assert "Report" in listed.content and "[final]" in listed.content

    asyncio.run(_drive())


def test_kb_store_converts_non_text(tmp_path) -> None:
    async def _drive() -> None:
        store = KnowledgeStore(await connect(tmp_path, "usr_pdf"), embedding_dim=_DIM)
        settings = _settings()
        files = _StubFiles({"report.pdf": b"%PDF-1.4 binary bytes"})
        ctx = _StubCtx("usr_pdf", files, _StubLlm(), peers=_StubPeers(files))
        out = await run_kb_store(
            ctx, KbStore(name="report.pdf", title="Report", tags=["t"], description="d"),
            settings=settings, store=store, preset="emb", lite_preset="lite",
        )
        # Non-text routed through md_converter, then chunked + stored.
        assert "Stored 'Report'" in out.content
        listed = await run_kb_list(ctx, KbList(), settings=settings, store=store)
        assert "Report" in listed.content

    asyncio.run(_drive())
