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
    run_kb_browse,
    run_kb_delete,
    run_kb_modify,
)
from bp_agents.agents.knowledge_base.chunking import chunk_markdown
from bp_agents.common.payloads import KbBrowse, KbDelete
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


class _RecordingLlm(_StubLlm):
    """Records the size of each embed batch so a test can assert that `store`
    splits a document's chunks into bounded requests instead of one big call."""

    def __init__(self, meta: dict | None = None) -> None:
        super().__init__(meta)
        self.batches: list[int] = []

    async def embed(self, texts, *, preset=None):
        self.batches.append(len(texts))
        return [_kw_vec(t) for t in texts]


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

        # Metadata filter is case-insensitive: tag "ANIMALS" matches "animals".
        tagged = await run_kb_retrieve(
            ctx, KbRetrieve(query="tell me about cats", tags=["ANIMALS"], count=1),
            settings=settings, store=store, preset="emb",
        )
        assert "cats" in tagged.content.lower()

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


def test_kb_store_batches_embeddings(tmp_path) -> None:
    """A document's chunks are embedded in bounded batches (≤ batch_size per
    request), not one giant call — and the vectors still cover every chunk."""
    async def _drive() -> None:
        store = KnowledgeStore(await connect(tmp_path, "usr_b"), embedding_dim=_DIM)
        # Tiny chunks + batch so a moderate doc clearly spans several requests.
        settings = SuiteSettings(
            embedding_dim=_DIM, kb_max_chunk_len=20, kb_min_chunk_len=10,
            kb_overlap_len=0, kb_embed_batch_size=2,
        )
        body = (
            b"alpha bravo charlie delta echo foxtrot golf hotel india juliet "
            b"kilo lima mike november oscar papa quebec romeo sierra tango"
        )
        llm = _RecordingLlm()
        ctx = _StubCtx("usr_b", _StubFiles({"doc.md": body}), llm)

        out = await run_kb_store(
            ctx, KbStore(name="doc.md"),
            settings=settings, store=store, preset="emb",
        )
        assert llm.batches, "embed was never called"
        # No batch exceeded the cap, and they tile the whole chunk set.
        assert all(b <= 2 for b in llm.batches)
        n_chunks = sum(llm.batches)
        assert n_chunks > 2  # the doc really did split into multiple batches
        assert len(llm.batches) == (n_chunks + 2 - 1) // 2  # ceil(n / batch_size)
        assert f"({n_chunks} chunks)" in out.content

    asyncio.run(_drive())


def test_safe_embed_batch_caps_by_embedding_dim() -> None:
    """The effective batch is bounded by the inline result-frame size, so a
    high embedding_dim shrinks it below the configured chunk cap — keeping a
    batch's vectors under the ~1 MiB WS frame cap (a 100×1536 result is ~3 MiB
    and can't be received → the embed call hangs)."""
    from bp_agents.agents.knowledge_base.agent import _safe_embed_batch

    # Tiny dim → the configured cap wins unchanged.
    assert _safe_embed_batch(100, 8) == 100
    # dim 1536 → response budget binds, far below the configured 100.
    b1536 = _safe_embed_batch(100, 1536)
    assert 1 <= b1536 <= 30
    # The resulting frame stays well under 1 MiB (~21 bytes per JSON float).
    assert b1536 * 1536 * 21 < 1_000_000
    # Higher dim → an even smaller batch.
    assert _safe_embed_batch(100, 3072) < b1536


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


# ---------------------------------------------------------------------------
# Webapp Knowledge base page modes — browse / delete
# ---------------------------------------------------------------------------


async def _seed_docs(store) -> None:
    for title, coll, tags in [
        ("Cats 101", "pets", ["cat"]),
        ("Dogs 101", "pets", ["dog"]),
        ("Paris guide", "travel", ["fr"]),
    ]:
        await store.store_document(
            collection=coll, title=title, tags=tags, description="d",
            sha256=title, source_name=f"{title}.md",
            chunks=[(0, title, _kw_vec(title))],
        )


def test_kb_browse_filters_and_fields(tmp_path) -> None:
    async def _drive() -> tuple[dict, dict, dict]:
        store = KnowledgeStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        await _seed_docs(store)
        ctx = _StubCtx("usr_a", None, _StubLlm())
        allout = await run_kb_browse(ctx, KbBrowse(), settings=_settings(), store=store)
        pets = await run_kb_browse(
            ctx, KbBrowse(collection="pets"), settings=_settings(), store=store
        )
        titled = await run_kb_browse(
            ctx, KbBrowse(query="paris"), settings=_settings(), store=store
        )
        return (json.loads(allout.content), json.loads(pets.content),
                json.loads(titled.content))

    allres, pets, titled = asyncio.run(_drive())
    assert allres["total"] == 3
    assert {i["title"] for i in pets["items"]} == {"Cats 101", "Dogs 101"}
    assert [i["title"] for i in titled["items"]] == ["Paris guide"]
    first = allres["items"][0]
    assert {"doc_id", "title", "collection", "tags", "updated_at"} <= set(first)


def test_kb_browse_metadata_filters_are_case_insensitive(tmp_path) -> None:
    """collection / tag filters match regardless of case — seeded `pets` /
    `cat` are found via `PETS` / `Cat`."""
    async def _drive() -> tuple[dict, dict]:
        store = KnowledgeStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        await _seed_docs(store)
        ctx = _StubCtx("usr_a", None, _StubLlm())
        coll = await run_kb_browse(
            ctx, KbBrowse(collection="PETS"), settings=_settings(), store=store
        )
        tag = await run_kb_browse(
            ctx, KbBrowse(tag="Cat"), settings=_settings(), store=store
        )
        return json.loads(coll.content), json.loads(tag.content)

    coll, tag = asyncio.run(_drive())
    assert {i["title"] for i in coll["items"]} == {"Cats 101", "Dogs 101"}
    assert {i["title"] for i in tag["items"]} == {"Cats 101"}


def test_kb_browse_paging_caps_at_50(tmp_path) -> None:
    async def _drive() -> int:
        store = KnowledgeStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        for i in range(55):
            await store.store_document(
                collection="c", title=f"doc {i}", tags=[], description="",
                sha256=f"s{i}", source_name=f"{i}.md",
                chunks=[(0, f"doc {i}", _kw_vec("x"))],
            )
        out = await run_kb_browse(
            _StubCtx("usr_a", None, _StubLlm()), KbBrowse(start=0, end=999),
            settings=_settings(), store=store,
        )
        return len(json.loads(out.content)["items"])

    assert asyncio.run(_drive()) == 50


def test_kb_delete_removes_by_title(tmp_path) -> None:
    async def _drive() -> tuple[dict, int]:
        store = KnowledgeStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        await _seed_docs(store)
        out = await run_kb_delete(
            _StubCtx("usr_a", None, _StubLlm()),
            KbDelete(title="Dogs 101", collection="pets"),
            settings=_settings(), store=store,
        )
        return json.loads(out.content), len(await store.list_documents())

    res, remaining = asyncio.run(_drive())
    assert res["deleted"] == 1 and remaining == 2
