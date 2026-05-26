"""bp_agents.lance.knowledge — per-user knowledge-base store.

`documents` (metadata) + `chunks` (searchable units) tables
([data-model.md] §2.1). Pure data layer: embeddings are computed by the
agent and passed in. Blocking LanceDB calls run in `asyncio.to_thread`.
Small metadata reads scan the (per-user, modest) `documents` table and
filter in Python — avoids SQL-escaping arbitrary titles/collections.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa


def _documents_schema() -> pa.Schema:
    return pa.schema([
        ("doc_id", pa.string()),
        ("collection", pa.string()),
        ("title", pa.string()),
        ("tags", pa.list_(pa.string())),
        ("description", pa.string()),
        ("sha256", pa.string()),
        ("source_name", pa.string()),
        ("created_at", pa.string()),
        ("updated_at", pa.string()),
    ])


def _chunks_schema(dim: int) -> pa.Schema:
    return pa.schema([
        ("chunk_id", pa.string()),
        ("doc_id", pa.string()),
        ("collection", pa.string()),
        ("title", pa.string()),
        ("tags", pa.list_(pa.string())),
        ("chunk_index", pa.int64()),
        ("content", pa.string()),
        ("embedding", pa.list_(pa.float32(), dim)),
    ])


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeStore:
    """Per-user knowledge base over one LanceDB connection."""

    def __init__(self, db: Any, *, embedding_dim: int) -> None:
        self._db = db
        self._dim = embedding_dim

    # ------------------------------------------------------------------
    # Table accessors (lazy create)
    # ------------------------------------------------------------------

    def _docs(self) -> Any:
        return self._db.create_table(
            "documents", schema=_documents_schema(), exist_ok=True
        )

    def _chunks(self) -> Any:
        return self._db.create_table(
            "chunks", schema=_chunks_schema(self._dim), exist_ok=True
        )

    # ------------------------------------------------------------------
    # Sync cores
    # ------------------------------------------------------------------

    def _scan_docs(self) -> list[dict[str, Any]]:
        return self._docs().search().limit(100_000).to_list()

    def _store_document_sync(
        self, *, doc_id: str, collection: str, title: str,
        tags: list[str], description: str, sha256: str, source_name: str,
        chunks: list[tuple[int, str, list[float]]],
    ) -> None:
        now = _now()
        self._docs().add([{
            "doc_id": doc_id, "collection": collection, "title": title,
            "tags": tags, "description": description, "sha256": sha256,
            "source_name": source_name, "created_at": now, "updated_at": now,
        }])
        self._chunks().add([
            {
                "chunk_id": uuid.uuid4().hex, "doc_id": doc_id,
                "collection": collection, "title": title, "tags": tags,
                "chunk_index": idx, "content": content, "embedding": emb,
            }
            for idx, content, emb in chunks
        ])

    def _filter(
        self, rows: list[dict[str, Any]], *, collection: str | None,
        title: str | None, tags: list[str] | None, count: int,
    ) -> list[dict[str, Any]]:
        """Apply the metadata filters in Python and cap at `count`."""
        out: list[dict[str, Any]] = []
        for r in rows:
            if collection is not None and r["collection"] != collection:
                continue
            if title is not None and r["title"] != title:
                continue
            if tags and not set(tags).issubset(set(r.get("tags") or [])):
                continue
            out.append(r)
            if len(out) >= count:
                break
        return out

    def _vector_rows(self, query_vector: list[float], over: int) -> list[dict[str, Any]]:
        return self._chunks().search(query_vector).limit(over).to_list()

    def _bm25_rows(self, query: str, over: int) -> list[dict[str, Any]]:
        tbl = self._chunks()
        if tbl.count_rows() == 0:
            return []
        # (Re)build the BM25/FTS index over `content` on this handle, then
        # search it. Rebuilt at query time so it always covers current rows
        # — per-user stores are modest, so staleness is impossible.
        tbl.create_fts_index("content", use_tantivy=False, replace=True)
        return tbl.search(query, query_type="fts").limit(over).to_list()

    def _retrieve_sync(
        self, *, query: str, query_vector: list[float], search_type: str,
        collection: str | None, title: str | None, tags: list[str] | None,
        count: int,
    ) -> list[dict[str, Any]]:
        over = max(count * 10, 50)
        if search_type == "vector":
            rows = self._vector_rows(query_vector, over)
        elif search_type == "bm25":
            rows = self._bm25_rows(query, over)
        else:  # hybrid — reciprocal-rank fusion of the two legs
            rows = self._fuse(
                self._vector_rows(query_vector, over), self._bm25_rows(query, over)
            )
        return self._filter(
            rows, collection=collection, title=title, tags=tags, count=count
        )

    @staticmethod
    def _fuse(
        vec_rows: list[dict[str, Any]], bm_rows: list[dict[str, Any]], *, k: int = 60
    ) -> list[dict[str, Any]]:
        """Reciprocal-rank fusion: merge two ranked lists by 1/(k+rank),
        keyed on `chunk_id`. Version-independent (no LanceDB reranker)."""
        scored: dict[str, dict[str, Any]] = {}
        for rows in (vec_rows, bm_rows):
            for rank, r in enumerate(rows):
                entry = scored.setdefault(r["chunk_id"], {"row": r, "score": 0.0})
                entry["score"] += 1.0 / (k + rank + 1)
        ranked = sorted(scored.values(), key=lambda e: e["score"], reverse=True)
        return [e["row"] for e in ranked]

    def _modify_sync(
        self, *, title: str, collection: str | None,
        target_collection: str | None, target_title: str | None,
        tags: list[str] | None, description: str | None,
    ) -> int:
        docs = [
            d for d in self._scan_docs()
            if d["title"] == title
            and (collection is None or d["collection"] == collection)
        ]
        if not docs:
            return 0
        new_collection = target_collection
        new_title = target_title
        doc_vals: dict[str, Any] = {"updated_at": _now()}
        chunk_vals: dict[str, Any] = {}
        if new_collection is not None:
            doc_vals["collection"] = chunk_vals["collection"] = new_collection
        if new_title is not None:
            doc_vals["title"] = chunk_vals["title"] = new_title
        if tags is not None:
            doc_vals["tags"] = chunk_vals["tags"] = tags
        if description is not None:
            doc_vals["description"] = description
        for d in docs:
            where = f"doc_id = '{d['doc_id']}'"  # uuid hex — safe
            self._docs().update(where=where, values=doc_vals)
            if chunk_vals:
                self._chunks().update(where=where, values=chunk_vals)
        return len(docs)

    def _remove_sync(self, *, title: str, collection: str | None) -> int:
        docs = [
            d for d in self._scan_docs()
            if d["title"] == title
            and (collection is None or d["collection"] == collection)
        ]
        if not docs:
            return 0
        ids = ", ".join(f"'{d['doc_id']}'" for d in docs)  # uuid hex — safe
        self._chunks().delete(f"doc_id IN ({ids})")
        self._docs().delete(f"doc_id IN ({ids})")
        return len(docs)

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------

    async def find_by_sha256(self, sha256: str) -> str | None:
        docs = await asyncio.to_thread(self._scan_docs)
        for d in docs:
            if d["sha256"] == sha256:
                return d["doc_id"]
        return None

    async def store_document(
        self, *, collection: str, title: str, tags: list[str],
        description: str, sha256: str, source_name: str,
        chunks: list[tuple[int, str, list[float]]],
    ) -> str:
        doc_id = uuid.uuid4().hex
        await asyncio.to_thread(
            self._store_document_sync, doc_id=doc_id, collection=collection,
            title=title, tags=tags, description=description, sha256=sha256,
            source_name=source_name, chunks=chunks,
        )
        return doc_id

    async def list_documents(
        self, *, collection: str | None = None, tag: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        docs = await asyncio.to_thread(self._scan_docs)
        q = query.lower() if query else None
        out = []
        for d in docs:
            if collection is not None and d["collection"] != collection:
                continue
            if tag is not None and tag not in (d.get("tags") or []):
                continue
            if q is not None and q not in (
                f"{d['title']} {d['description']}".lower()
            ):
                continue
            out.append(d)
        return out

    async def retrieve(
        self, *, query: str, query_vector: list[float],
        search_type: str = "hybrid", collection: str | None = None,
        title: str | None = None, tags: list[str] | None = None,
        count: int = 3,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._retrieve_sync, query=query, query_vector=query_vector,
            search_type=search_type, collection=collection, title=title,
            tags=tags, count=count,
        )

    async def remove_document(
        self, *, title: str, collection: str | None = None
    ) -> int:
        return await asyncio.to_thread(
            self._remove_sync, title=title, collection=collection
        )

    async def modify_document(
        self, *, title: str, collection: str | None = None,
        target_collection: str | None = None, target_title: str | None = None,
        tags: list[str] | None = None, description: str | None = None,
    ) -> int:
        """Update metadata on the matching document(s) + their (denormalized)
        chunks. Returns the number of documents modified."""
        return await asyncio.to_thread(
            self._modify_sync, title=title, collection=collection,
            target_collection=target_collection, target_title=target_title,
            tags=tags, description=description,
        )
