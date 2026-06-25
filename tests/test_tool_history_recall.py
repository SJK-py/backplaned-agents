"""Unit tests for on-demand tool-history recall
([agent-tool-history-recall.md]) — extraction, paging glue, rendering,
and the local tool's clamping. DB-free: the paging query is exercised
with a fake connection that emulates the `ORDER BY id DESC LIMIT` tail,
so the Python pairing/skip/limit slicing is what's under test.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from bp_agents.common import tool_history as th
from bp_agents.db import queries
from bp_sdk import Message

_T0 = datetime(2026, 1, 1, 12, 0, 0)


def _row(rid: int, role: str, message: str) -> dict:
    return {
        "id": rid,
        "session_id": "ses_1",
        "agent_id": "orchestrator",
        "role": role,
        "message": message,
        "created_at": _T0 + timedelta(seconds=rid),
        "incumbent": False,
        "hidden": True,
    }


class _FakeConn:
    """Emulates the `recent_tool_exchanges` query: returns the newest
    `limit` tool rows by id, ascending (mirrors the inner DESC+LIMIT,
    outer ASC SQL)."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetch(self, _sql: str, session_id, agent_id, fetch_rows):
        tool = [
            r for r in self._rows
            if r["session_id"] == session_id
            and r["agent_id"] == agent_id
            and r["role"] in ("tool_call", "tool_result")
        ]
        tool.sort(key=lambda r: r["id"])
        return tool[-fetch_rows:]


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def test_extract_pairs_calls_with_results() -> None:
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content=[
            {"text": "searching"},
            {"function_call": {"id": "tc1", "name": "web_search", "args": {"q": "x"}}},
        ]),
        Message.tool_response(tool_call_id="tc1", name="web_search", response="hits"),
        Message(role="assistant", content="done"),
    ]
    ex = th.extract_tool_exchanges(msgs)
    assert len(ex) == 1
    assert ex[0].name == "web_search"
    assert ex[0].args == {"q": "x"}
    assert ex[0].result == "hits"


def test_extract_excludes_terminal_call_without_result() -> None:
    # A terminal tool (hand_off) is never dispatched → no `tool` message →
    # must not be extracted (it would have no result).
    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "h1", "name": "hand_off", "args": {"agent_id": "research"}}},
        ]),
    ]
    assert th.extract_tool_exchanges(msgs) == []


def test_extract_flattens_file_ref_result() -> None:
    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "tc1", "name": "read_file", "args": {"name": "a.pdf"}}},
        ]),
        Message.tool_response(
            tool_call_id="tc1", name="read_file",
            response=[{"text": "here"}, {"file_ref": {"name": "a.pdf"}}],
        ),
    ]
    ex = th.extract_tool_exchanges(msgs)
    assert len(ex) == 1
    assert "here" in ex[0].result
    assert "a.pdf" in ex[0].result and "read_file" in ex[0].result


# --------------------------------------------------------------------------
# paging query (pairing + skip + limit)
# --------------------------------------------------------------------------

def _seed_exchanges(n: int) -> list[dict]:
    """n exchanges, ids 1..2n, exchange k = call(2k-1)/result(2k)."""
    rows: list[dict] = []
    for k in range(1, n + 1):
        rows.append(_row(2 * k - 1, "tool_call",
                         f'{{"name": "t{k}", "args": {{"i": {k}}}}}'))
        rows.append(_row(2 * k, "tool_result", f"result-{k}"))
    return rows


def test_paging_newest_first_and_skip() -> None:
    async def _drive() -> None:
        conn = _FakeConn(_seed_exchanges(5))  # exchanges 1..5 (5 newest)

        # newest one
        page = await queries.recent_tool_exchanges(
            conn, session_id="ses_1", agent_id="orchestrator", limit=1
        )
        assert len(page) == 1
        assert page[0][1].message == "result-5"

        # skip=1 → next older, no overlap
        page = await queries.recent_tool_exchanges(
            conn, session_id="ses_1", agent_id="orchestrator", limit=1, skip=1
        )
        assert len(page) == 1 and page[0][1].message == "result-4"

        # a 2-wide page, ascending (newest last)
        page = await queries.recent_tool_exchanges(
            conn, session_id="ses_1", agent_id="orchestrator", limit=2, skip=1
        )
        assert [p[1].message for p in page] == ["result-3", "result-4"]

        # skip past the start → empty
        page = await queries.recent_tool_exchanges(
            conn, session_id="ses_1", agent_id="orchestrator", limit=2, skip=10
        )
        assert page == []

    asyncio.run(_drive())


def test_paging_scopes_to_thread() -> None:
    async def _drive() -> None:
        rows = _seed_exchanges(2)
        # a different agent's exchange must not leak in
        other = _row(99, "tool_call", '{"name": "leak", "args": {}}')
        other["agent_id"] = "computer_use"
        other2 = _row(100, "tool_result", "LEAK")
        other2["agent_id"] = "computer_use"
        conn = _FakeConn(rows + [other, other2])
        page = await queries.recent_tool_exchanges(
            conn, session_id="ses_1", agent_id="orchestrator", limit=10
        )
        assert all("LEAK" not in p[1].message for p in page)
        assert len(page) == 2

    asyncio.run(_drive())


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_render_labels_distance_back_skip_aware() -> None:
    async def _drive() -> None:
        conn = _FakeConn(_seed_exchanges(5))
        page = await queries.recent_tool_exchanges(
            conn, session_id="ses_1", agent_id="orchestrator", limit=2, skip=1
        )
        out = th.render_recall(page, skip=1)
        # newest returned is exchange 4 → 2 back (skip 1 + 1); older is 3 back
        assert "[2 exchanges back] t4(" in out
        assert "[3 exchanges back] t3(" in out
        assert "result-4" in out and "result-3" in out

    asyncio.run(_drive())


def test_render_truncates_large_result() -> None:
    big = _row(1, "tool_call", '{"name": "t", "args": {}}')
    res = _row(2, "tool_result", "Z" * (th.PER_RESULT_CHARS + 500))

    class _Row:
        def __init__(self, d): self.message = d["message"]

    out = th.render_recall([(_Row(big), _Row(res))], skip=0)
    assert "more chars)" in out
    assert len(out) < th.PER_RESULT_CHARS + 300


# --------------------------------------------------------------------------
# the local tool — clamping + empty-state messaging
# --------------------------------------------------------------------------

def test_tool_clamps_count_and_skip() -> None:
    async def _drive() -> None:
        captured = {}

        async def _stub(conn, *, session_id, agent_id, limit, skip):
            captured["limit"] = limit
            captured["skip"] = skip
            return []

        orig = queries.recent_tool_exchanges
        queries.recent_tool_exchanges = _stub
        try:
            tool = th.make_recall_tool_history_tool(
                _FakePool(_FakeConn([])), session_id="ses_1", agent_id="orchestrator"
            )
            # over-cap count, negative skip
            msg = await tool.handler(None, {"count": 999, "skip": -3})
            assert captured["limit"] == th.MAX_RECALL
            assert captured["skip"] == 0
            assert "No earlier tool calls" in msg

            # skip>0 empty → "older" wording
            await tool.handler(None, {"count": 1, "skip": 2})
            msg2 = await tool.handler(None, {"skip": 2})
            assert "older" in msg2.lower()

            # non-int args fall back to defaults, not crash
            await tool.handler(None, {"count": "abc"})
            assert captured["limit"] == 1
        finally:
            queries.recent_tool_exchanges = orig

    asyncio.run(_drive())
