"""Audit MED-3: hash-chain predecessor picked by a monotonic key.

`append_audit_event` linked each row to `sha256(prev_hash + body)`,
choosing `prev` via `ORDER BY ts DESC, event_id DESC`. `event_id`
is a RANDOM `gen_random_uuid()` and `ts` is wall-clock
(non-monotonic under an NTP step, equal at microsecond resolution
within a burst). The advisory lock serialises the append but cannot
fix a non-insertion-ordered head pick: a later append could select
the wrong predecessor → the chain forks and any linear
tamper-evidence verification breaks permanently.

Fix: `audit_log.seq bigserial` (assigned at INSERT under the same
advisory lock → strictly monotonic in chain order) and
`ORDER BY seq DESC LIMIT 1`.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest


class _FakeConn:
    """Captures the SQL append_audit_event issues + the INSERT args."""

    def __init__(self, prev_row: dict | None) -> None:
        self._prev_row = prev_row
        self.fetchrow_sql: list[str] = []
        self.execute_sql: list[tuple[str, tuple]] = []

    def transaction(self):  # type: ignore[no-untyped-def]
        class _Txn:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *a: Any) -> bool:
                return False

        return _Txn()

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_sql.append((sql, args))
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        self.fetchrow_sql.append(sql)
        return self._prev_row


def _append(conn: _FakeConn) -> None:
    from bp_router.db import queries

    asyncio.run(
        queries.append_audit_event(
            conn,  # type: ignore[arg-type]
            actor_kind="admin",
            actor_id="usr_admin",
            event="thing.happened",
            target_kind="user",
            target_id="usr_bob",
            payload={"k": "v"},
        )
    )


def _insert_args(conn: _FakeConn) -> tuple:
    for sql, args in conn.execute_sql:
        if "INSERT INTO audit_log" in sql:
            return args
    raise AssertionError("no INSERT INTO audit_log issued")


# ---------------------------------------------------------------------------
# Behavioural — predecessor selected by seq, chain links from it
# ---------------------------------------------------------------------------


def test_prev_selected_by_seq_desc_not_ts_or_event_id() -> None:
    pytest.importorskip("asyncpg")
    conn = _FakeConn(prev_row={"self_hash": "PREVHASH"})
    _append(conn)

    sel = next(s for s in conn.fetchrow_sql if "FROM audit_log" in s)
    assert "ORDER BY seq DESC" in sel
    assert "ts DESC" not in sel
    assert "event_id DESC" not in sel


def test_new_row_chains_off_the_seq_selected_predecessor() -> None:
    """prev_hash of the inserted row == self_hash of the row the
    seq-ordered SELECT returned (positional arg 8: ts, actor_kind,
    actor_id, event, target_kind, target_id, payload, prev_hash,
    self_hash)."""
    pytest.importorskip("asyncpg")
    conn = _FakeConn(prev_row={"self_hash": "PREVHASH"})
    _append(conn)

    args = _insert_args(conn)  # (ts, ak, aid, ev, tk, tid, payload, prev, self)
    assert args[7] == "PREVHASH"      # prev_hash
    assert isinstance(args[8], str) and len(args[8]) == 64  # self_hash sha256


def test_genesis_when_no_predecessor() -> None:
    """Empty table → prev row None → prev_hash inserted as None
    (genesis). The advisory-lock double-genesis guard is unchanged."""
    pytest.importorskip("asyncpg")
    conn = _FakeConn(prev_row=None)
    _append(conn)

    # Advisory lock still acquired before the predecessor read.
    assert any(
        "pg_advisory_xact_lock" in sql for sql, _ in conn.execute_sql
    )
    args = _insert_args(conn)
    assert args[7] is None  # genesis prev_hash


# ---------------------------------------------------------------------------
# Source / schema pins
# ---------------------------------------------------------------------------


def test_append_audit_event_orders_by_seq() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.append_audit_event)
    assert "ORDER BY seq DESC" in src
    assert "ORDER BY ts DESC, event_id DESC" not in src


def test_consolidated_schema_has_seq_bigserial_and_unique_index() -> None:
    body = (
        Path(__file__).parent.parent
        / "bp_router" / "db" / "migrations" / "versions"
        / "0001_initial_schema.py"
    ).read_text()
    audit_ddl = body[body.index("CREATE TABLE audit_log"):body.index(
        "audit_log_ts_idx"
    )]
    assert "seq" in audit_ddl and "bigserial" in audit_ddl
    assert (
        "CREATE UNIQUE INDEX audit_log_seq_idx ON audit_log(seq)" in body
    )


def test_audit_log_row_model_has_seq() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.models import AuditLogRow

    assert "seq" in AuditLogRow.model_fields
