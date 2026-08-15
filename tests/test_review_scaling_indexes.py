"""Scaling quick-wins: index-backed reads/sweeps (perf pass ④).

④ registration_attempts — the hourly GC `DELETE WHERE attempted_at < cutoff`
   filtered on the 3rd column of window_idx (not a leftmost prefix) → full
   scan. A dedicated (attempted_at) index now backs it.

Pass ③ covered the webapp's per-user `session_info` list. That table is gone:
the session list is the ROUTER's (`GET /v1/sessions`), so the index and the
bounded-read pin moved with it and are the router's to keep.

These assert against the LIVE schema (authoritative) + the query text.
"""

from __future__ import annotations

import asyncio

import asyncpg


def _index_defs(conn_url: str, table: str):  # type: ignore[no-untyped-def]
    async def _go() -> dict[str, str]:
        conn = await asyncpg.connect(conn_url)
        try:
            rows = await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = $1",
                table,
            )
            return {r["indexname"]: r["indexdef"] for r in rows}
        finally:
            await conn.close()

    return asyncio.run(_go())


# --- ④ registration_attempts GC --------------------------------------------


def test_registration_attempts_has_gc_index(test_db_url: str) -> None:
    defs = _index_defs(test_db_url, "registration_attempts")
    assert "registration_attempts_gc_idx" in defs, (
        "the hourly GC delete needs a dedicated (attempted_at) index, else it "
        "full-scans (attempted_at is not a leftmost prefix of window_idx)"
    )
    assert "attempted_at" in defs["registration_attempts_gc_idx"]


def test_gc_delete_uses_the_index_not_seq_scan(test_db_url: str) -> None:
    """EXPLAIN the GC delete: it must use an Index Scan, not a Seq Scan."""
    async def _go() -> str:
        conn = await asyncpg.connect(test_db_url)
        try:
            rows = await conn.fetch(
                "EXPLAIN DELETE FROM registration_attempts "
                "WHERE attempted_at < now() - interval '30 days'"
            )
            return "\n".join(r["QUERY PLAN"] for r in rows)
        finally:
            await conn.close()

    plan = asyncio.run(_go())
    assert "registration_attempts_gc_idx" in plan, plan
    assert "Seq Scan" not in plan, f"GC delete should not seq-scan:\n{plan}"
