"""KnowledgeStore — LanceDB round-trips with fake embeddings (no LLM).

Validates the per-user store: document + chunk insert, vector retrieve
with metadata filters, sha256 dedup lookup, listing filters, and removal.
"""

from __future__ import annotations

import asyncio

from bp_agents.lance import connect
from bp_agents.lance.knowledge import KnowledgeStore

_DIM = 8


def _vec(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i] = 1.0
    return v


def test_knowledge_store_round_trip(tmp_path) -> None:
    async def _drive() -> None:
        db = await connect(tmp_path, "usr_a")
        store = KnowledgeStore(db, embedding_dim=_DIM)

        await store.store_document(
            collection="default", title="Cats", tags=["animals"],
            description="about cats", sha256="sha-cats", source_name="cats.md",
            chunks=[(0, "cats purr and nap", _vec(0))],
        )
        await store.store_document(
            collection="work", title="Dogs", tags=["animals", "pets"],
            description="about dogs", sha256="sha-dogs", source_name="dogs.md",
            chunks=[(0, "dogs bark and fetch", _vec(1))],
        )

        # Vector retrieve returns the nearest chunk.
        hits = await store.retrieve(query_vector=_vec(0), count=1)
        assert len(hits) == 1 and hits[0]["title"] == "Cats"

        # Collection filter scopes the result.
        scoped = await store.retrieve(
            query_vector=_vec(0), collection="work", count=1
        )
        assert scoped == [] or scoped[0]["collection"] == "work"

        # Tag filter.
        tagged = await store.retrieve(
            query_vector=_vec(1), tags=["pets"], count=2
        )
        assert all("pets" in h["tags"] for h in tagged)

        # sha256 dedup lookup.
        assert await store.find_by_sha256("sha-cats") is not None
        assert await store.find_by_sha256("sha-missing") is None

        # Listing + filters.
        assert len(await store.list_documents()) == 2
        assert len(await store.list_documents(collection="work")) == 1
        assert len(await store.list_documents(tag="pets")) == 1
        assert len(await store.list_documents(query="cats")) == 1

        # Removal drops the doc + its chunks.
        assert await store.remove_document(title="Cats") == 1
        assert await store.find_by_sha256("sha-cats") is None
        assert await store.retrieve(query_vector=_vec(0), count=5) == [] or all(
            h["title"] != "Cats"
            for h in await store.retrieve(query_vector=_vec(0), count=5)
        )

    asyncio.run(_drive())
