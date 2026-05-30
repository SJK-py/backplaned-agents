# Scaling

> Where this system stands on horizontal scale today, and the ranked
> backlog of work to lift each ceiling. Companion to
> [`deployment.md`](./deployment.md) (topology, secrets, graceful
> shutdown) and [`router/storage.md §6`](./router/storage.md#6-concurrency-model)
> (the router concurrency model).

This document is the single home for two things that were previously
scattered across the docs:

1. **Current scaling posture** — what is safe to run multi-instance
   today, what is not, and which subsystems already have a Redis path.
2. **Deferred scaling backlog** — the perf/scale findings from the
   pre-release review passes, ranked, each with the impact, the scale at
   which it bites, and the intended fix. None are launch blockers; the
   target deployment is modest (single router worker, one channel
   instance) and every item below is a ceiling, not a current bug.

---

## 1. Current scaling posture

### 1.1 Router — single worker today

**The router runs as a single worker.** The in-memory socket registry,
pending-ack futures, the LLM-correlation maps, the user-level / caller-agent
caches, and the catalog cache are all **process-local**. Deployments needing
more capacity should scale **vertically** (a bigger box) — not by adding
router workers/replicas — until the multi-worker work below lands.

Running more than one router worker today causes, concretely:

- **WS delivery misses.** A frame admitted on worker A can't reach an
  agent whose socket lives on worker B (the registry is per-process), so
  cross-worker spawn/delegate/Result fan-out silently fails to deliver.
- **Progress fan-out pays a per-frame DB hit.** `caller_agent_cache` is
  populated only on the worker that admitted the task, so Progress frames
  arriving on another worker always miss and fall through to a `tasks`
  lookup (see backlog item **②**).
- **JTI revocation no-ops without Redis.** `revoke_jti` / `is_jti_revoked`
  silently no-op when `ROUTER_REDIS_URL` is unset, so a logout on one
  worker isn't seen by another (see [`security.md`](./security.md) §
  "Single-worker dev fallback"). With Redis set, revocation is shared.
- **Quota counters drift.** In-memory rate-limit buckets are per-process;
  without the shared Redis counter the same user gets N× the limit across
  N workers (see [`design/quota-enforcement.md`](./design/quota-enforcement.md)).

The intended multi-worker path (sticky WS routing by `agent_id`, the
socket registry in Redis, pending-ack futures staying process-local) is
specified in [`router/storage.md §6.1`](./router/storage.md#61-multi-worker--planned).

### 1.2 What Redis already buys (partial multi-instance)

Redis support is **partially implemented** — some subsystems are already
cross-process-correct when a Redis URL is configured, even though the
router WS plane (§1.1) is not. So "needs Redis" and "multi-worker safe"
are **not** the same thing; the table is the precise map:

| Subsystem | Config | Single instance | Multi-instance correctness |
|---|---|---|---|
| Router WS socket registry / fan-out | — | ✅ | ❌ not yet (item ②; storage §6.1) |
| JWT JTI revocation | `ROUTER_REDIS_URL` | ✅ (no-op, documented) | ✅ with Redis |
| Login / rate-limit quota counters | `ROUTER_REDIS_URL` | ✅ (in-mem) | ✅ with Redis |
| Suite per-session turn lock | `SUITE_REDIS_URL` | ✅ (`asyncio.Lock`) | ✅ with Redis (distributed lock) |
| Suite memory per-user lock | `SUITE_REDIS_URL` | ✅ (in-proc) | ✅ with Redis |
| Cron double-fire safety | — (DB) | ✅ | ✅ atomic DB claim |

The suite per-session lock detail lives in
[`agent-suite/sessions.md §4`](./agent-suite/sessions.md); memory in
[`agent-suite/memory.md`](./agent-suite/memory.md); cron in
[`agent-suite/cron.md`](./agent-suite/cron.md).

### 1.3 Per-service scaling

- **router** — single worker (§1.1); scale vertically. The one-shot
  `migrate` is the only process that runs `alembic upgrade`.
- **chatbot** — stateful (per-session FIFO queue, Telegram offset). v1:
  **single instance**. To run more, set `SUITE_REDIS_URL` (distributed
  session lock) and add session→worker affinity.
- **webapp** — shares the suite image; the per-session lock makes a
  second instance alongside the chatbot safe **for session serialization**,
  but it connects to the single-worker router, so it inherits §1.1.
- **stores (KB + memory)** — co-located; >1 replica requires the memory
  per-user lock in Redis (`SUITE_REDIS_URL`).
- **sandbox** — scale by workspace / runtime capacity; keep the isolation
  invariants (uid drop, rlimits, workspace confinement) regardless of
  replica count.

---

## 2. Deferred scaling backlog

Findings from the second-pass perf/scale review. Each is a ceiling that
bites at a stated scale, not a current defect. Ordered by "realistic
deployment impact". The two pure-index quick-wins from this list were
already shipped (`session_info` list index + `registration_attempts` GC
index); what remains:

### ① Catalog broadcast fan-out — **HIGH** (bites at low hundreds of agents)

`push_catalog_update_to_all` recomputes every connected agent's visible
catalog on **every agent onboard**, **synchronously on the onboard
request path**, at cost ≈ `live_agents × total_agents × deployment_levels
× acl_rules` pattern-matches. A fleet of N agents booting (router restart,
rolling deploy) is ~O(N²) of that. The DB-scan storm is already collapsed
by `_CatalogCache`, but the **rule recomputation** (the dominant cost) is
not.

**Fix:** (a) memoize the per-`(caller, callee)` rule decision within a
single broadcast; (b) coalesce/debounce broadcasts (one timer-collapsed
rebuild instead of one-per-onboard); (c) move the broadcast off the
awaited onboard path (fire-and-forget) so onboard latency is decoupled.
*Files:* `bp_router/catalog.py`, `bp_router/visibility.py`,
`bp_router/api/onboard.py`.

### ② Multi-worker Progress-cache gap — **MED-HIGH** (any 2+ router worker)

`caller_agent_cache` is populated only on the worker that admitted the
task, so in a multi-worker deployment Progress frames arriving on a
different worker always miss and do a per-frame `tasks` PK lookup (a pool
checkout per frame on the dominant streaming path). Tied to §1.1 — only
relevant once the router is multi-worker.

**Fix:** pin a task's frames to its admitting worker (sticky WS routing,
the same mechanism §1.1 needs), OR share the cache via Redis, OR document
as a known multi-worker cost. *File:* `bp_router/dispatch.py`.

