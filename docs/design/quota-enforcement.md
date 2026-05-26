# Quota enforcement (WS-H3)

> **Status:** §7 phase 1 + 2 LANDED. Phase 3 (concurrent-tasks
> SET-based cap) and §11 (per-agent inbound frame rate-limit)
> still deferred — see §12 below for what shipped.
>
> **Scope:** the post-merge full-codebase review's WS-H3 finding —
> the router has metric infrastructure for quota
> (`router_quota_exceeded_total{counter, level}`) but no caller
> increments it, and `admit_task` does no per-tier rate limiting.
> The metric was declared but the enforcement leg was never
> implemented. Surfaced again by the Redis-completeness audit.
>
> **Outcome we want:** a tier-N agent cannot spawn unlimited child
> tasks per second; admins can see quota pressure on dashboards
> and tune limits per tier.

---

## 1. The gap today

`bp_router/observability/metrics.py:88` declares the counter:

```python
router_quota_exceeded_total = Counter(
    "router_quota_exceeded_total",
    "Quota refusals at admit time.",
    ["counter", "level"],
)
```

`grep -rn "quota_exceeded_total" bp_router/` confirms zero call
sites that increment it. `admit_task` (`bp_router/tasks.py:58-`)
runs through ACL but no rate-limit gate. The `AdmitError`
docstring at `tasks.py:45` advertises *"ACL/quota/validation"* —
the quota leg is unimplemented.

PR #65 capped **task-tree depth** via `Settings.spawn_max_depth`
(WS-H2). That's a structural cap — independent of throughput —
so a flat fan-out at depth 1 is still unbounded. WS-H3 covers
the throughput axis.

## 2. What "quota" should mean

Two natural rate-limit axes, both per (user_id, level):

  - **Tasks-admitted-per-second**: the throughput cap. A
    runaway loop or adversarial agent can't drown the router.
  - **Concurrently-running-tasks**: the queue-depth cap. Even
    with healthy throughput, an agent with 10⁴ in-flight tasks
    can starve siblings. Postgres connection pool + WS outbox
    sizing are the real bottlenecks; this surfaces them
    explicitly.

The tier dimension matters because `admin` and `service`
principals need higher (or no) caps than `tier0..tierN` users.

## 3. Token-bucket vs leaky-bucket

For the throughput axis, **token bucket** is the natural choice:

  - Allows bursts up to bucket size while still honouring the
    long-term rate.
  - Per-key state is just `(tokens_remaining, last_refill_ts)` —
    fits easily in Redis with a Lua script for atomicity.
  - Time-windowed dashboards stay readable: rate of refusals over
    1m / 5m / 1h is straightforward.

Leaky bucket (request-queue) shape would be overengineered for
admit-time rejection — we don't want to hold requests, we want
to reject them so the caller backs off.

## 4. Storage: Redis vs in-memory

In-memory works for single-worker deployments, but the existing
deployment shape (multi-worker FastAPI + shared Redis for the
JWT revocation set) makes Redis the correct choice. Per-tier
limits live in `Settings`; per-(user_id, level) buckets live in
Redis with TTL = bucket-fill-time × 2 to bound the keyspace.

A shared Lua script wraps the read-update-write so two workers
admitting concurrent tasks can't both see "1 token left" and
both spend it.

## 5. Settings shape

```python
class Settings(BaseSettings):
    # Per-second rate at which the bucket refills. Per-tier
    # override via `_for_level(...)`. None = no limit (admin /
    # service defaults).
    quota_admit_rate_per_s: dict[str, Optional[float]] = {
        "admin":   None,
        "service": None,
        "tier0":   100.0,
        "tier1":    20.0,
        "tier2":     5.0,
        "tier3":     1.0,
    }
    # Burst size: how many tokens the bucket holds. Defaults to
    # 2× the per-second rate (~2 s of burst capacity).
    quota_admit_burst: dict[str, Optional[int]] = ...

    # Per-user maximum concurrent in-flight tasks. None = no cap.
    quota_concurrent_tasks: dict[str, Optional[int]] = {
        "admin":   None,
        "service": None,
        "tier0":   1_000,
        "tier1":     200,
        "tier2":      50,
        "tier3":      10,
    }
```

## 6. Wire shape

A new `AdmitError` code:

```python
raise AdmitError(
    "quota_exceeded",
    f"per-{counter} rate limit hit (level={level})",
)
```

Dispatch maps to HTTP 429 (with `Retry-After` from the bucket's
next-refill time) for the admin API path. WS-attached agents see
a result frame with `error.code = "quota_exceeded"` —
already-defined error code; just needs to start emitting.

## 7. Implementation plan

