"""MemoryStore — fact-graph data layer with fake embeddings (no LLM)."""

from __future__ import annotations

import asyncio

from bp_agents.lance import connect
from bp_agents.lance.memory import MemoryStore

_DIM = 8


def _vec(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i % _DIM] = 1.0
    return v


def test_memory_store_graph_ops(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)

        f0 = await store.insert_fact(fact="likes cats", kind="preference", embedding=_vec(0))
        f1 = await store.insert_fact(fact="lives in Paris", kind="personal_info", embedding=_vec(1))
        await store.insert_fact(fact="project alpha", kind="project", embedding=_vec(2))

        # Vector search returns the nearest fact.
        hits = await store.search(query_vector=_vec(0), limit=1)
        assert hits[0]["uid"] == f0

        # Edge-set: bidirectional + deduped.
        await store.add_edge(f0, f1)
        await store.add_edge(f1, f0)  # dedup
        assert await store.neighbors(f0) == [f1]
        assert await store.neighbors(f1) == [f0]

        # Update preserves uid + edges.
        await store.update_fact(uid=f0, fact="adores cats", embedding=_vec(0))
        again = await store.get_fact(f0)
        assert again["fact"] == "adores cats"
        assert await store.neighbors(f0) == [f1]

        # Remove cascades edges on both endpoints.
        await store.remove_fact(f1)
        assert await store.get_fact(f1) is None
        assert await store.neighbors(f0) == []

        # GC removes stale facts (horizon 0 → everything is "older").
        removed = await store.gc(horizon_days=0)
        assert removed >= 1
        assert await store.all_facts() == []

    asyncio.run(_drive())


def test_memory_store_bm25(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_b"), embedding_dim=_DIM)
        await store.insert_fact(fact="likes cats", kind="preference", embedding=_vec(0))
        await store.insert_fact(
            fact="rides a bicycle", kind="event", embedding=_vec(1)
        )
        # BM25 over `fact` text — the keyword surfaces the matching fact even
        # with an unrelated query vector.
        hits = await store.search_bm25(query="bicycle", limit=5)
        assert hits and hits[0]["fact"] == "rides a bicycle"

    asyncio.run(_drive())


def test_memory_store_bm25_empty(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_e"), embedding_dim=_DIM)
        assert await store.search_bm25(query="anything", limit=5) == []

    asyncio.run(_drive())


def test_memory_store_degree_cap(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_d"), embedding_dim=_DIM)
        hub = await store.insert_fact(fact="hub", kind="event", embedding=_vec(0))
        others = [
            await store.insert_fact(fact=f"f{i}", kind="event", embedding=_vec(i))
            for i in range(1, 13)
        ]
        for o in others:
            await store.add_edge(hub, o)
        # Degree is capped at 10 (oldest edges evicted).
        assert len(await store.neighbors(hub)) == 10

    asyncio.run(_drive())
