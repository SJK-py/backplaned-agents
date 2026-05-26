"""bp_router.observability.metrics — Prometheus metric registry.

See `docs/observability.md` §4 for the canonical metric set.
The registry is a module-level singleton so any subsystem can import
and increment without dependency injection.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Module-level registry. Tests reset by replacing with a fresh
# CollectorRegistry and re-creating the metric handles.
REGISTRY = CollectorRegistry()


# ---------------------------------------------------------------------------
# Frame / WS
# ---------------------------------------------------------------------------

frames_total = Counter(
    "router_frames_total",
    "WebSocket frames sent or received, by direction/type.",
    # Deliberately NOT labelled by `agent_id`.
    # Agent IDs are caller-supplied and can be ephemeral (test
    # fleets, evicted-then-rejoined agents under new ids), so
    # adding the label creates an unbounded series count that the
    # Prometheus client never expires — RSS bloat, scrape size
    # bloat, query latency on /metrics. Per-agent rates aren't
    # actionable from /metrics anyway; observe via tracing/logs.
    # Mirrors the careful exclusion in `acl_decisions_total`,
    # `task_state_transitions_total`, `llm_calls_total` etc.
    ["direction", "type"],
    registry=REGISTRY,
)
frame_size_bytes = Histogram(
    "router_frame_size_bytes",
    "WebSocket frame size in bytes.",
    ["direction", "type"],
    buckets=(1024, 4096, 16384, 65536, 262144, 1_048_576),
    registry=REGISTRY,
)
ws_connected_agents = Gauge(
    "router_ws_connected_agents_count",
    "Currently connected agent sockets.",
    registry=REGISTRY,
)
ws_disconnects_total = Counter(
    "router_ws_disconnects_total",
    "Agent socket disconnects, by reason.",
    ["reason"],
    registry=REGISTRY,
)
ws_unknown_correlation_total = Counter(
    "router_ws_unknown_correlation_total",
    "Inbound frames whose ref_correlation_id is NOT in the socket's "
    "inflight_correlations set. Bounded cardinality (`frame_type` is "
    "an enum, deliberately no `agent_id` label). Sustained non-zero "
    "rate is operator-actionable: either a peer is sending bogus "
    "ref_correlation_ids (potential abuse), or our bookkeeping has "
    "desynced (potential bug). The drop itself is intentional "
    "this metric just makes the rate visible.",
    ["frame_type"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

task_state_transitions_total = Counter(
    "router_task_state_transitions_total",
    "Task state transitions.",
    ["from", "to"],
    registry=REGISTRY,
)
task_duration_seconds = Histogram(
    "router_task_duration_seconds",
    "Task duration from creation to terminal state.",
    ["terminal_state"],
    registry=REGISTRY,
)
task_active_count = Gauge(
    "router_task_active_count",
    "Tasks currently in a non-terminal state.",
    ["state"],
    registry=REGISTRY,
)
# asyncpg pool occupancy, sampled once per timeout-sweep tick (~5s).
# The default 10-conn pool is the most likely first-week
# bottleneck (delegation ack-storm, fleet reconnect, chatty
# Progress fan-out). Without this, pool exhaustion is invisible
# until requests start timing out. Alert when
# `in_use / max` stays near 1. Bounded label `state` ∈
# {in_use, idle, max} — no per-conn cardinality.
db_pool_connections = Gauge(
    "router_db_pool_connections",
    "asyncpg pool connection count by state (sampled per sweep).",
    ["state"],
    registry=REGISTRY,
)
# `complete_task` drops a Result whose reporting agent isn't the
# task's current `active_agent_id`. Bounded label `reporter`:
#   - `owning`: the original task `agent_id` reported late after a
#     legitimate hand-off — benign/expected (the delegate produces
#     the real Result).
#   - `other`: any other mismatch — the actionable signal,
#     including the rare delegation race where the delegate
#     finishes + reports between its ack and the active-agent flip
#     commit (the task then hangs until the deadline sweep). NO
#     `agent_id`/`task_id` labels — same unbounded-cardinality
#     discipline as `frames_total` / `task_state_transitions_total`.
result_from_wrong_agent_total = Counter(
    "router_result_from_wrong_agent_total",
    "Terminal Results dropped because the reporter wasn't the "
    "task's active executor, by reporter class.",
    ["reporter"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# ACL / quotas
# ---------------------------------------------------------------------------

acl_decisions_total = Counter(
    "router_acl_decisions_total",
    "ACL evaluation outcomes.",
    ["decision", "effect", "rule_name"],
    registry=REGISTRY,
)
quota_exceeded_total = Counter(
    "router_quota_exceeded_total",
    "Quota check denials.",
    ["counter", "level"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# DB / storage
# ---------------------------------------------------------------------------

db_query_duration_seconds = Histogram(
    "router_db_query_duration_seconds",
    "Time spent on a single DB query.",
    ["query"],
    registry=REGISTRY,
)
storage_bytes_total = Counter(
    "router_storage_bytes_total",
    "Bytes uploaded or downloaded by backend and operation.",
    ["backend", "op"],
    registry=REGISTRY,
)
storage_op_duration_seconds = Histogram(
    "router_storage_op_duration_seconds",
    "Time spent on a single storage operation.",
    ["backend", "op"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "router_llm_calls_total",
    "LLM calls by model alias, provider, and status.",
    ["model", "provider", "status"],
    registry=REGISTRY,
)
llm_tokens_total = Counter(
    "router_llm_tokens_total",
    "LLM tokens consumed.",
    ["model", "direction"],
    registry=REGISTRY,
)
llm_cost_microusd_total = Counter(
    "router_llm_cost_microusd_total",
    "LLM cost in micro-USD.",
    ["model"],
    registry=REGISTRY,
)


# Fallback chain — observability for the retry/fallback machinery.
#
# `outcome` labels for `llm_fallback_attempts_total`:
#   `success`     — adapter call returned a result
#   `retry`       — adapter raised; another attempt at the same preset followed
#   `failed`      — adapter raised on the LAST attempt for this preset; chain
#                   walks to fallback_preset (or exhausts if there's none)
#   `setup_retry` — streaming path only: provider stream SETUP failed and
#                   the SAME preset was re-issued (distinct from `retry`,
#                   which is the non-stream attempt loop). Emitted by
#                   `LlmService._generate_stream_with_setup_retry`.
#
# Sum across `outcome` to get total attempts; the four give per-preset
# health: success rate, (stream-setup) retry pressure, fallback-trigger.
llm_fallback_attempts_total = Counter(
    "router_llm_fallback_attempts_total",
    "LLM call attempts within the retry/fallback wrapper.",
    ["preset", "outcome"],
    registry=REGISTRY,
)
# Increments once per request when the entire chain (root + every
# fallback target reached) failed. `root_preset` is what the caller
# asked for; the rest of the chain is implied by the in-memory map.
llm_fallback_chain_exhausted_total = Counter(
    "router_llm_fallback_chain_exhausted_total",
    "LLM calls where the full fallback chain failed.",
    ["root_preset"],
    registry=REGISTRY,
)
# Increments once per request when the call succeeds via a NON-root
# preset — i.e. the fallback chain saved the request. Useful for
# alerting on degraded primary providers.
llm_fallback_used_total = Counter(
    "router_llm_fallback_used_total",
    "LLM calls that succeeded via a fallback (not the requested preset).",
    ["root_preset", "winning_preset"],
    registry=REGISTRY,
)
# Increments when a mid-chain fallback target is silently skipped
# because the user's tier doesn't satisfy `min_user_level`. Only
# triggered for fallback targets — first-preset denials are surfaced
# to the caller as `preset_not_allowed` and don't increment this.
llm_fallback_skipped_tier_total = Counter(
    "router_llm_fallback_skipped_tier_total",
    "Fallback targets skipped because the caller's tier didn't satisfy min_user_level.",
    ["preset"],
    registry=REGISTRY,
)
# Increments when `_call_with_fallback` refuses to fall back to a
# different-provider preset while messages carry tool_call_ids
# generated by the root provider. Sending the stale IDs through
# would 400 the downstream provider ("no such tool_use_id /
# unknown call_id"); skipping protects the call from a broken hop.
# `root_provider` and `fallback_provider` labels expose the
# cross-provider attempt so operators can spot misconfigured
# chains (e.g. an anthropic preset listing an openai fallback).
llm_fallback_skipped_provider_total = Counter(
    "router_llm_fallback_skipped_provider_total",
    "Fallback hops skipped because they would cross provider with stale tool-call IDs.",
    ["preset", "root_provider", "fallback_provider"],
    registry=REGISTRY,
)
# Increments on any preset_not_allowed denial — the caller's first
# requested preset failed the tier gate. Useful for capacity planning
# (admins can see which restricted presets are getting hit).
llm_tier_gate_denied_total = Counter(
    "router_llm_tier_gate_denied_total",
    "First-preset tier-gate denials surfaced as preset_not_allowed.",
    ["preset"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# User lifecycle
# ---------------------------------------------------------------------------

users_soft_deleted_total = Counter(
    "router_users_soft_deleted_total",
    "Admin user soft-deletes (DELETE /v1/admin/users/{id} success path).",
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# AgentInfoUpdate (Phase 10e)
# ---------------------------------------------------------------------------

agent_info_update_total = Counter(
    "router_agent_info_update_total",
    "AgentInfoUpdate frames by outcome (accepted / rejected / rate_limited).",
    ["outcome"],
    registry=REGISTRY,
)


# Per-actor dampener drops on denial-audit writes. Sustained
# non-zero rate indicates a service principal flooding a mint
# endpoint that they're not authorised on (`not_serviced_by`)
# or that's per-target rate-limited. The audit row is the
# operator's view of the abuse; the dampener bounds the rate
# at which the chain writer pays the hash-chain cost.
audit_denials_dropped_total = Counter(
    "router_audit_denials_dropped_total",
    "Denial-audit writes dropped by the per-actor dampener.",
    ["event"],
    registry=REGISTRY,
)

# User-level cache observability. `peek_user_level_cached` is on
# the auth hot path (every request) and `_user_level_cache`'s
# eviction policy is LRU-by-resolve (the touch happens on
# `resolve_user_level`, not on peek). Peek-heavy users (admin UI
# sessions that never make LLM calls) get no touch and can fall
# out of the cache while resolve-heavy callers push them aside.
# The metric makes that risk observable: a sustained `outcome=miss`
# rate against admin-UI traffic signals the cap needs raising or
# the LRU policy needs adjusting.
user_level_cache_peek_total = Counter(
    "router_user_level_cache_peek_total",
    "Outcomes of `peek_user_level_cached` calls "
    "(hit / miss / expired / no_user).",
    ["outcome"],
    registry=REGISTRY,
)

# Per-frame-type drop counter for `deliver_frame` / `fanout_frame`
# QueueFull saturations. Operators care most about `Result`
# saturations — those are the terminal task notification. Sustained
# non-zero rate signals agents are too slow on draining their
# outbox; calling agents that miss a Result frame can recover by
# polling task state via the admin API but the missed-on-wire
# event itself is silent without this metric. R6 third-pass review.
deliver_frame_dropped_total = Counter(
    "router_deliver_frame_dropped_total",
    "Frames dropped on per-socket outbox saturation, by frame type.",
    ["frame_type"],
    registry=REGISTRY,
)


# Hit/miss of the short-TTL single-flight `list_agents` cache that
# fronts the WS-handshake catalog build. Under a fleet reconnect
# storm a healthy cache shows ~1 miss per TTL with the rest hits;
# a sustained miss rate means TTL is too short (or 0) and the
# O(N²) handshake scan is back. Bounded label set {hit, miss}.
ws_handshake_catalog_cache_total = Counter(
    "router_ws_handshake_catalog_cache_total",
    "WS-handshake agent-catalog cache lookups, by result.",
    ["result"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Setup / exposition
# ---------------------------------------------------------------------------


def configure_metrics() -> None:
    """No-op for now; the registry is populated at import time. Reserved
    for future setup (default labels, label allowlist enforcement)."""
    return None


def render_exposition() -> bytes:
    """Render the Prometheus text exposition for the /metrics endpoint."""
    return generate_latest(REGISTRY)


# ---------------------------------------------------------------------------
# Redis health
# ---------------------------------------------------------------------------
#
# When the JWT revocation lookup or admit-quota bucket hits a Redis
# exception, the code falls back to in-process behaviour (token not
# revoked / per-process token bucket). Without these metrics, operators
# only learned about the degraded mode when something downstream broke.
#
# Single alert rule covers both subsystems:
#   `router_redis_health == 0 OR rate(router_redis_fallback_total[1m]) > 0`
redis_health = Gauge(
    "router_redis_health",
    "1 = Redis reachable; 0 = unreachable / in-process fallback in use.",
    registry=REGISTRY,
)
redis_fallback_total = Counter(
    "router_redis_fallback_total",
    "Operations that fell back to in-process behaviour on Redis errors.",
    ["subsystem"],
    registry=REGISTRY,
)
