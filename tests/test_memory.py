"""memory agent — 4-phase add (NEW / UPDATE) + decay retrieve.

Scripted stub LLM (`generate` returns queued JSON; `embed` keyword
vectors) + a real MemoryStore on a tmp LanceDB. No router/provider.
"""

from __future__ import annotations

import asyncio
import json

from bp_agents.agents.memory import (
    MemAdd,
    MemRetrieve,
    run_memory_add,
    run_memory_retrieve,
)
from bp_agents.agents.memory.agent import (
    _extract_system,
    _now_line,
    gc_sweep,
    run_memory_delete,
    run_memory_list,
    run_memory_manual_add,
)
from bp_agents.common.payloads import MemDelete, MemList, MemManualAdd
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.lance import connect
from bp_agents.lance.memory import MemoryStore
from bp_agents.settings import SuiteSettings
from bp_sdk import LlmResponse  # noqa: E402

_DIM = 8


def _kw_vec(text: str) -> list[float]:
    v = [0.0] * _DIM
    t = text.lower()
    idx = 0 if "cat" in t else 1 if "dog" in t else 2 if "paris" in t else 3
    v[idx] = 1.0
    return v


class _ScriptLlm:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.generate_calls = 0

    async def generate(self, messages, **kw) -> LlmResponse:
        self.generate_calls += 1
        text = self._responses.pop(0) if self._responses else "{}"
        return LlmResponse(text=text, tool_calls=[])

    async def embed(self, texts, *, preset=None):
        return [_kw_vec(t) for t in texts]


class _Ctx:
    def __init__(self, llm, user_id="usr_a") -> None:
        self.llm = llm
        self.user_id = user_id


def _settings() -> SuiteSettings:
    return SuiteSettings(embedding_dim=_DIM)


