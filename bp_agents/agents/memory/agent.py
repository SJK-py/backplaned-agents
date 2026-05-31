"""memory agent — per-user fact graph (4-phase add + decay retrieve).

`add` (fire-and-forget) runs under a per-user lock ([memory.md] §1):
  1. extract durable facts from the turn,
  2. reconcile each against the top-N similar facts (NEW / UPDATE / REMOVE
     + relate edges),
  3. relate-out propagation (1-hop, best-effort),
  4. update propagation (1-hop, best-effort).
`retrieve` is lock-free: hybrid pool → recency-decay re-rank → graph
expansion → refresh last_used.

LLM structured decisions are JSON-in-text, parsed leniently; enumeration
numbers are mapped to uids per phase and out-of-range numbers dropped
(LLM index drift must not corrupt the graph). Phases 3–4 are enhancement
and wrapped best-effort.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bp_agents.common import text_output
from bp_agents.common.payloads import (
    MAX_PAGE,
    MemAdd,
    MemDelete,
    MemList,
    MemManualAdd,
    MemRetrieve,
)
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.lance import connect
from bp_agents.lance.base import user_db_path
from bp_agents.lance.memory import MemoryStore
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

MEMORY_AGENT_ID = "memory"
_MAX_FACTS_PER_TURN = 10
_VALID_KINDS = {"preference", "personal_info", "event", "project"}

_EXTRACT_INSTRUCTIONS = """\
Extract durable facts about the user worth remembering long-term from this \
turn — preferences, personal info, ongoing events, projects. Ignore \
small talk and transient/piecemeal detail.

Resolve every relative or partial time expression to an ABSOLUTE timestamp \
using the current time given above, and bake it into the fact so it stands \
alone without the conversation. Compute concrete dates from the weekday and \
date provided (e.g. "next Monday", "tomorrow", "in two weeks", "this \
evening"). For example, "we'll release the example project next Monday at \
9am" becomes a fact like "Example project release is scheduled for \
2025-06-09 09:00". Use the `YYYY-MM-DD HH:MM` format (24-hour; omit the time \
if only a date was given); append the timezone only when it differs from the \
user's local one. Leave facts that carry no time untouched.

Return ONLY JSON:
{"facts": [{"fact": "<self-contained statement>", "kind": "preference|personal_info|event|project"}]}
Return {"facts": []} if nothing is worth keeping.\
"""


def _now_line(timezone: str) -> str:
    """`2026-05-29 14:30 America/New_York (Friday)` in the user's timezone
    (falling back to UTC on an unknown tz) — the anchor for resolving
    relative time expressions during extraction."""
    try:
        tz = ZoneInfo(timezone)
        label = timezone
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
        label = "UTC"
    return datetime.now(tz).strftime(f"%Y-%m-%d %H:%M {label} (%A)")


def _extract_system(now_line: str) -> str:
    return f"The current time is {now_line}.\n\n{_EXTRACT_INSTRUCTIONS}"

_RECONCILE_SYSTEM = """\
You maintain a user's long-term memory. Given a NEW candidate fact and the \
most similar EXISTING facts (numbered), decide how to reconcile. Return ONLY JSON:
{"action": "NEW|UPDATE|REMOVE", "fact_number": <int, for UPDATE/REMOVE>, \
"content": "<text, for NEW/UPDATE>", "kind": "preference|personal_info|event|project", \
"related": [<existing fact numbers related to this one>]}
Use NEW if it's genuinely new, UPDATE if it refines an existing fact, \
REMOVE if it contradicts/obsoletes one.\
"""

_RELATE_SYSTEM = """\
Given an anchor fact and its neighbor facts (numbered), decide per neighbor. \
Return ONLY JSON: {"decisions": [{"action": "RELATE|UPDATE|REMOVE", \
"fact_number": <int>, "contents": "<text, for UPDATE>"}]}.\
"""

_UPDATE_PROP_SYSTEM = """\
An anchor fact was just updated. Given its neighbor facts (numbered), decide \
per neighbor. Return ONLY JSON: {"decisions": [{"action": "UPDATE|REMOVE|UNRELATE", \
"fact_number": <int>, "contents": "<text, for UPDATE>"}]}.\
"""