Three small PRs:

  1. **Settings + token-bucket helper.** Add `quota_admit_*` and
     `quota_concurrent_*` settings. New
     `bp_router/security/rate_limit.py` with a `TokenBucket`
     class and a `try_consume` method that honours Redis when
     configured / falls back to a **bounded per-process LRU**
     (`BoundedLRUDict`, cap `_MEM_FALLBACK_MAX = 50_000`) when
     not — an unbounded dict here would leak one entry per
     distinct key under a Redis outage. No call-site changes yet.

  2. **Wire into `admit_task`.** Insert the quota check between
     ACL (`step 3`) and depth check (`step 3c`). On rejection:
     `metrics.router_quota_exceeded_total.labels(...).inc()` and
     raise `AdmitError("quota_exceeded", ...)`.

  3. **Concurrent-tasks cap.** Maintain a Redis SET keyed on
     `quota:in_flight:{user_id}` of in-flight task_ids;
     `admit_task` SADDs, `complete_task` / `cancel_task` SREMs.
     Cap on SADD via `SCARD` check — Lua-scripted for atomicity.

## 8. Operational rollout

Per-tier `None` (no cap) is the safe default, so deployments
that don't configure caps keep working unchanged. Operators
opt in by setting `ROUTER_QUOTA_ADMIT_RATE_PER_S` in env or
via the admin UI's settings panel (when that lands).

Dashboards: `rate(router_quota_exceeded_total[5m])` panel per
counter / level. Alert when sustained > 0 — quota refusals are
admin-actionable (tune limits, identify the runaway agent).

## 9. Why this isn't being done now

Three reasons:

  1. **No deployment is hitting it.** The current operator
     uses dedicated agent fleets per tenant; the throughput
     axis isn't shared. Adding a quota layer without a real
     workload to validate it would mean shipping numbers
     pulled out of thin air.

  2. **Redis-or-in-memory ambiguity.** The right backend choice
     depends on whether this deployment has Redis available
     (multi-worker setup) or is single-worker (no Redis). The
     review fixes #62-#67 left that decision unresolved; deferring
     until at least one deployment commits.

  3. **Concurrent-tasks cap interacts with cancellation.** The
     SET-based tracking needs the cancel-result correctness work
     in PR #66 (synthetic Result on cancel-win) to be merged
     first, otherwise an abandoned task could keep consuming the
     concurrent-tasks slot until correlation_timeout. That
     prerequisite is now landed (✓), so the path is clear when
     someone wants to do this work.

## 10. Tracking

This doc is the durable home for the WS-H3 finding. When a
deployment needs quota enforcement, start here and walk the
implementation plan in §7.

## 12. What shipped (Redis-completeness PR)

§7 phase 1 + 2 are now in `main`:

  - `bp_router/security/rate_limit.py` — `TokenBucket` class with
    a Lua script for atomic check-and-deduct on Redis, plus a
    **bounded** per-process LRU fallback (`BoundedLRUDict`, cap
    `_MEM_FALLBACK_MAX = 50_000`) for single-worker deployments
    and Redis-outage degradation — bounded so a Redis blip can't
    leak the keyspace into RSS.
    Redis flake (`eval` raises) drops to the fallback path with a
    warning log, so a Redis blip doesn't cascade into a
    global admit-task outage.
  - `Settings.quota_admit_rate_per_s` / `quota_admit_burst` per-tier
    dicts (defaults match §5: admin / service uncapped, tier0…3
    descending). Cross-field validator
    `_quota_admit_rate_burst_paired` rejects half-set shapes
    (rate-without-burst, burst-without-rate, zero or negative)
    at startup.
  - `_redis_required_in_non_dev` Settings validator: rejects
    deployment_env in {staging, prod} when ROUTER_REDIS_URL is
    None. Closes the silent-revocation-bypass / silent-quota-
    fan-out foot-gun across multi-worker replicas. Single-worker
    dev still works without Redis.
  - `admit_task` consults `state.admit_quota.try_consume(...)`
    after the ACL gate (step 3c) and before the spawn-depth gate.
    `None` rate short-circuits before the bucket call so admin /
    service admits don't pay a Redis round-trip. On rejection:
    `metrics.quota_exceeded_total.labels(counter="admit_rate",
    level=level).inc()` and `AdmitError("quota_exceeded", ...,
    retry_after_s=d.retry_after_s)`.
  - `AdmitError.retry_after_s` field plumbed through the admin
    `POST /v1/admin/tasks/test` handler — emits
    `Retry-After: <seconds>` (rounded up per RFC 7231 §7.1.3) on
    HTTP 429 responses.
  - Real-Redis-protocol integration tests via `fakeredis` cover:
    JTI revocation round-trip, bucket drain → refill → re-consume,
    per-key isolation, atomic two-worker race, fallback on
    Redis flake. No mocks for the contract surface.
  - Live E2E in `scripts/run-test-agents.sh --run-quota-test`:
    fires N admit calls, asserts at least one 429 + matching
    `Retry-After` header + non-zero metric value.

What didn't ship:

  - **§7 phase 3** (concurrent-tasks SET-based cap). Same shape
    as the rate cap but scoped on simultaneous in-flight tasks
    rather than admit throughput. Dependencies all landed; pick
    up in a follow-up.
  - **§11** (per-agent inbound WS-frame rate cap). Less urgent
    after the M-3 `parent_agent_cache` fix removed the
    main DB-saturation amplifier. The same `TokenBucket` helper
    can host it when needed.

