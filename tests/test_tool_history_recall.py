"""Unit tests for on-demand tool-history recall
([agent-tool-history-recall.md]) — extraction, paging, rendering, and the
local tool's clamping, over the fake session store.

Recall reads with `include_retired=True`: a summarization fold moves the
thread's floor past old turns, and their tool detail is exactly what the
model needs recall for once the prose has been compressed away.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bp_agents.common import tool_history as th
from bp_sdk import Message
from tests.fake_store import FakeHistory, FakeStore


def _ctx(store: FakeStore, owner: str = "orchestrator"):  # noqa: ANN202
    return SimpleNamespace(
        user_id=store.user_id, session_id=store.session_id,
        history=FakeHistory(store, owner),
    )


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
# recall is not self-amplifying: its own exchange stores a marker
# --------------------------------------------------------------------------

def test_recall_exchange_stored_as_marker_not_digest() -> None:
    digest = (
        "[2 exchanges back] web_search({}) →\n" + "H" * 5000 + "\n\n"
        "[1 exchange back] read_file({}) →\nbody"
    )
    ex = th.ToolExchange(name="recall_tool_history", args={"count": 2}, result=digest)
    stored = th.storable_result(ex)
    assert "recalled 2 earlier tool exchanges" in stored
    assert "H" * 50 not in stored  # the digest body is NOT re-stored
    assert len(stored) < 200

    # a normal tool keeps its real result
    other = th.ToolExchange(name="web_search", args={}, result="hits-and-more")
    assert th.storable_result(other) == "hits-and-more"

    # empty recall → "nothing matched", not a count
    empty = th.ToolExchange(
        name="recall_tool_history", args={},
        result="No earlier tool calls in this conversation to recall.",
    )
    assert "nothing matched" in th.storable_result(empty)


# --------------------------------------------------------------------------
# paging (pairing + skip + limit)
# --------------------------------------------------------------------------


def _seed_exchanges(store: FakeStore, n: int, *, owner: str = "orchestrator") -> None:
    """n exchanges on `owner`'s thread; exchange k = call then result."""
    for k in range(1, n + 1):
        store.add(owner, "tool_call", f'{{"name": "t{k}", "args": {{"i": {k}}}}}',
                  hidden=True)
        store.add(owner, "tool_result", f"result-{k}", hidden=True)


async def _recall(store: FakeStore, **args) -> str:
    tool = th.make_recall_tool_history_tool(agent_id="orchestrator")
    return await tool.handler(_ctx(store), args)


def test_paging_newest_first_and_skip() -> None:
    async def _drive() -> None:
        store = FakeStore()
        _seed_exchanges(store, 5)

        assert "result-5" in await _recall(store, count=1)
        # skip=1 → next older, no overlap
        page = await _recall(store, count=1, skip=1)
        assert "result-4" in page and "result-5" not in page
        # a 2-wide page carries both, newest last
        page = await _recall(store, count=2, skip=1)
        assert "result-3" in page and "result-4" in page
        # skip past the start → the empty-state message
        assert "reached the start" in await _recall(store, count=2, skip=10)

    asyncio.run(_drive())


def test_paging_scopes_to_the_callers_own_thread() -> None:
    """Structural, not checked: a `Read` with no owner defaults to the
    caller's own thread, and the router derives that from the task's active
    executor. There is no parameter through which another agent's thread
    could be named."""
    async def _drive() -> None:
        store = FakeStore()
        _seed_exchanges(store, 2)
        store.add("computer_use", "tool_call", '{"name": "leak", "args": {}}',
                  hidden=True)
        store.add("computer_use", "tool_result", "LEAK", hidden=True)
        out = await _recall(store, count=10)
        assert "LEAK" not in out
        assert "result-1" in out and "result-2" in out

    asyncio.run(_drive())


def test_recall_reaches_past_a_summarization_floor() -> None:
    """The point of recall: once a fold retires the prose, the tool detail
    behind it is what the model still needs."""
    async def _drive() -> None:
        store = FakeStore()
        _seed_exchanges(store, 2)
        store.floors[("orchestrator", "")] = store.messages[-1].id
        assert "result-2" in await _recall(store, count=2)

    asyncio.run(_drive())


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_render_labels_distance_back_skip_aware() -> None:
    async def _drive() -> None:
        store = FakeStore()
        _seed_exchanges(store, 5)
        out = await _recall(store, count=2, skip=1)
        # newest returned is exchange 4 → 2 back (skip 1 + 1); older is 3 back
        assert "[2 exchanges back] t4(" in out
        assert "[3 exchanges back] t3(" in out
        assert "result-4" in out and "result-3" in out

    asyncio.run(_drive())


def test_render_truncates_large_result() -> None:
    async def _drive() -> None:
        store = FakeStore()
        store.add("orchestrator", "tool_call", '{"name": "t", "args": {}}',
                  hidden=True)
        store.add("orchestrator", "tool_result", "Z" * (th.PER_RESULT_CHARS + 500),
                  hidden=True)
        out = await _recall(store, count=1)
        assert "more chars)" in out
        assert len(out) < th.PER_RESULT_CHARS + 300

    asyncio.run(_drive())


# --------------------------------------------------------------------------
# the local tool — clamping + empty-state messaging
# --------------------------------------------------------------------------


def test_tool_clamps_count_and_skip() -> None:
    async def _drive() -> None:
        store = FakeStore()
        _seed_exchanges(store, 20)

        # over-cap count is clamped to MAX_RECALL exchanges
        out = await _recall(store, count=999, skip=0)
        assert out.count("exchanges back]") + out.count("exchange back]") == th.MAX_RECALL
        # a negative skip is floored at 0 — the newest is still included
        assert "result-20" in await _recall(store, count=1, skip=-3)
        # non-int args fall back to the defaults rather than crashing
        assert "result-20" in await _recall(store, count="abc")

        empty = FakeStore()
        assert "No earlier tool calls" in await _recall(empty, count=1)
        assert "reached the start" in await _recall(empty, count=1, skip=2)

    asyncio.run(_drive())