agent = Agent(
    info=AgentInfo(
        agent_id=MEMORY_AGENT_ID,
        description=(
            "Per-user long-term memory — a fact graph of what's been "
            "learned about the user across conversations."
        ),
        groups=["l3"],
        capabilities=["memory.add", "memory.retrieval"],
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None
# Per-user lock — at most one structural mutation (add / GC) per user.
_user_locks: dict[str, asyncio.Lock] = {}
_stop = asyncio.Event()
_gc_task: asyncio.Task | None = None


def _user_lock(user_id: str) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


@agent.on_startup
async def _startup() -> None:
    global _pool, _gc_task  # noqa: PLW0603 — startup-wired handles
    _pool = await open_pool(_settings)
    _stop.clear()
    _gc_task = asyncio.create_task(
        gc_sweep_loop(_pool, _settings, stop=_stop)
    )


@agent.on_shutdown
async def _shutdown() -> None:
    _stop.set()
    if _gc_task is not None:
        await _gc_task
    if _pool is not None:
        await _pool.close()


async def gc_sweep_loop(
    pool: asyncpg.Pool, settings: SuiteSettings, *, stop: asyncio.Event
) -> None:
    """Periodic GC sweep ([memory.md] §5). Same scheduler shape as cron:
    run a pass, then wait `memory_gc_interval_s` (or until stopped)."""
    while not stop.is_set():
        try:
            await gc_sweep(pool, settings)
        except Exception:  # noqa: BLE001
            logger.exception("memory_gc_error", extra={"event": "memory_gc_error"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.memory_gc_interval_s)
        except TimeoutError:
            pass


async def gc_sweep(pool: asyncpg.Pool, settings: SuiteSettings) -> int:
    """One sweep over every user with an existing fact graph. Each user's
    GC runs under that user's lock (serialized against `add`). Returns the
    number of facts swept."""
    async with pool.acquire() as conn:
        user_ids = await queries.list_user_ids(conn)
    total = 0
    for user_id in user_ids:
        # Skip users with no LanceDB yet — don't materialize empty stores.
        if not user_db_path(settings.lance_root, user_id).exists():
            continue
        async with _user_lock(user_id):
            db = await connect(settings.lance_root, user_id)
            store = MemoryStore(db, embedding_dim=settings.embedding_dim)
            total += await store.gc(horizon_days=settings.memory_gc_horizon_days)
    return total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _presets(ctx: TaskContext, settings: SuiteSettings) -> tuple[str, str]:
    """(lite_preset, embedding_preset) for this user."""
    if _pool is None:
        return settings.default_preset_lite, settings.default_preset_embedding
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    if cfg is None:
        return settings.default_preset_lite, settings.default_preset_embedding
    return cfg.preset_lite, cfg.preset_embedding


async def _user_timezone(ctx: TaskContext) -> str:
    """The user's IANA timezone (for resolving relative time in extraction);
    UTC when there's no pool/config — mirrors `_presets`' degradation."""
    if _pool is None:
        return "UTC"
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    return cfg.timezone if cfg else "UTC"


async def _store_for(ctx: TaskContext, settings: SuiteSettings) -> MemoryStore:
    db = await connect(settings.lance_root, ctx.user_id)
    return MemoryStore(db, embedding_dim=settings.embedding_dim)


async def _llm_json(
    ctx: TaskContext, *, preset: str, system: str, user: str
) -> dict[str, Any]:
    resp = await ctx.llm.generate(
        [Message(role="system", content=system), Message(role="user", content=user)],
        preset=preset,
    )
    text = resp.text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        text = text.removeprefix("json").strip().strip("`").strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("memory_llm_json_parse_failed", extra={"event": "memory_llm_json_parse_failed"})
        return {}


def _decay(last_used_iso: str, settings: SuiteSettings) -> float:
    try:
        age_days = (datetime.now(UTC) - datetime.fromisoformat(last_used_iso)).days
    except ValueError:
        return 1.0
    start, horizon, floor = (
        settings.memory_decay_start_days,
        settings.memory_gc_horizon_days,
        settings.memory_decay_floor,
    )
    if age_days <= start:
        return 1.0
    if age_days >= horizon:
        return floor
    frac = (age_days - start) / (horizon - start)
    return 1.0 - (1.0 - floor) * frac


# ---------------------------------------------------------------------------
# retrieve (lock-free)
# ---------------------------------------------------------------------------


async def run_memory_retrieve(
    ctx: TaskContext,
    payload: MemRetrieve,
    *,
    settings: SuiteSettings,
    store: MemoryStore | None = None,
    embed_preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    if embed_preset is None:
        _lite, embed_preset = await _presets(ctx, settings)

    limit = settings.memory_retrieve_pool
    qv = (await ctx.llm.embed([payload.query], preset=embed_preset))[0]
    vec_pool = await store.search(query_vector=qv, limit=limit)
    try:
        bm_pool = await store.search_bm25(query=payload.query, limit=limit)
    except Exception:  # noqa: BLE001 — recall must survive an FTS parse error
        logger.debug("memory_bm25_failed", exc_info=True)
        bm_pool = []
    if not vec_pool and not bm_pool:
        return text_output("No relevant memories.")

    # Hybrid pool ([memory.md] §4): reciprocal-rank fusion of the vector and
    # BM25 legs gives each fact a base relevance, which recency decay then
    # re-ranks. (Version-independent — no LanceDB reranker.)
    relevance: dict[str, float] = {}
    for rows in (vec_pool, bm_pool):
        for rank, f in enumerate(rows):
            relevance[f["uid"]] = relevance.get(f["uid"], 0.0) + 1.0 / (60 + rank + 1)
    by_uid = {f["uid"]: f for f in (*vec_pool, *bm_pool)}

    def _score(f: dict[str, Any]) -> float:
        return relevance[f["uid"]] * _decay(f["last_used_at"], settings)

    ranked = sorted(by_uid.values(), key=_score, reverse=True)
    top = ranked[: payload.count]
    top_uids = {f["uid"] for f in top}

    # 1-hop graph expansion: neighbors of the top, excluding the top.
    expansion: list[dict[str, Any]] = []
    seen: set[str] = set(top_uids)
    for f in top:
        for nuid in await store.neighbors(f["uid"]):
            if nuid in seen:
                continue
            seen.add(nuid)
            nf = by_uid.get(nuid) or await store.get_fact(nuid)
            if nf is not None:
                expansion.append(nf)
    expansion.sort(key=lambda f: _decay(f["last_used_at"], settings), reverse=True)
    expansion = expansion[: payload.child_count]

    returned = top + expansion
    await store.touch([f["uid"] for f in returned])
    lines = [f"- {f['fact']} ({f['kind']})" for f in returned]
    return text_output("\n".join(lines))


# ---------------------------------------------------------------------------
# add (under per-user lock)
# ---------------------------------------------------------------------------


def _enumerate(facts: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i + 1}. {f['fact']}" for i, f in enumerate(facts))


async def _propagate(
    ctx: TaskContext,
    store: MemoryStore,
    *,
    anchor_uid: str,
    system: str,
    lite_preset: str,
    embed_preset: str,
) -> None:
    """1-hop neighbor propagation (phases 3/4). Best-effort."""
    neighbor_uids = await store.neighbors(anchor_uid)
    if not neighbor_uids:
        return
    neighbors = [nf for u in neighbor_uids if (nf := await store.get_fact(u))]
    if not neighbors:
        return
    anchor = await store.get_fact(anchor_uid)
    user = (
        f"Anchor: {anchor['fact'] if anchor else anchor_uid}\n\nNeighbors:\n"
        + _enumerate(neighbors)
    )
    decision = await _llm_json(ctx, preset=lite_preset, system=system, user=user)
    for d in decision.get("decisions", []):
        n = d.get("fact_number")
        if not isinstance(n, int) or not (1 <= n <= len(neighbors)):
            continue
        target = neighbors[n - 1]["uid"]
        action = str(d.get("action", "")).upper()
        if action == "REMOVE":
            await store.remove_fact(target)
        elif action == "UNRELATE":
            await store.remove_edge(anchor_uid, target)
        elif action == "RELATE":
            await store.add_edge(anchor_uid, target)
        elif action == "UPDATE" and d.get("contents"):
            vec = (await ctx.llm.embed([d["contents"]], preset=embed_preset))[0]
            await store.update_fact(uid=target, fact=d["contents"], embedding=vec)


async def run_memory_add(
    ctx: TaskContext,
    payload: MemAdd,
    *,
    settings: SuiteSettings,
    store: MemoryStore | None = None,
    lite_preset: str | None = None,
    embed_preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    if lite_preset is None or embed_preset is None:
        lite_preset, embed_preset = await _presets(ctx, settings)

    # Phase 1 — extract (+ batch dedup). The system prompt carries the
    # current time so the LLM resolves relative dates to absolute ones.
    now_line = _now_line(await _user_timezone(ctx))
    extracted = await _llm_json(
        ctx, preset=lite_preset, system=_extract_system(now_line),
        user=f"User: {payload.user_prompt}\nAssistant: {payload.assistant_response}",
    )
    raw = extracted.get("facts", []) or []
    seen_text: set[str] = set()
    facts: list[dict[str, str]] = []
    for f in raw[:_MAX_FACTS_PER_TURN]:
        t = (f.get("fact") or "").strip()
        kind = f.get("kind") if f.get("kind") in _VALID_KINDS else "personal_info"
        if t and t.lower() not in seen_text:
            seen_text.add(t.lower())
            facts.append({"fact": t, "kind": kind})
    if not facts:
        return text_output("")

    await _reconcile_and_store(
        ctx, store, facts, settings=settings,
        lite_preset=lite_preset, embed_preset=embed_preset,
    )
    return text_output("")


async def _reconcile_and_store(
    ctx: TaskContext,
    store: MemoryStore,
    facts: list[dict[str, str]],
    *,
    settings: SuiteSettings,
    lite_preset: str,
    embed_preset: str,
) -> None:
    """Phases 2–4: reconcile each candidate fact against its nearest
    neighbours (NEW / UPDATE / REMOVE), wire edges, then propagate. Shared by
    the post-turn `add` (after extraction) and the webapp `manual_add`."""
    related_anchors: list[str] = []  # NEW/UPDATE'd uids that gained edges
    updated_uids: list[str] = []

    # Phase 2 — reconcile each fact against its nearest neighbours.
    for item in facts:
        fv = (await ctx.llm.embed([item["fact"]], preset=embed_preset))[0]
        cands = await store.search(
            query_vector=fv, limit=settings.memory_reconcile_candidates
        )
        cand_uids = [c["uid"] for c in cands]
        decision = await _llm_json(
            ctx, preset=lite_preset, system=_RECONCILE_SYSTEM,
            user=f"Candidate fact: {item['fact']}\n\nExisting facts:\n"
            + (_enumerate(cands) or "(none)"),
        )
        action = str(decision.get("action", "NEW")).upper()
        content = (decision.get("content") or item["fact"]).strip()
        kind = decision.get("kind") if decision.get("kind") in _VALID_KINDS else item["kind"]
        related = [
            cand_uids[n - 1]
            for n in decision.get("related", []) or []
            if isinstance(n, int) and 1 <= n <= len(cand_uids)
        ]
        fnum = decision.get("fact_number")
        valid_target = isinstance(fnum, int) and 1 <= fnum <= len(cand_uids)

        if action == "REMOVE" and valid_target:
            await store.remove_fact(cand_uids[fnum - 1])
            continue
        if action == "UPDATE" and valid_target:
            target = cand_uids[fnum - 1]
            vec = (await ctx.llm.embed([content], preset=embed_preset))[0]
            await store.update_fact(uid=target, fact=content, embedding=vec)
            updated_uids.append(target)
            anchor = target
        else:  # NEW (or an UPDATE/REMOVE with an invalid number → treat as NEW)
            vec = (await ctx.llm.embed([content], preset=embed_preset))[0]
            anchor = await store.insert_fact(fact=content, kind=kind, embedding=vec)
        for r in related:
            await store.add_edge(anchor, r)
        if related:
            related_anchors.append(anchor)

    # Phase 3 — relate-out propagation (best-effort, 1-hop).
    for anchor in related_anchors:
        try:
            await _propagate(
                ctx, store, anchor_uid=anchor, system=_RELATE_SYSTEM,
                lite_preset=lite_preset, embed_preset=embed_preset,
            )
        except Exception:  # noqa: BLE001
            logger.debug("memory_phase3_failed", exc_info=True)

    # Phase 4 — update propagation (best-effort, 1-hop).
    for anchor in updated_uids:
        try:
            await _propagate(
                ctx, store, anchor_uid=anchor, system=_UPDATE_PROP_SYSTEM,
                lite_preset=lite_preset, embed_preset=embed_preset,
            )
        except Exception:  # noqa: BLE001
            logger.debug("memory_phase4_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Webapp Memory page — list / delete / manual_add (tool:false, JSON)
# ---------------------------------------------------------------------------


def _fact_item(f: dict[str, Any], *, score: float | None = None) -> dict[str, Any]:
    item = {
        "uid": f["uid"],
        "fact": f["fact"],
        "kind": f.get("kind", ""),
        "created_at": f.get("created_at", ""),
        "last_used_at": f.get("last_used_at", ""),
    }
    if score is not None:
        item["score"] = round(score, 4)
    return item


async def run_memory_list(
    ctx: TaskContext,
    payload: MemList,
    *,
    settings: SuiteSettings,
    store: MemoryStore | None = None,
    embed_preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    start = max(0, payload.start)
    end = max(start, min(payload.end, start + MAX_PAGE))
    query = (payload.query or "").strip()

    if not query:
        # No query → newest first by last_used_at.
        facts = await store.all_facts()
        if payload.kind:
            kind = payload.kind.casefold()
            facts = [f for f in facts if (f.get("kind") or "").casefold() == kind]
        facts.sort(key=lambda f: f.get("last_used_at", ""), reverse=True)
        items = [_fact_item(f) for f in facts[start:end]]
        return text_output(json.dumps({"items": items, "total": len(facts)}))

    # Query → hybrid pool ranked by the retrieval formula (relevance × decay),
    # without graph expansion or touch (browsing must not reset decay).
    if embed_preset is None:
        _lite, embed_preset = await _presets(ctx, settings)
    limit = settings.memory_retrieve_pool
    qv = (await ctx.llm.embed([query], preset=embed_preset))[0]
    vec_pool = await store.search(query_vector=qv, limit=limit)
    try:
        bm_pool = await store.search_bm25(query=query, limit=limit)
    except Exception:  # noqa: BLE001 — survive an FTS parse error
        logger.debug("memory_list_bm25_failed", exc_info=True)
        bm_pool = []
    relevance: dict[str, float] = {}
    for rows in (vec_pool, bm_pool):
        for rank, f in enumerate(rows):
            relevance[f["uid"]] = relevance.get(f["uid"], 0.0) + 1.0 / (60 + rank + 1)
    by_uid = {f["uid"]: f for f in (*vec_pool, *bm_pool)}
    pool = list(by_uid.values())
    if payload.kind:
        kind = payload.kind.casefold()
        pool = [f for f in pool if (f.get("kind") or "").casefold() == kind]

    def _score(f: dict[str, Any]) -> float:
        return relevance[f["uid"]] * _decay(f["last_used_at"], settings)

    ranked = sorted(pool, key=_score, reverse=True)
    items = [_fact_item(f, score=_score(f)) for f in ranked[start:end]]
    return text_output(json.dumps({"items": items, "total": len(ranked)}))


async def run_memory_delete(
    ctx: TaskContext,
    payload: MemDelete,
    *,
    settings: SuiteSettings,
    store: MemoryStore | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    if await store.get_fact(payload.uid) is None:
        return text_output(json.dumps({"deleted": False}))
    await store.remove_fact(payload.uid)
    return text_output(json.dumps({"deleted": True}))


async def run_memory_manual_add(
    ctx: TaskContext,
    payload: MemManualAdd,
    *,
    settings: SuiteSettings,
    store: MemoryStore | None = None,
    lite_preset: str | None = None,
    embed_preset: str | None = None,
) -> AgentOutput:
    store = store or await _store_for(ctx, settings)
    if lite_preset is None or embed_preset is None:
        lite_preset, embed_preset = await _presets(ctx, settings)
    fact = payload.fact.strip()
    if not fact:
        return text_output(json.dumps({"added": False}))
    kind = payload.kind if payload.kind in _VALID_KINDS else "personal_info"
    await _reconcile_and_store(
        ctx, store, [{"fact": fact, "kind": kind}],
        settings=settings, lite_preset=lite_preset, embed_preset=embed_preset,
    )
    return text_output(json.dumps({"added": True}))


@agent.handler(
    mode="retrieve",
    description="Recall facts remembered about this user (preferences, "
    "personal details, prior context) relevant to a query — call to "
    "personalize a reply or when the user refers to something said before.",
)
async def retrieve_mode(ctx: TaskContext, payload: MemRetrieve) -> AgentOutput:
    # Lock-free; only writes last_used_at (a recency heuristic).
    return await run_memory_retrieve(ctx, payload, settings=_settings)


@agent.handler(
    mode="add", tool=False,
    description="Extract and store durable facts from a completed turn "
    "(background; not user-facing).",
)
async def add_mode(ctx: TaskContext, payload: MemAdd) -> AgentOutput:
    async with _user_lock(ctx.user_id):
        return await run_memory_add(ctx, payload, settings=_settings)


@agent.handler(
    mode="list", tool=False,
    description="List/browse the user's stored facts for the Memory page "
    "(JSON; newest-first or query-ranked, kind-filterable, paged).",
)
async def list_mode(ctx: TaskContext, payload: MemList) -> AgentOutput:
    return await run_memory_list(ctx, payload, settings=_settings)


@agent.handler(
    mode="delete", tool=False,
    description="Delete one stored fact by uid (Memory page).",
)
async def delete_mode(ctx: TaskContext, payload: MemDelete) -> AgentOutput:
    async with _user_lock(ctx.user_id):
        return await run_memory_delete(ctx, payload, settings=_settings)


@agent.handler(
    mode="manual_add", tool=False,
    description="Store a user-authored fact, bypassing extraction (Memory "
    "page); reconciles against existing facts like a normal add.",
)
async def manual_add_mode(ctx: TaskContext, payload: MemManualAdd) -> AgentOutput:
    async with _user_lock(ctx.user_id):
        return await run_memory_manual_add(ctx, payload, settings=_settings)


if __name__ == "__main__":
    agent.run()
