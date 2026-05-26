"""bp_agents.lance.memory — per-user fact-graph store.

`facts` + `edges` tables ([data-model.md] §2.2, [memory.md]). Relations
are an **edge-set** of deduped unordered pairs stored with `uid_a <
uid_b`, so bidirectionality and remove-cascade are free. Pure data layer:
embeddings are passed in; the LLM pipeline + per-user lock live in the
agent. Sync LanceDB calls run in `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa

_DEGREE_CAP = 10


def _facts_schema(dim: int) -> pa.Schema:
    return pa.schema([
        ("uid", pa.string()),
        ("fact", pa.string()),
        ("kind", pa.string()),
        ("created_at", pa.string()),
        ("last_used_at", pa.string()),
        ("embedding", pa.list_(pa.float32(), dim)),
    ])


def _edges_schema() -> pa.Schema:
    return pa.schema([
        ("uid_a", pa.string()),
        ("uid_b", pa.string()),
        ("created_at", pa.string()),
    ])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class MemoryStore:
    """Per-user fact graph over one LanceDB connection."""

    def __init__(self, db: Any, *, embedding_dim: int) -> None:
        self._db = db
        self._dim = embedding_dim

    def _facts(self) -> Any:
        return self._db.create_table(
            "facts", schema=_facts_schema(self._dim), exist_ok=True
        )

    def _edges(self) -> Any:
        return self._db.create_table(
            "edges", schema=_edges_schema(), exist_ok=True
        )

    # ------------------------------------------------------------------
    # Sync cores
    # ------------------------------------------------------------------

    def _all_facts_sync(self) -> list[dict[str, Any]]:
        return self._facts().search().limit(1_000_000).to_list()

    def _all_edges_sync(self) -> list[dict[str, Any]]:
        return self._edges().search().limit(1_000_000).to_list()

    def _insert_fact_sync(
        self, *, uid: str, fact: str, kind: str, embedding: list[float],
        created_at: str,
    ) -> None:
        now = _now()
        self._facts().add([{
            "uid": uid, "fact": fact, "kind": kind,
            "created_at": created_at, "last_used_at": now,
            "embedding": embedding,
        }])

    def _delete_facts_sync(self, uids: list[str]) -> None:
        if not uids:
            return
        ids = ", ".join(f"'{u}'" for u in uids)  # uuid hex — safe
        self._facts().delete(f"uid IN ({ids})")

    def _delete_edges_for_sync(self, uids: list[str]) -> None:
        if not uids:
            return
        ids = ", ".join(f"'{u}'" for u in uids)
        self._edges().delete(f"uid_a IN ({ids}) OR uid_b IN ({ids})")

    def _search_sync(
        self, query_vector: list[float], limit: int
    ) -> list[dict[str, Any]]:
        return self._facts().search(query_vector).limit(limit).to_list()

    def _touch_sync(self, uids: list[str]) -> None:
        if not uids:
            return
        ids = ", ".join(f"'{u}'" for u in uids)
        self._facts().update(where=f"uid IN ({ids})", values={"last_used_at": _now()})

    def _add_edge_sync(self, a: str, b: str) -> None:
        if a == b:
            return
        ua, ub = _pair(a, b)
        edges = self._all_edges_sync()
        if any(e["uid_a"] == ua and e["uid_b"] == ub for e in edges):
            return
        # Degree cap: evict the oldest edge of any endpoint already at cap.
        for endpoint in (ua, ub):
            incident = sorted(
                (e for e in edges if endpoint in (e["uid_a"], e["uid_b"])),
                key=lambda e: e["created_at"],
            )
            if len(incident) >= _DEGREE_CAP:
                oldest = incident[0]
                self._edges().delete(
                    f"uid_a = '{oldest['uid_a']}' AND uid_b = '{oldest['uid_b']}'"
                )
        self._edges().add([{"uid_a": ua, "uid_b": ub, "created_at": _now()}])

    def _remove_edge_sync(self, a: str, b: str) -> None:
        ua, ub = _pair(a, b)
        self._edges().delete(f"uid_a = '{ua}' AND uid_b = '{ub}'")

    def _gc_sync(self, horizon_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=horizon_days)).isoformat()
        stale = [
            f["uid"] for f in self._all_facts_sync()
            if f["last_used_at"] < cutoff
        ]
        if stale:
            self._delete_facts_sync(stale)
            self._delete_edges_for_sync(stale)
        return len(stale)

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------

    async def insert_fact(
        self, *, fact: str, kind: str, embedding: list[float]
    ) -> str:
        uid = uuid.uuid4().hex
        await asyncio.to_thread(
            self._insert_fact_sync, uid=uid, fact=fact, kind=kind,
            embedding=embedding, created_at=_now(),
        )
        return uid

    async def update_fact(
        self, *, uid: str, fact: str, embedding: list[float]
    ) -> None:
        """Rewrite a fact's content/embedding, preserving its uid + edges
        (delete the fact row only, re-insert with the same uid)."""
        existing = await self.get_fact(uid)
        if existing is None:
            return
        await asyncio.to_thread(self._delete_facts_sync, [uid])
        await asyncio.to_thread(
            self._insert_fact_sync, uid=uid, fact=fact,
            kind=existing["kind"], embedding=embedding,
            created_at=existing["created_at"],
        )

    async def remove_fact(self, uid: str) -> None:
        """Delete a fact and cascade its edges (both endpoints)."""
        await asyncio.to_thread(self._delete_facts_sync, [uid])
        await asyncio.to_thread(self._delete_edges_for_sync, [uid])

    async def get_fact(self, uid: str) -> dict[str, Any] | None:
        facts = await asyncio.to_thread(self._all_facts_sync)
        return next((f for f in facts if f["uid"] == uid), None)

    async def all_facts(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._all_facts_sync)

    async def search(
        self, *, query_vector: list[float], limit: int
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._search_sync, query_vector, limit)

    async def neighbors(self, uid: str) -> list[str]:
        edges = await asyncio.to_thread(self._all_edges_sync)
        out: list[str] = []
        for e in edges:
            if e["uid_a"] == uid:
                out.append(e["uid_b"])
            elif e["uid_b"] == uid:
                out.append(e["uid_a"])
        return out

    async def add_edge(self, a: str, b: str) -> None:
        await asyncio.to_thread(self._add_edge_sync, a, b)

    async def remove_edge(self, a: str, b: str) -> None:
        await asyncio.to_thread(self._remove_edge_sync, a, b)

    async def touch(self, uids: list[str]) -> None:
        await asyncio.to_thread(self._touch_sync, uids)

    async def gc(self, *, horizon_days: int = 100) -> int:
        return await asyncio.to_thread(self._gc_sync, horizon_days)