---

## 11. Related: H5 — per-agent inbound frame rate-limit (deferred)

A second-pass review surfaced a closely-related gap on the WS
side: there's no per-agent rate limit on **inbound** frames.
A chatty (or adversarial) agent can pump frames at the speed
of `parse_frame` and saturate the dispatcher. Two amplifiers
make this a clean DoS vector:

  - `_handle_progress` (`bp_router/dispatch.py:197-214`) runs
    a fresh `pool.acquire()` + JOIN against `tasks` for every
    inbound `ProgressFrame` to find the parent agent. At line
    rate this saturates the DB connection pool.
  - `_handle_llm_request` spawns a router-side asyncio.Task
    per request that calls upstream LLM providers; provider
    rate-limit pushback then propagates as `429`s the operator
    has to triage.

These are the same shape as WS-H3 (admit-time quota): a
token-bucket per (agent_id) keyed on inbound-frame timestamps,
with rejection on exhaustion. Two natural axes:

  - **Frames-per-second**: catch chatty Progress / LLM
    requesters.
  - **Concurrent in-flight LLM requests per agent**: bound the
    fan-out to upstream providers so one runaway agent can't
    starve the others.

### 11.1 Why bundled here

Both rate-limit dimensions (admit-time + per-frame) want the
same `TokenBucket` infrastructure. Implementing two separate
buckets when the underlying primitive is identical would be
duplication. The §7 plan adds the bucket helper in PR #1; PR #2
wires admit-time; a new PR #4 would wire WS recv-loop side.

### 11.2 Settings shape

Extends the §5 settings:

```python
class Settings(BaseSettings):
    # Existing admit-side quotas (§5).
    quota_admit_rate_per_s: dict[str, Optional[float]] = ...

    # NEW: per-agent inbound frame rate. None = no limit.
    # Independent of admit quota — agent-level not user-level
    # since the throttle protects the dispatcher's recv loop
    # which is per-WS-socket. Kept separate from `admit` so an
    # admin agent (no admit cap) can still be rate-limited if
    # it spams frames.
    quota_ws_frames_per_s: Optional[float] = 100.0
    quota_ws_frames_burst: Optional[int] = 200

    # NEW: concurrent in-flight LLM requests per agent. None =
    # no cap. Default low because each in-flight request holds
    # a router-side asyncio.Task + a provider connection.
    quota_concurrent_llm_per_agent: Optional[int] = 10
```

### 11.3 Wire shape

The frame-rate cap lives in `_recv_loop`'s tight loop in
`bp_router/ws_hub.py`:

```python
async def _recv_loop(entry, state):
    bucket = state.frame_buckets.for_agent(entry.agent_id)
    while not entry.closed.is_set():
        text = await entry.websocket.receive_text()
        if not bucket.try_consume(1):
            # Drop the frame (silent — loud logging would itself
            # be a log-spam DoS vector). Increment a metric
            # counter so operators can see the rejection rate.
            metrics.ws_frame_rate_limited_total.labels(
                agent_id=entry.agent_id
            ).inc()
            continue
        # ... parse + dispatch as today
```

The concurrent-LLM cap lives in `_handle_llm_request`'s entry —
acquire a semaphore from `state.llm_semaphores[agent_id]`,
release on completion, refuse with `quota_exceeded` typed code
when full.

### 11.4 Knock-on: cache parent_agent_id (✓ landed)

**Status:** ✓ landed as the third-pass review's M-3 fix.

`_handle_progress` now consults `state.parent_agent_cache`
populated at admit time (parent_agent_id IS the caller_agent_id;
immutable for the task's lifetime), evicted on terminal-state
transition via `_notify_task_terminal`. SQL fallback covers
multi-worker / pre-restart cases and back-fills the cache. With
this, the per-frame DB JOIN that motivated the H5 frame-rate cap
is gone for any task admitted on the same worker — a chatty
agent's Progress frames are now O(1) dict lookups. The frame-rate
cap proposal here remains relevant for protecting the recv loop's
parse-and-dispatch cost, but the dispatcher saturation under
sustained Progress traffic is no longer the gating concern.

### 11.5 Why deferred (same as WS-H3)

  - **No deployment is hitting it.** The current operator runs
    dedicated agent fleets per tenant; throughput axis isn't
    shared.
  - **Settings defaults need real workloads to validate.**
    Setting "100 frames/sec" out of thin air would either be
    too tight (legitimate UI agents streaming progress get
    throttled) or too loose (DoS vector remains).
  - **Cache-invalidation correctness for parent_agent_id
    interacts with the cancel-result work in PR #66.** That
    prerequisite has landed; the path is clear when someone
    wants to do this work.

### 11.6 Tracking

H5 follows the same lifecycle as WS-H3: durable here until a
deployment commits. Operators noticing dispatcher saturation
under high-frequency Progress traffic should start with §11.4
(parent_agent_id cache) — it's a one-PR fix that buys 90% of
the headroom without the policy work.
