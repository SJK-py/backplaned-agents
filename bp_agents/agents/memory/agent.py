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

from pydantic import BaseModel

from bp_agents.common import text_output
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.lance import connect
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

_EXTRACT_SYSTEM = """\
Extract durable facts about the user worth remembering long-term from this \
turn — preferences, personal info, ongoing events, projects. Ignore \
small talk and transient/piecemeal detail. Return ONLY JSON:
{"facts": [{"fact": "<self-contained statement>", "kind": "preference|personal_info|event|project"}]}
Return {"facts": []} if nothing is worth keeping.\
"""

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


class MemAdd(BaseModel):
    user_prompt: str
    assistant_response: str


class MemRetrieve(BaseModel):
    query: str
    count: int = 3
    child_count: int = 2


agent = Agent(
    info=AgentInfo(
        agent_id=MEMORY_AGENT_ID,
        description="Per-user long-term memory (fact graph).",
        groups=["l3"],
        capabilities=["memory.add", "memory.retrieval"],
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None
# Per-user lock — at most one structural mutation (add / GC) per user.
_user_locks: dict[str, asyncio.Lock] = {}


def _user_lock(user_id: str) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


@agent.on_startup
async def _startup() -> None:
    global _pool  # noqa: PLW0603 — startup-wired handle
    _pool = await open_pool(_settings)


@agent.on_shutdown
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


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

    qv = (await ctx.llm.embed([payload.query], preset=embed_preset))[0]
    pool = await store.search(query_vector=qv, limit=settings.memory_retrieve_pool)
    if not pool:
        return text_output("No relevant memories.")

    def _score(f: dict[str, Any]) -> float:
        sim = 1.0 / (1.0 + float(f.get("_distance", 0.0)))
        return sim * _decay(f["last_used_at"], settings)

    ranked = sorted(pool, key=_score, reverse=True)
    top = ranked[: payload.count]
    top_uids = {f["uid"] for f in top}
    by_uid = {f["uid"]: f for f in pool}

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

    # Phase 1 — extract (+ batch dedup).
    extracted = await _llm_json(
        ctx, preset=lite_preset, system=_EXTRACT_SYSTEM,
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

    return text_output("")


@agent.handler(mode="retrieve")
async def retrieve_mode(ctx: TaskContext, payload: MemRetrieve) -> AgentOutput:
    # Lock-free; only writes last_used_at (a recency heuristic).
    return await run_memory_retrieve(ctx, payload, settings=_settings)


@agent.handler(mode="add", tool=False)
async def add_mode(ctx: TaskContext, payload: MemAdd) -> AgentOutput:
    async with _user_lock(ctx.user_id):
        return await run_memory_add(ctx, payload, settings=_settings)


if __name__ == "__main__":
    agent.run()
