"""Scaling quick-wins: index-backed reads/sweeps (perf pass ③ + ④).

③ session_info — the webapp list ordered by created_at DESC on a (user_id)-only
   index (sort every render) with no LIMIT (unbounded read for heavy users).
   Now a (user_id, created_at DESC) index serves it as a bounded top-N.
④ registration_attempts — the hourly GC `DELETE WHERE attempted_at < cutoff`
   filtered on the 3rd column of window_idx (not a leftmost prefix) → full
   scan. A dedicated (attempted_at) index now backs it.

These assert against the LIVE schema (authoritative) + the query text.
"""

from __future__ import annotations

import asyncio
import inspect

import asyncpg

from bp_agents.db import queries as suite_queries


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


# --- ③ session_info ---------------------------------------------------------


def test_session_info_index_is_composite_with_created_at(suite_db_url: str) -> None:
    defs = _index_defs(suite_db_url, "session_info")
    idx = defs.get("session_info_user_idx", "")
    assert "user_id" in idx and "created_at" in idx, (
        f"session_info_user_idx must cover (user_id, created_at DESC); got {idx!r}"
    )


def test_session_info_list_is_bounded() -> None:
    src = inspect.getsource(suite_queries.list_session_info_for_user)
    assert "LIMIT" in src, "the per-user session_info read must be bounded"
    assert "ORDER BY created_at DESC" in src


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