### ⑤ Audit-log append throughput — **MED** (high auditable-op rate)

Every `append_audit_event` takes a **single global** `pg_advisory_xact_lock`
for the duration of its transaction (the hash-chain integrity requires a
serial append order). So all auditable operations — delegations, ACL
changes, admin actions, onboards — globally serialize through one lock.
Correctness-driven, so it can't be naively removed.

**Fix (architectural — needs its own design pass):** per-tenant chains
(chain-per-`user_id`/tenant) or move the append off the request path into
a queued single-writer. Track, don't rush. *File:*
`bp_router/db/queries.py` (`append_audit_event`).

### ⑥ Cron scheduler N+1 — **MED** (10k+ active jobs deployment-wide)

`tick()` loads **all** active cron jobs across all users every 60s
(partial-index-backed) then issues a per-due-job `claim_cron_job` UPDATE
and 1–2 `_resolve_session` queries per firing. Scan + serial claims scale
with the global active-job count.

**Fix:** store an indexed `next_fire_at` column and push the due-time
filter into SQL (return only due jobs), then batch the claims. *Files:*
`bp_agents/agents/chatbot/cron.py`, suite schema.

### ⑦ Memory GC sweep — serial per user — **MED** (thousands of users)

`gc_sweep` lists all user ids (an unbounded `SELECT user_id FROM
user_config`), then for **every** user does a blocking `Path.exists()` +
LanceDB connect + `store.gc()` **serially**, under the per-user lock. The
sweep wall-time grows linearly with user count and may not finish within
`memory_gc_interval_s`.

**Fix:** paginate `list_user_ids`; run the per-user GC with bounded
concurrency; skip the `exists()` syscall by tracking which users have a
store. *File:* `bp_agents/agents/memory/agent.py`.

### ⑧ Uncapped spawn breadth — **LOW-MED** (adversarial / buggy wide trees)

Spawn **depth** is capped (`spawn_max_depth`), but **breadth** is not —
one parent may spawn unboundedly many children. `list_descendants` is
depth-bounded but breadth-unbounded, so a single `cancel_task` /
`fail_task` on a wide root is O(subtree) with a per-descendant transaction
on one shared connection.

**Fix:** add a per-parent child cap at admit, and/or a `LIMIT` + batching
on the descendant cascade. *Files:* `bp_router/tasks.py`,
`bp_router/db/queries.py`.

### ⑨ Per-upload storage re-SUM + retention — **LOW-MED**

- `count_user_storage_bytes` re-`SUM`s **all** of a user's `file_names`
  rows on **every** upload (quota gate). Cost grows with names-per-user.
  **Fix:** maintain a denormalized per-user byte counter, incremented on
  insert/repoint/delete.
- `audit_log` and `task_events` have **no GC** — append-only, grow
  forever (`audit_log` is intentionally so for the hash chain;
  `task_events` only clears on `purge_session`). **Fix:** an operator
  retention/partitioning runbook for `audit_log`; a `task_events`
  retention sweep for never-purged sessions.
*Files:* `bp_router/db/queries.py`, ops runbook.

---

## 3. Items verified healthy (no action)

For the record, the review confirmed these scale fine as-is: the deadline
sweep (batched, partial-index-backed), the file GC (batched, storage I/O
outside the txn), the user-level / caller-agent / preset / adapter caches
(bounded LRU + TTL, correct invalidation), recursive-CTE depth bounds, and
the admit-path idempotency lookup (fully index-backed). The two quick-win
indexes (`session_info` list, `registration_attempts` GC) are shipped.
