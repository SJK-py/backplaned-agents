# Observability — Traces, Logs, Metrics

> Conventions for the three observability pillars across router and SDK.
> Companion to [`router/storage.md §4`](./router/storage.md#4-observability),
> which introduced the topic. This document is prescriptive: span names,
> required attributes, log fields, metric names. Deviations break
> dashboards and alerts.

> **Implementation status (router).** This document is the target
> spec; current coverage differs by pillar. **Logs:** implemented —
> structured `event` + `error_code` + `task_id`/`correlation_id`.
> **Metrics:** partially implemented (the table below marks unbuilt
> entries `# planned`). **Traces:** *specification only* —
> `configure_tracing` wires an OTLP exporter, but **no spans are
> emitted anywhere** and every frame's `trace_id` is a constant
> placeholder (`"0" * 32`). Setting `ROUTER_OTEL_ENDPOINT` therefore
> yields an empty trace stream. Until spans exist, correlate via the
> structured-log `event` + `task_id`/`correlation_id` + `error_code`
> stream — **not** traces or `trace_id`. The span/trace conventions
> below (O1/O2/O5 and §2) describe the intended design, not current
> behaviour.

## 1. Principles

**O1. On by default.** No agent or operator should have to opt in.
Disabling is possible (`OTEL_SDK_DISABLED=true`) but not the default.

**O2. Trace-id everywhere.** Every WebSocket frame, every HTTP
request, every log line, every metric label set carries the
originating trace context. Correlation is non-negotiable.

**O3. Cardinality budgets.** No metric label may carry unbounded
values (`task_id`, `user_id`, free-form strings). Logs and traces
are the place for high-cardinality data, not metrics.

**O4. Privacy by default.** No prompt content, no PII, no file
content, no auth tokens in any pillar. Hash or omit. Operators can
opt into prompt logging at higher levels for development.

**O5. Same conventions, both sides.** Router and SDK emit the same
span names, the same attribute keys, the same log fields. An agent's
spans nest under the router's spans automatically through OTel
context propagation.

## 2. Tracing

### 2.1 Propagation

Every `NewTask` frame carries `trace_id` and `span_id` (`protocol.md`
§2.1). The router and SDK attach OTel context using these values:

- **Root.** A user-initiated session opens a root span at the
  router's HTTP edge (login or session-open endpoint). Its
  `trace_id` is propagated to the orchestrator's first `NewTask`.
- **Inheritance.** Each `NewTask` becomes a child span of its
  parent. The frame carries the parent span's id; the receiver
  starts a new span with the same `trace_id` and a fresh `span_id`.
- **Async wait (target spec; not emitted — see status note above).**
  A parent that has spawned children keeps its span open until it
  reaches a terminal state; children's spans are siblings under it.
  Note there is no `WAITING_CHILDREN` runtime transition (see
  [`router/state.md`](./router/state.md)) — with `wait=True` the
  parent simply stays `RUNNING` while its handler awaits the child
  `Result`.

### 2.2 Span names

```
router.session.open
router.session.close
router.task.dispatch          # router admits a NewTask
router.task.transition        # state-machine transition
router.acl.evaluate
router.frame.send
router.frame.recv
router.db.query
router.storage.put
router.storage.get
router.llm.call               # router-side LLM service span
sdk.handler                   # span around user handler invocation
sdk.peer.spawn
sdk.peer.delegate
sdk.llm.call
sdk.files.fetch
sdk.files.put
```

Names use `dot.separated.lowercase`. The first segment identifies
the emitter (`router` / `sdk`); the rest is hierarchical action.

### 2.3 Required attributes

Every span carries:

| Attribute              | Type   | Notes                                       |
| ---------------------- | ------ | ------------------------------------------- |
| `service.name`         | string | `"router"` or `"sdk"`                       |
| `service.instance.id`  | string | Worker / agent process id                   |
| `deployment.env`       | string | `"prod"`, `"staging"`, etc.                 |
| `protocol.version`     | string | Frame protocol version                      |

Spans for in-task work add:

| Attribute           | Type   | Notes                                       |
| ------------------- | ------ | ------------------------------------------- |
| `bp.task_id`        | string | Current task                                |
| `bp.parent_task_id` | string | If any                                      |
| `bp.user_id`        | string | First-class user                            |
| `bp.session_id`     | string |                                             |
| `bp.agent_id`       | string | Agent owning the span                       |
| `bp.frame.type`     | string | For frame.send / frame.recv                 |
| `bp.acl.rule_name`  | string | For acl.evaluate                            |
| `bp.acl.effect`     | string | `"allow"` or `"deny"`                       |
| `bp.state.from`     | string | For task.transition                         |
| `bp.state.to`       | string |                                             |
| `bp.llm.model`      | string | For llm.call (alias, not raw provider id)   |
| `bp.llm.tokens.in`  | int    | For llm.call                                |
| `bp.llm.tokens.out` | int    | For llm.call                                |

The `bp.` prefix avoids clashes with the OpenTelemetry semantic
conventions namespace.

### 2.4 Span events vs. child spans

- Use **child spans** for operations with measurable duration
  (`db.query`, `llm.call`, `peer.spawn`).
- Use **span events** for instantaneous occurrences inside a span
  (`frame_acked`, `cache_hit`, `quota_check_passed`).

Don't create child spans for trivially short operations; they bloat
the trace UI.

### 2.5 Sampling

- **Errors:** always sampled. A 5xx span pulls the entire trace.
- **Tail-based sampling** is recommended in production: sample
  100% locally, ship to a collector that retains errors and high-
  latency traces and downsamples the rest.
- **Default head sampler** for low-traffic deployments: 10%.

## 3. Structured logs

### 3.1 Format

JSON, one object per line, stdout. No multi-line logs. No
`print()` calls anywhere — CI lints for it. Standard library
`logging` is configured with a JSON formatter at process startup.

### 3.2 Required fields

```jsonc
{
  "ts": "2026-04-26T12:34:56.123456Z",
  "level": "INFO",
  "logger": "router.dispatch",
  "trace_id": "...",
  "span_id": "...",
  "event": "task_dispatched",
  "service": "router",
  "service.instance.id": "router-7c4a",
  "deployment.env": "prod"
}
```

Plus contextually-bound fields where applicable: `bp.task_id`,
`bp.user_id`, `bp.session_id`, `bp.agent_id`. The SDK's `ctx.log`
is pre-bound with these.

### 3.3 `event` field

Lowercase, snake_case, action-like:

```
session_opened           session_closed
task_admitted            task_dispatched
task_transitioned        task_completed
frame_received           frame_sent           frame_dropped
acl_decision             quota_exceeded
agent_connected          agent_disconnected   agent_resumed
storage_uploaded         storage_deleted
llm_call_start           llm_call_finished
handler_error            transport_error
```

Free-form messages are permitted in the `message` field for human
readers, but the canonical signal is `event` plus structured fields.

### 3.4 Levels

| Level    | Use                                                      |
| -------- | -------------------------------------------------------- |
| DEBUG    | Verbose diagnostics; off in production.                  |
| INFO     | Normal lifecycle events (connect, dispatch, complete).   |
| WARNING  | Recoverable anomalies (retry, fallback, slow path).      |
| ERROR    | Task failures, transport failures, validation rejects.   |
| CRITICAL | Process-level emergencies (DB unreachable, OOM).         |

### 3.5 Privacy

By default, the SDK and router log:

- Prompts: only token counts and a 16-char truncated SHA-256.
- LLM responses: only token counts and finish reason.
- File contents: never. Filenames and SHA-256 only.
- Auth tokens / API keys: never. Even on errors.

A development-only setting (`ROUTER_LOG_PROMPTS=true` — Pydantic
Settings under the `ROUTER_` env prefix) enables full prompt
logging. Refused in production by a startup check that rejects the
flag if `ROUTER_DEPLOYMENT_ENV=prod`.

## 4. Metrics

### 4.1 Naming

Prometheus exposition. Names follow `<service>_<subject>_<unit>`,
lowercased. Metrics in **bold** are emitted today; the rest are
declared in the registry but not yet incremented from the code path
they describe — they are the target shape for follow-up
instrumentation work.

Exhaustively mirrors the registry in
`bp_router/observability/metrics.py` (32 `router_*` metrics).
**Bold** = incremented from the code path it describes today; the
rest are registered with the target shape but not yet emitted.

```
**router_frames_total{direction, type}**                                counter       # NO agent_id label — unbounded cardinality
router_frame_size_bytes{direction, type}                                histogram     # planned
**router_ws_connected_agents_count**                                    gauge
**router_ws_disconnects_total{reason}**                                 counter
**router_ws_unknown_correlation_total{frame_type}**                     counter       # inbound ref_correlation_id not in inflight set
**router_task_state_transitions_total{from, to}**                       counter
router_task_duration_seconds{terminal_state}                            histogram     # planned
router_task_active_count{state}                                         gauge         # planned
**router_db_pool_connections{state}**                                   gauge         # state ∈ in_use | idle | max; sampled per sweep tick
**router_result_from_wrong_agent_total{reporter}**                      counter       # reporter ∈ owning | other; Result dropped, reporter != active executor
**router_acl_decisions_total{decision, effect, rule_name}**             counter
**router_quota_exceeded_total{counter, level}**                         counter       # per-(user,level) admit-rate bucket denials
router_db_query_duration_seconds{query}                                 histogram     # planned
router_storage_bytes_total{backend, op}                                 counter       # planned
router_storage_op_duration_seconds{backend, op}                         histogram     # planned
**router_llm_calls_total{model, provider, status}**                     counter
**router_llm_tokens_total{model, direction}**                           counter
router_llm_cost_microusd_total{model}                                   counter       # planned (cost calc)
**router_llm_fallback_attempts_total{preset, outcome}**                 counter       # outcome ∈ success | retry | failed | setup_retry
**router_llm_fallback_chain_exhausted_total{root_preset}**              counter       # full chain failed
**router_llm_fallback_used_total{root_preset, winning_preset}**         counter       # rescued by a non-root preset
**router_llm_fallback_skipped_tier_total{preset}**                      counter       # mid-chain target skipped (tier mismatch)
**router_llm_fallback_skipped_provider_total{preset, root_provider, fallback_provider}**  counter  # mid-chain target skipped (provider mismatch)
**router_llm_tier_gate_denied_total{preset}**                           counter       # first-preset preset_not_allowed

**router_users_soft_deleted_total**                                     counter       # admin DELETE /v1/admin/users/{id}
**router_agent_info_update_total{outcome}**                             counter       # outcome ∈ accepted | rejected | rate_limited
**router_audit_denials_dropped_total{event}**                           counter       # audit write dropped on a denial path
**router_user_level_cache_peek_total{outcome}**                         counter       # LlmService user-level cache hit/miss
**router_deliver_frame_dropped_total{frame_type}**                      counter       # per-socket outbox saturation, per frame type
**router_ws_handshake_catalog_cache_total{result}**                     counter       # result ∈ hit | miss; single-flight list_agents cache
**router_redis_health**                                                 gauge         # 1 = reachable; 0 = degraded / in-process fallback
**router_redis_fallback_total{subsystem}**                              counter       # subsystem ∈ rate_limit | jwt_revocation; Redis op fell back

sdk_handler_duration_seconds{agent_id, status}                          histogram     # planned (SDK-side, not the router registry)
sdk_pending_acks_count{agent_id}                                        gauge         # planned (SDK-side)
sdk_pending_results_count{agent_id}                                     gauge         # planned (SDK-side)
sdk_reconnects_total{agent_id, reason}                                  counter       # planned (SDK-side)
```

> **`router_deliver_frame_dropped_total{frame_type}`.** Operators
> should alert on the `Result`-typed series specifically. Each
> increment represents a terminal task notification that didn't
> reach the calling agent on the wire (the task row in the DB is
> still authoritative — agents can reconcile via the admin API
> after reconnect). Durable Result-frame replay was considered
> and deferred — see `bp_router/delivery.py` module docstring for
> the design discussion and reopen criteria. `Progress`-typed
> drops are best-effort by design (`docs/router/protocol.md §4.5`)
> and don't typically warrant alerts.

> **`/metrics` access.** The Prometheus exposition is bearer-gated
> via `ROUTER_METRICS_TOKEN` (compared with `hmac.compare_digest`).
> A model_validator REJECTS startup with `deployment_env in
> {staging, prod}` if no token is set — open `/metrics` leaks the
> agent ID list and per-endpoint counters. Configure Prometheus
> scrapes with `Authorization: Bearer <token>`.

The "planned" rows are stable names — emit them when the
instrumentation lands and dashboards built against them keep working.

### 4.2 Cardinality rules

Permitted as labels: `direction`, `type`, `state`, `effect`,
`decision`, `rule_name`, `reason`, `outcome`, `result`,
`reporter`, `subsystem`, `frame_type`, `model` (alias, not raw),
`provider`, `status`, `backend`, `op`, `preset`, `root_preset`,
`winning_preset`, `query`, `event`, `level` (bounded set: admin,
service, tier0, tier1, ...).

**Forbidden** as labels: `agent_id`, `task_id`, `user_id`,
`session_id`, `trace_id`, `correlation_id`, raw error strings,
free-form prompts, file paths. `agent_id` in particular is
caller-supplied and ephemeral (test fleets, evicted-then-rejoined
ids) — the registry deliberately excludes it everywhere
(`frames_total` carries the canonical rationale). The SDK-side
`sdk_*` metrics above DO carry `agent_id`, but those live in the
SDK process, not the router registry, and are out of scope for
this allowlist.

The Pydantic Settings layer enforces an allowlist of label keys per
metric at registration time; new labels require code review.

### 4.3 Histogram buckets

- Latencies: default OTel buckets (`5ms, 10ms, 25ms, 50ms, 100ms,
  250ms, 500ms, 1s, 2.5s, 5s, 10s, 30s`).
- Sizes: `1KB, 4KB, 16KB, 64KB, 256KB, 1MB`.

Don't tune buckets per-metric without a strong reason — uniform
buckets make cross-metric comparison easier.

## 5. Dashboards and SLOs — planned

**Status: not yet shipped.** A `dashboards/` directory of Grafana
JSON definitions, keyed off the metric names above, is the target.
Until those JSON files are committed, deployments build their own
panels. The recommended panel set follows; treat it as the
specification dashboards should evolve toward.

**Router health (one row per panel):**

- `router_ws_connected_agents_count` over time
- p50 / p95 / p99 of `router_task_duration_seconds` by terminal_state
- Rate of `router_task_state_transitions_total{to="failed"}`
- Rate of `router_acl_decisions_total{effect="deny"}`
- Top 10 `(rule_name, agent_id)` pairs in deny logs

**Per-user / per-tier:**

- Task admit rate by `level`
- Quota exhaustion rate by `counter` and `level`
- LLM cost burn (`router_llm_cost_microusd_total`)

**LLM fallback chain (alert when primary providers degrade):**

- `rate(router_llm_fallback_used_total[5m]) > 0` — fallback rescued a
  request; primary preset is degraded.
- `rate(router_llm_fallback_chain_exhausted_total[5m]) > 0` — entire
  chain failed; either an upstream-wide outage or a misconfigured
  fallback chain.
- `sum by (preset) (rate(router_llm_fallback_attempts_total{outcome="retry"}[5m]))`
  — per-preset retry pressure. A spike means the preset's upstream is
  flapping.
- `sum by (preset) (rate(router_llm_fallback_attempts_total{outcome="setup_retry"}[5m]))`
  — streaming setup-retry rate (PR #3 of the M6 sequence). Each
  increment means the wrapper failed before delivering any deltas
  and re-issued the SAME preset. Diverges from `outcome="retry"`
  on streaming-only fault patterns; useful when a provider's
  streaming endpoint is degraded but its non-streaming path is
  fine.
- `rate(router_llm_tier_gate_denied_total[5m]) > 0` — clients are
  hitting tier-gated presets they can't access; could be misconfigured
  routing or a privilege demotion that hasn't propagated.

**Typed `LLM_*` error codes (M6).** The `LlmResultFrame.error.code`
vocabulary is documented in `docs/sdk/services.md` §1.1. The
`router_llm_calls_total{status="error"}` counter doesn't yet carry
the typed code as a label (planned), so for now operators alert on
the typed codes via the structured log stream.

The router emits an `llm_call_failed` log on every failed attempt
inside `_call_with_fallback` with these fields:

```jsonc
{
  "event": "llm_call_failed",
  "preset": "claude-haiku",
  "attempt": 2,
  "max_attempts": 3,
  "error_code": "upstream_rate_limited",   // ErrorCode.LLM_*
  "upstream_class": "RateLimitError",      // telemetry only
  "error_message": "...",
  "fallback_pending": true                 // about to walk to fallback_preset
}
```

A typical log → metric pipeline (Promtail / Vector / Logstash)
expands these into a `log_llm_call_failed_total` counter labelled
on `error_code` and `preset`. The PromQL below assumes that
expansion — substitute your pipeline's naming if it differs.

```promql
# Rate of rate-limit hits across all presets — early signal that
# an upstream is throttling us.
sum(rate(log_llm_call_failed_total{error_code="upstream_rate_limited"}[5m]))

# Per-preset auth failures — operator must rotate the key.
sum by (preset) (
  rate(log_llm_call_failed_total{error_code="upstream_auth_failed"}[15m])
) > 0

# Mid-stream drops — agents got partial output. NOT retriable; if
# this rate is non-trivial, look at the streaming adapter path or
# the upstream's connection-stability story.
rate(log_llm_call_failed_total{error_code="stream_interrupted"}[5m]) > 0.01
```

**Chain exhaustion by code (planned).** When
`llm_fallback_chain_exhausted_total` gains a `code` label
(currently labelled by `root_preset` only), the same shape
becomes a direct counter alert without going through logs:

```promql
# Entire fallback chain exhausted with the same upstream
# category — a multi-provider outage rather than one bad preset.
sum by (code) (
  rate(router_llm_fallback_chain_exhausted_total[5m])
) > 0.05
```

Until that label lands, derive the same view from
`llm_call_failed{fallback_pending=false}` events that fire on the
chain's last preset.

**Recommended alert thresholds** (tune per traffic volume):

| Symptom | PromQL | Threshold | Severity |
| --- | --- | --- | --- |
| Upstream rate-limit storm | `rate(log_llm_call_failed_total{error_code="upstream_rate_limited"}[5m])` | > 1/s for 5m | warning |
| Auth credential rotation needed | `rate(log_llm_call_failed_total{error_code="upstream_auth_failed"}[15m])` | > 0 for 15m | page |
| Provider-wide outage (chain exhausted) | `rate(router_llm_fallback_chain_exhausted_total[5m])` | > 0.1/s for 2m | page |
| Streaming-path degradation | `rate(router_llm_fallback_attempts_total{outcome="setup_retry"}[5m])` | > 0.5/s for 5m | warning |
| Mid-stream drops impacting agents | `rate(log_llm_call_failed_total{error_code="stream_interrupted"}[5m])` | > 0.01/s for 10m | warning |
| Quota exhausted (admin action required) | `rate(log_llm_call_failed_total{error_code="upstream_quota_exhausted"}[1m])` | > 0 | page |

The non-retriable codes (`upstream_auth_failed`,
`upstream_quota_exhausted`, `upstream_content_filter`) are
operator-actionable on first occurrence — paging on them is
appropriate. The retriable codes are best alerted on rate /
sustained pressure, not point events, since the SDK and router
absorb single transients silently.

`upstream_quota_exhausted` and `upstream_content_filter` are
**reserved** in the protocol but not emitted by any classifier
today. Pre-wire the alerts so they fire when the codes start
landing rather than scrambling later.

**SLOs (deployment-local; documented as defaults):**

- 99% of `task.dispatch` admits within 100 ms (router-side).
- 99% of frame acks within 1 s.
- < 0.1% of tasks reach `failed` due to `transport_error`.
- < 1% of agent reconnects within a 5-minute window per agent_id.

## 6. Local development — planned

**Status: not yet shipped.** A `docker-compose.observability.yml`
that brings up Jaeger, Prometheus, Grafana, and (optionally) Loki
is the target — but the file isn't in the repo today. Local
operators can point the router at any OTLP-compatible collector
via `ROUTER_OTEL_ENDPOINT=http://localhost:4318/v1/traces` and
scrape `/metrics` directly with their own Prometheus.

## 7. Anti-patterns

- **Logging the same thing as a span event and a log line.** Pick
  one — span events for in-trace context, logs for everything else.
- **Adding a metric for "this is what I want to debug today."**
  Metrics are budget; reach for a span event or log first.
- **Including `task_id` in a metric label.** Always wrong. Use a
  log line or a span event.
- **Free-form error strings as labels.** `error="connection refused on agent_id=foo"`
  blows up cardinality. Bucket errors into a small enum
  (`reason="transport_error" | "ack_timeout" | ...`).
- **One span per await.** Spans should map to operations a human
  cares about, not to every coroutine.

## 8. Testing observability — planned

**Status: not yet shipped.** The harness below is the target;
nothing in `bp_sdk.testing` exposes these helpers today. Tests that
need to assert on emitted events currently use `caplog` or
hand-rolled context managers.

- **Unit:** `assert_emitted_event(log, "task_dispatched", task_id=...)`
  helper in the test harness.
- **Integration:** the `TestRouter` (`sdk/services.md` §7) collects
  emitted spans/logs/metrics into a typed buffer; tests assert on
  shape, not exact values.
- **CI smoke:** the `dashboards/` JSON is validated against the
  metric registry on every PR — a panel referencing a non-existent
  metric fails CI.