def test_memory_add_new_fact(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        llm = _ScriptLlm([
            '{"facts": [{"fact": "likes cats", "kind": "preference"}]}',
            '{"action": "NEW", "related": []}',
        ])
        await run_memory_add(
            _Ctx(llm), MemAdd(user_prompt="i love cats", assistant_response="noted"),
            settings=_settings(), store=store, lite_preset="l", embed_preset="e",
        )
        facts = await store.all_facts()
        assert [f["fact"] for f in facts] == ["likes cats"]

    asyncio.run(_drive())


def test_memory_add_updates_existing(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_u"), embedding_dim=_DIM)
        f0 = await store.insert_fact(
            fact="likes cats", kind="preference", embedding=_kw_vec("cats")
        )
        llm = _ScriptLlm([
            '{"facts": [{"fact": "likes cats and dogs", "kind": "preference"}]}',
            '{"action": "UPDATE", "fact_number": 1, "content": "likes cats and dogs"}',
        ])
        await run_memory_add(
            _Ctx(llm, "usr_u"),
            MemAdd(user_prompt="also dogs now", assistant_response="ok"),
            settings=_settings(), store=store, lite_preset="l", embed_preset="e",
        )
        facts = await store.all_facts()
        assert len(facts) == 1
        assert facts[0]["uid"] == f0
        assert facts[0]["fact"] == "likes cats and dogs"

    asyncio.run(_drive())


def test_memory_add_extracts_nothing(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_n"), embedding_dim=_DIM)
        llm = _ScriptLlm(['{"facts": []}'])
        await run_memory_add(
            _Ctx(llm, "usr_n"),
            MemAdd(user_prompt="hi", assistant_response="hello"),
            settings=_settings(), store=store, lite_preset="l", embed_preset="e",
        )
        assert await store.all_facts() == []
        assert llm.generate_calls == 1  # only the extract call

    asyncio.run(_drive())


def test_memory_retrieve_with_graph_expansion(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_r"), embedding_dim=_DIM)
        f0 = await store.insert_fact(
            fact="likes cats", kind="preference", embedding=_kw_vec("cats")
        )
        f1 = await store.insert_fact(
            fact="lives in Paris", kind="personal_info", embedding=_kw_vec("paris")
        )
        await store.add_edge(f0, f1)

        out = await run_memory_retrieve(
            _Ctx(_ScriptLlm([]), "usr_r"),
            MemRetrieve(query="cats", count=1, child_count=1),
            settings=_settings(), store=store, embed_preset="e",
        )
        # Top hit (cats) + its graph neighbour (Paris) via expansion.
        assert "likes cats" in out.content
        assert "lives in Paris" in out.content

    asyncio.run(_drive())


class _FixedEmbedLlm:
    """Embeds every text to the same (fact-orthogonal) vector, so the vector
    leg ties and the BM25 leg breaks it — proving hybrid fusion is live."""

    async def generate(self, messages, **kw) -> LlmResponse:
        return LlmResponse(text="{}", tool_calls=[])

    async def embed(self, texts, *, preset=None):
        return [[0.0] * (_DIM - 1) + [1.0] for _ in texts]


def test_memory_retrieve_hybrid_bm25_leg(tmp_path) -> None:
    async def _drive() -> None:
        store = MemoryStore(await connect(tmp_path, "usr_h"), embedding_dim=_DIM)
        await store.insert_fact(
            fact="likes cats", kind="preference", embedding=_kw_vec("cats")
        )
        await store.insert_fact(
            fact="rides a bicycle", kind="event", embedding=_kw_vec("dog")
        )
        out = await run_memory_retrieve(
            _Ctx(_FixedEmbedLlm(), "usr_h"),
            MemRetrieve(query="bicycle", count=1, child_count=0),
            settings=_settings(), store=store, embed_preset="e",
        )
        # The vector leg can't distinguish the two facts; BM25 surfaces the
        # keyword match as the top result.
        assert "bicycle" in out.content
        assert "cats" not in out.content

    asyncio.run(_drive())


def test_memory_gc_sweep(suite_db_url: str, tmp_path) -> None:
    """The background sweep GCs every user with an existing fact graph,
    keyed off `user_config`. A user with no LanceDB dir is skipped."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE TABLE session_history, session_info, user_config, "
                    "suite_platform_mappings RESTART IDENTITY"
                )
                # One user has a store; the other never materialised one.
                await queries.create_user_config(
                    conn, user_id="usr_gc", default_session_id="s1"
                )
                await queries.create_user_config(
                    conn, user_id="usr_none", default_session_id="s2"
                )
            store = MemoryStore(await connect(tmp_path, "usr_gc"), embedding_dim=_DIM)
            await store.insert_fact(
                fact="old fact", kind="event", embedding=_kw_vec("cats")
            )
            # Age it past any horizon so the sweep collects it.
            await asyncio.to_thread(
                lambda: store._facts().update(
                    where="uid != ''", values={"last_used_at": "2000-01-01T00:00:00+00:00"}
                )
            )
            assert len(await store.all_facts()) == 1

            settings = SuiteSettings(
                database_url=suite_db_url, embedding_dim=_DIM,
                lance_root=str(tmp_path), memory_gc_horizon_days=1,
            )
            swept = await gc_sweep(pool, settings)
            assert swept == 1
            assert await store.all_facts() == []
        finally:
            await pool.close()

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Relative → absolute time in extraction
# ---------------------------------------------------------------------------


def test_now_line_has_date_weekday_and_tz_with_utc_fallback() -> None:
    import datetime as _dt

    today = _dt.datetime.now(_dt.UTC)
    line = _now_line("UTC")
    assert today.strftime("%Y-%m-%d") in line
    assert today.strftime("%A") in line  # day of week present
    assert "UTC" in line
    assert "UTC" in _now_line("Not/AZone")  # unknown tz → UTC


def test_extract_system_instructs_absolute_time_conversion() -> None:
    sys = _extract_system("2025-06-06 09:00 UTC (Friday)")
    assert "The current time is 2025-06-06 09:00 UTC (Friday)." in sys
    assert "ABSOLUTE" in sys
    assert "YYYY-MM-DD HH:MM" in sys
    assert "next Monday" in sys  # the worked example is present


def test_extract_system_allows_multiple_facts_without_over_fragmenting() -> None:
    sys = _extract_system("2025-06-06 09:00 UTC (Friday)")
    # Explicitly allows a turn to yield a LIST of several facts...
    assert "SEVERAL distinct facts" in sys
    assert "one list item per fact" in sys
    # ...while guarding against shattering one fact into trivia...
    assert "over-fragment" in sys
    # ...anchored by a worked multi-fact example.
    assert "Is vegetarian" in sys
    assert "Wife's name is Jenna" in sys


class _CaptureLlm(_ScriptLlm):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.systems: list[str] = []

    async def generate(self, messages, **kw) -> LlmResponse:
        self.systems.append(messages[0].content)
        return await super().generate(messages, **kw)


def test_extract_prompt_carries_current_time(tmp_path) -> None:
    import datetime as _dt

    async def _drive() -> str:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        llm = _CaptureLlm(['{"facts": []}'])
        await run_memory_add(
            _Ctx(llm),
            MemAdd(user_prompt="ship it next monday 9am", assistant_response="ok"),
            settings=_settings(), store=store, lite_preset="l", embed_preset="e",
        )
        return llm.systems[0]

    system = asyncio.run(_drive())
    assert "The current time is" in system
    assert _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d") in system  # today, UTC
    assert "ABSOLUTE" in system


# ---------------------------------------------------------------------------
# Webapp Memory page modes — list / delete / manual_add
# ---------------------------------------------------------------------------


async def _seed_facts(store) -> dict[str, str]:
    """Insert three facts directly; return name→uid."""
    uids = {}
    for name, kind in [("likes cats", "preference"),
                       ("has a dog", "preference"),
                       ("lives in paris", "personal_info")]:
        uids[name] = await store.insert_fact(
            fact=name, kind=kind, embedding=_kw_vec(name)
        )
    return uids


def test_memory_list_no_query_returns_all_with_kind_filter(tmp_path) -> None:
    async def _drive() -> tuple[dict, dict]:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        await _seed_facts(store)
        allout = await run_memory_list(
            _Ctx(_ScriptLlm([])), MemList(), settings=_settings(),
            store=store, embed_preset="e",
        )
        prefout = await run_memory_list(
            _Ctx(_ScriptLlm([])), MemList(kind="preference"),
            settings=_settings(), store=store, embed_preset="e",
        )
        return json.loads(allout.content), json.loads(prefout.content)

    allres, prefres = asyncio.run(_drive())
    assert allres["total"] == 3 and len(allres["items"]) == 3
    assert {i["fact"] for i in prefres["items"]} == {"likes cats", "has a dog"}
    assert all("score" not in i for i in allres["items"])  # no query → no score


def test_memory_list_kind_filter_is_case_insensitive(tmp_path) -> None:
    """The `kind` filter matches regardless of case — seeded `preference`
    facts are found via `Preference`."""
    async def _drive() -> dict:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        await _seed_facts(store)
        out = await run_memory_list(
            _Ctx(_ScriptLlm([])), MemList(kind="Preference"),
            settings=_settings(), store=store, embed_preset="e",
        )
        return json.loads(out.content)

    res = asyncio.run(_drive())
    assert {i["fact"] for i in res["items"]} == {"likes cats", "has a dog"}


def test_memory_list_query_ranks_by_relevance(tmp_path) -> None:
    async def _drive() -> dict:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        await _seed_facts(store)
        out = await run_memory_list(
            _Ctx(_ScriptLlm([])), MemList(query="cat"),
            settings=_settings(), store=store, embed_preset="e",
        )
        return json.loads(out.content)

    res = asyncio.run(_drive())
    assert res["items"], "query returned no items"
    assert res["items"][0]["fact"] == "likes cats"  # cat vector ranks first
    assert "score" in res["items"][0]


def test_memory_list_paging_caps_at_50(tmp_path) -> None:
    async def _drive() -> int:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        for i in range(60):
            await store.insert_fact(fact=f"f{i}", kind="event", embedding=_kw_vec("x"))
        out = await run_memory_list(
            _Ctx(_ScriptLlm([])), MemList(start=0, end=999),
            settings=_settings(), store=store, embed_preset="e",
        )
        return len(json.loads(out.content)["items"])

    assert asyncio.run(_drive()) == 50  # end clamped to start + MAX_PAGE


def test_memory_delete_removes_fact(tmp_path) -> None:
    async def _drive() -> tuple[dict, int]:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        uids = await _seed_facts(store)
        out = await run_memory_delete(
            _Ctx(_ScriptLlm([])), MemDelete(uid=uids["has a dog"]),
            settings=_settings(), store=store,
        )
        return json.loads(out.content), len(await store.all_facts())

    res, remaining = asyncio.run(_drive())
    assert res["deleted"] is True and remaining == 2


def test_memory_manual_add_bypasses_extraction(tmp_path) -> None:
    async def _drive() -> tuple[dict, list]:
        store = MemoryStore(await connect(tmp_path, "usr_a"), embedding_dim=_DIM)
        # Only a reconcile decision is needed (no extraction call).
        llm = _ScriptLlm(['{"action": "NEW", "related": []}'])
        out = await run_memory_manual_add(
            _Ctx(llm), MemManualAdd(fact="allergic to peanuts", kind="personal_info"),
            settings=_settings(), store=store, lite_preset="l", embed_preset="e",
        )
        return json.loads(out.content), await store.all_facts()

    res, facts = asyncio.run(_drive())
    assert res["added"] is True
    assert [f["fact"] for f in facts] == ["allergic to peanuts"]
