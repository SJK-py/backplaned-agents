# Agent Suite — Memory Fact-Graph

> Per-user long-term memory: a graph of facts with bi-directional relations,
> built from conversation turns and recalled by relevance + recency. Backed
> by per-user LanceDB. Schema in [`data-model.md`](./data-model.md). The
> overriding concern is concurrency — see §1.

## 1. Concurrency: a per-user lock (the load-bearing rule)

`memory.add` is fired fire-and-forget by the channel and is **per-user**, so concurrent adds collide across a user's sessions and rapid turns. The fact-graph is read-modify-write and **LanceDB is non-transactional** (no row locks, no multi-row ACID). Unserialized, two adds will duplicate `NEW`s, lose `UPDATE`s, drop/asymmetric-link `related`, and dangle on `REMOVE`.

**Rule:** a **per-`user_id` lock/queue** wraps the entire `add` (phases 1–4) **and** GC — at most one structural mutation per user at a time (in-memory single-instance / Valkey multi-worker, same pattern as the session queue but keyed on user). `retrieve` stays **lock-free**: its only write is `last_used_at`, a recency heuristic that tolerates races. This is what makes phases 2–4 correct.

## 2. Fact representation

A **fact**: `uid`, `fact` (text — descriptive enough to stand alone), `kind` (`preference` \| `personal_info` \| `event` \| `project`), `created_at`, `last_used_at` (init = `created_at`; refreshed on every retrieval or update), plus its embedding.

**Relations as an edge-set**, *not* a `related: [uid]` field: store deduped unordered pairs `(min_uid, max_uid)`. This makes bidirectionality and cascades free:

- neighbors of X = pairs containing X;
- `REMOVE X` = delete pairs containing X (no dangling refs, no inbound scan);
- degree cap (≤10 per fact) evicts an edge → gone for **both** endpoints automatically.

*(If kept as a list-on-row: enforce dual-write on every link and inbound-cascade on every remove — the edge-set just makes those automatic.)*

## 3. `add` — four phases (under the per-user lock)

Input: `{user_prompt, assistant_response}`. Commit incrementally so later facts in a batch see earlier `NEW`s.

- **Phase 1 — extract.** Decide whether the turn carries facts worth keeping (discard piecemeal / non-user-relevant info) and extract a deduped list of facts. (Dedup the batch here.) The extract system prompt is **stamped with the current time + weekday in the user's timezone**, and instructs the model to resolve **relative time** ("next Monday 9am", "tomorrow") to an **absolute `YYYY-MM-DD HH:MM`** baked into the fact — so a stored fact stands alone without the conversation or the moment it was said.
- **Phase 2 — reconcile (per extracted fact).** Hybrid (vector + BM25) search → enumerate top 5 → structured output: list of `{action: NEW|UPDATE|REMOVE, fact_number (ignore if NEW), content (ignore if REMOVE), kind (ignore if REMOVE), related (ignore if REMOVE)}`. Apply: `NEW` inserts; `UPDATE` rewrites content (+`last_used_at`); `REMOVE` deletes **with edge cascade**.
- **Phase 3 — relate-out propagation.** For every fact `NEW`'d with `related` and every fact whose `related` changed: enumerate the neighbor facts → structured output `{action: UPDATE|REMOVE|RELATE, fact_number, contents (UPDATE only)}`. `RELATE` adds an edge; `UPDATE` rewrites content + adds an edge; `REMOVE` cascades.
- **Phase 4 — update propagation.** For every fact `UPDATE`d in phase 2: enumerate its neighbors → `{action: UPDATE|REMOVE|UNRELATE, fact_number, contents (UPDATE only)}`. `UNRELATE` removes the edge (both sides, automatic).

**Hardening (every phase):**
- **Enumeration→UID validation** — build the number→UID map per phase; drop out-of-range numbers; `NEW` carries no number; ignore unresolvable `fact_number`/`related`. LLM index drift here silently corrupts the graph.
- **Propagation is 1-hop and deliberate** — phases 3/4 touch only direct neighbors and don't recurse. Accept that a propagated `UPDATE` may leave *its* neighbors slightly stale; memory is heuristic, not a consistent KB.
- **Best-effort failure** — `add` is fire-and-forget; a mid-run crash leaves phase-2 facts landed (fine) and phases 3/4 are **enhancement** (safe to skip). Edge writes are atomic on both endpoints (free with the edge-set).
- **Rough idempotency** — an accidental re-fire dedups via phase-2 search (finds the just-added facts → `UPDATE`/no-op, not duplicate).

## 4. `retrieve`

Input: `{query, count?=3, child_count?=2}`. Lock-free.

- **Phase 1 — score.** Hybrid search → top 50 (env-configurable). Apply **recency decay** keyed on **`last_used_at`**: facts older than 30 days get a score multiplier sliding from 1.0 (30 d) toward 0.5 (approaching the 100-day GC horizon) — all thresholds env-configurable. Re-rank.
- **Phase 2 — graph expansion.** Take the top `count`. Gather their neighbors, **exclude the top `count`**, **dedup**, rank by decayed score, take `child_count`. Return `count + child_count` facts.
- **Refresh `last_used_at = now` on _all_ returned facts** (incl. the expansion) — so a surfaced cluster ages and survives together.

Returned as `AgentOutput(content=<formatted facts>)` so the caller threads it as a normal tool result.

## 5. GC

Remove facts whose `last_used_at` is older than 100 days (env-configurable). GC is a **per-user serialized `REMOVE`-with-cascade** sweep (shares the add lock) — it deletes the fact and its edges. Because decay + GC both key on `last_used_at` (refreshed on retrieval/update), a fact that keeps getting surfaced never decays or GCs; only genuinely-unused facts fade then vanish.

## 6. Models & cost

- **Two model roles:** the **lite chat preset** for the phase 1–4 extraction/decision calls; a **separate embedding preset** (`ctx.llm.embed`) for fact + query vectors. The embedding preset is distinct from the three chat presets in user-config.
- **Fan-out:** a turn with K facts → ~`1 + 3K` lite-preset calls. Bounded and async (fire-and-forget hides latency), but cap **facts-per-turn** and consider **batching** phases 2–4 across facts if cost matters.

## 7. Identity

`add`/`retrieve` run under the end-user's identity (`serviced_by`), so the per-user LanceDB resolves from the authoritative `user_id` — same isolation guarantee as the file store.
