"""history_summarizer core + the owner-side fold it feeds.

Two halves, both DB-free against the fake session store:

  * the summarizer reads a thread (and that thread's rolling summary) and
    returns the fold — it never applies it, and structurally cannot;
  * `common.thread.maybe_fold` is the caller that does, in ONE batch, and
    only when the built context is actually oversized.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bp_agents.agents.history_summarizer import (
    HISTORY_SUMMARIZER_AGENT_ID,
    SummarizeThread,
    run_summarize_thread,
)
from bp_agents.common.thread import SUMMARY_KEY, ThreadTurn, maybe_fold
from bp_sdk import LlmResponse, Message
from tests.fake_store import FakeHistory, FakeStore


class _StubLlm:
    def __init__(self, text: str) -> None:
        self.text = text
        self.captured: list[Message] | None = None

    async def generate(self, messages, **kw) -> LlmResponse:
        self.captured = list(messages)
        return LlmResponse(text=self.text, tool_calls=[])


def _ctx(store: FakeStore, owner: str, *, llm=None, peers=None):  # noqa: ANN001, ANN202
    return SimpleNamespace(
        user_id=store.user_id,
        session_id=store.session_id,
        llm=llm,
        peers=peers,
        history=FakeHistory(store, owner),
    )


# ---------------------------------------------------------------------------
# summarizer core
# ---------------------------------------------------------------------------


def test_summarize_thread_reads_another_agents_thread() -> None:
    """Reading another agent's thread is a first-class read; writing one is
    unrepresentable. That asymmetry is what lets the summarizer be useful
    without being trusted."""
    store = FakeStore()
    store.add("orchestrator", "user", "how do I reset it?")
    store.add("orchestrator", "assistant", "hold the button")
    llm = _StubLlm("THE SUMMARY")

    out = asyncio.run(
        run_summarize_thread(
            _ctx(store, HISTORY_SUMMARIZER_AGENT_ID, llm=llm),
            SummarizeThread(agent_id="orchestrator"),
        )
    )
    assert out.content == "THE SUMMARY"
    transcript = llm.captured[1].content
    assert "how do I reset it?" in transcript
    assert "hold the button" in transcript
    # It applied nothing: the orchestrator's thread and state are untouched.
    assert store.summary_of("orchestrator") is None
    assert store.roles("orchestrator") == ["user", "assistant"]


def test_summarize_thread_folds_in_the_previous_summary() -> None:
    store = FakeStore()
    store.thread_state[("orchestrator", "")] = {
        SUMMARY_KEY: __import__(
            "bp_protocol.frames", fromlist=["StateValue"]
        ).StateValue(key=SUMMARY_KEY, value="EARLIER", version=1)
    }
    store.add("orchestrator", "user", "and then?")
    llm = _StubLlm("MERGED")

    asyncio.run(
        run_summarize_thread(
            _ctx(store, HISTORY_SUMMARIZER_AGENT_ID, llm=llm),
            SummarizeThread(agent_id="orchestrator"),
        )
    )
    prompt = llm.captured[1].content
    assert "EARLIER" in prompt and "and then?" in prompt


def test_summarize_thread_honours_the_cutoff() -> None:
    """`up_to` is the last id to fold — an owner folding its oldest turns
    must not hand the summarizer the ones it is keeping."""
    store = FakeStore()
    old = store.add("orchestrator", "user", "OLD")
    store.add("orchestrator", "assistant", "KEPT")
    llm = _StubLlm("s")

    asyncio.run(
        run_summarize_thread(
            _ctx(store, HISTORY_SUMMARIZER_AGENT_ID, llm=llm),
            SummarizeThread(agent_id="orchestrator", up_to=old.id),
        )
    )
    prompt = llm.captured[1].content
    assert "OLD" in prompt and "KEPT" not in prompt


def test_empty_thread_preserves_the_existing_summary() -> None:
    """Returning '' would have the caller store "no summary" over a real
    one."""
    from bp_protocol.frames import StateValue

    store = FakeStore()
    store.thread_state[("orchestrator", "")] = {
        SUMMARY_KEY: StateValue(key=SUMMARY_KEY, value="KEEP ME", version=1)
    }
    llm = _StubLlm("should not be called")
    out = asyncio.run(
        run_summarize_thread(
            _ctx(store, HISTORY_SUMMARIZER_AGENT_ID, llm=llm),
            SummarizeThread(agent_id="orchestrator"),
        )
    )
    assert out.content == "KEEP ME"
    assert llm.captured is None


# ---------------------------------------------------------------------------
# the owner-side apply
# ---------------------------------------------------------------------------


class _SummarizerPeers:
    """`ctx.peers` that answers a summarize spawn with a canned fold."""

    def __init__(self, summary: str = "ROLLED UP") -> None:
        self.summary = summary
        self.spawns: list[tuple] = []

    async def spawn(self, dest, payload, *, mode=None, **kw):  # noqa: ANN001, ANN201
        self.spawns.append((dest, payload, mode))
        return SimpleNamespace(
            output=SimpleNamespace(content=self.summary, files=[])
        )


def _seeded(store: FakeStore, n: int) -> ThreadTurn:
    rows = [
        store.add("orchestrator", "user" if i % 2 == 0 else "assistant", f"turn {i} " + "x" * 400)
        for i in range(n)
    ]
    return ThreadTurn(
        agent_id="orchestrator", rows=rows, last_message_id=rows[-1].id
    )


def test_fold_writes_summary_and_floor_in_one_batch() -> None:
    """They are only correct together: a crash between them either re-folds
    those turns next pass or retires content the summary never got."""
    store = FakeStore()
    turn = _seeded(store, 10)
    peers = _SummarizerPeers()

    folded = asyncio.run(
        maybe_fold(
            _ctx(store, "orchestrator", peers=peers), turn,
            system="sys", limit_tokens=10,
        )
    )
    assert peers.spawns and peers.spawns[0][0] == HISTORY_SUMMARIZER_AGENT_ID
    cutoff = peers.spawns[0][1].up_to
    # ~70% folded, the rest kept.
    assert cutoff == turn.rows[6].id
    assert store.summary_of("orchestrator") == "ROLLED UP"
    assert store.floors[("orchestrator", "")] == cutoff
    assert folded.summary == "ROLLED UP"
    assert [r.id for r in folded.rows] == [r.id for r in turn.rows[7:]]


def test_fold_is_skipped_under_the_limit() -> None:
    store = FakeStore()
    turn = _seeded(store, 10)
    peers = _SummarizerPeers()
    out = asyncio.run(
        maybe_fold(
            _ctx(store, "orchestrator", peers=peers), turn,
            system="sys", limit_tokens=10_000_000,
        )
    )
    assert out is turn
    assert peers.spawns == []
    assert store.floors == {}


def test_fold_is_skipped_with_too_few_turns_to_compress() -> None:
    store = FakeStore()
    turn = _seeded(store, 3)
    peers = _SummarizerPeers()
    out = asyncio.run(
        maybe_fold(
            _ctx(store, "orchestrator", peers=peers), turn,
            system="sys", limit_tokens=1,
        )
    )
    assert out is turn and peers.spawns == []


def test_a_failed_fold_never_costs_the_user_their_answer() -> None:
    class _Broken:
        async def spawn(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("summarizer down")

    store = FakeStore()
    turn = _seeded(store, 10)
    out = asyncio.run(
        maybe_fold(
            _ctx(store, "orchestrator", peers=_Broken()), turn,
            system="sys", limit_tokens=10,
        )
    )
    assert out is turn
    assert store.floors == {}, "a failed fold must not move the floor"


def test_an_empty_summary_does_not_retire_the_thread() -> None:
    store = FakeStore()
    turn = _seeded(store, 10)
    out = asyncio.run(
        maybe_fold(
            _ctx(store, "orchestrator", peers=_SummarizerPeers("   ")), turn,
            system="sys", limit_tokens=10,
        )
    )
    assert out is turn
    assert store.floors == {}
