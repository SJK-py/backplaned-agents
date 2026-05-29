# Router — Storage, HTTP API, Operability

> Part 3 of the router design. Database schema, file-store backend,
> HTTP API surface, observability, configuration, and the
> implementation sequence. See [`protocol.md`](./protocol.md) and
> [`state.md`](./state.md) for the wire protocol and task model.

## 1. Database

Postgres ≥ 14, accessed via `asyncpg`. All migrations managed via
Alembic — no ad-hoc DDL. (An earlier draft proposed an `aiosqlite`
single-node path; that's been dropped — the codebase relies on
Postgres-only features like `jsonb`, GIN indexes, recursive CTEs,
and `SELECT ... FOR UPDATE`.)

### 1.1 Core tables

```
users(user_id PK, level, auth_kind, auth_secret_hash,
      email UNIQUE, created_at, suspended_at, deleted_at,
      serviced_by text[])
  -- `suspended_at` reversible (admin lock).
  -- `deleted_at` terminal soft-delete (row stays; audit / FK refs
  --   survive). Partial idx users_active_idx on
  --   (created_at DESC) WHERE deleted_at IS NULL.
  -- `serviced_by` lists service-principal user_ids authorised to
  --   mint refresh / password-reset tokens for this user (F8).

sessions(session_id PK, user_id FK, opened_at, closed_at,
         metadata JSONB)

agents(agent_id PK, kind, status, capabilities JSONB,
       groups JSONB, agent_info JSONB,
       auth_token_hash, public_key, registered_at, last_seen_at)

acl_rules(rule_id PK, ord UNIQUE, name, description,
          effect, user_level, caller_pattern, callee_pattern,
          created_at, created_by FK)

tasks(task_id PK, parent_task_id FK, root_task_id, user_id FK,
      session_id FK, agent_id FK,
      caller_agent_id FK, active_agent_id FK,
      state, status_code,
      idempotency_key UNIQUE(user_id, idempotency_key),
      priority, deadline, created_at, updated_at,
      input JSONB, output JSONB, error JSONB)
  -- `agent_id` is the destination the task was admitted to.
  -- `caller_agent_id` is who issued it (Progress/Result fan-out
  --   target — set explicitly, NOT inferred via parent, so root
  --   tasks from channel agents get replies). `active_agent_id`
  --   is the agent currently executing it: equals `agent_id` for
  --   new tasks, reassigned to the L1 on delegation; the terminal-
  --   Result auth check compares against `active_agent_id`. Both
  --   NOT NULL, FK agents, indexed (tasks_caller_idx,
  --   tasks_active_agent_idx).

task_events(event_id PK, task_id FK, ts, kind, actor_agent_id,
            from_state, to_state, payload JSONB)

files(file_id PK, sha256, user_id FK, session_id FK,
      task_id FK, byte_size, mime_type, storage_url,
      original_filename, created_at, expires_at,
      UNIQUE(user_id, sha256))

audit_log(event_id PK, ts, actor_kind, actor_id, event,
          target_kind, target_id, payload JSONB,
          prev_hash, self_hash)
  -- Partial idx audit_log_actor_ts_idx (actor_id, ts DESC)
  --   WHERE actor_id IS NOT NULL for admin actor-filter queries.
  -- `payload` is capped at 8 KiB by `_maybe_truncate_audit_payload`
  --   (storage AND hash-chain CPU bound), replaced with a marker
  --   on overflow.

invitations(token_hash PK, level, expires_at, used_at,
            used_by, created_by FK,
            created_at, idempotency_key,
            provisions_service_user)
  -- `used_by` is plain text, NOT a FK: invitations are consumed
  --   by AGENTS (agents.agent_id), not users — a users FK
  --   rejected every legitimate agent onboard. Audit-only.
  -- `idempotency_key` + partial unique index
  --   invitations_created_by_idempotency_key_uniq
  --   (created_by, idempotency_key) WHERE idempotency_key IS NOT
  --   NULL backs the Idempotency-Key request header (per-admin
  --   scope). `created_at` backs list pagination.
  -- `provisions_service_user` (bool, default false): when set, the
  --   onboard that consumes this invitation also creates a co-located
  --   `usr_service_{agent_id}` (level=service) + returns its refresh
  --   token (see security.md §3.2).

auth_refresh_tokens(token_hash PK, user_id FK,
                    issued_at, expires_at, used_at, replaced_by)

password_reset_tokens(token_hash PK, user_id FK ON DELETE CASCADE,
                      issued_at, expires_at, used_at, created_by FK)
  -- Single-use, time-limited "forgot password" token. Distinct
  --   from refresh tokens (different TTL / authz / audit story).

llm_presets(name PK, description, provider, concrete_model,
            api_key_ref, api_key, base_url, min_user_level,
            default_temperature, default_max_tokens,
            default_provider_options JSONB,
            fallback_preset FK ON DELETE SET NULL, max_retries,
            created_at, updated_at, created_by FK)
  -- Named (provider, model, sampling, tier-gate) bundles agents
  --   reference instead of repeating provider config per call.

pending_user_registrations(registration_id PK uuid, channel,
            external_id, display_name, requested_email,
            metadata JSONB, requested_at, attempts,
            last_attempt_at,
            submitted_by_service_user_id FK ON DELETE SET NULL,
            UNIQUE(channel, external_id))
  -- Channel-side registration queue; admin approves into a real
  --   user. `submitted_by_service_user_id` auto-grants servicing.

registration_attempts(id bigserial PK, channel, external_id,
            attempted_at)
  -- Rolling-window durable history for the per-(channel,
  --   external_id) rate-limit bucket. Grows unbounded — operators
  --   should prune `WHERE attempted_at < now() - interval '30d'`.

mcp_servers(server_id PK, description, url, transport, auth_kind,
            auth_value_ref, auth_header_name, groups text[],
            expose_to_llm, tools_cache JSONB, refresh_requested_at,
            created_at, last_connected_at, created_by FK)
  -- Admin-managed MCP bridge configs. One row → N runtime agents
  --   (one per MCP tool). `auth_value_ref` indirects via
  --   env://VAR or secret://path — raw secrets never stored here.
```

### 1.2 Indexes

Selected hot-path indexes (the full set is created by
`0001_initial_schema`):

- `tasks(user_id, state)` — quota / state filters.
- `tasks(parent_task_id)` — child traversal for cancellation.
- `tasks(caller_agent_id)`, `tasks(active_agent_id)` — fan-out
  and delegation-auth lookups.
- `tasks(state) WHERE state IN ('QUEUED','RUNNING','WAITING_CHILDREN')` —
  partial index for `timeout_sweep`.
- `task_events(task_id, ts)` — audit queries.
- `files(user_id, sha256)` UNIQUE — content-address dedup.
- `users(created_at DESC) WHERE deleted_at IS NULL` — active-user
  admin list; `users USING gin(serviced_by)` — reverse servicing.
- `audit_log(actor_id, ts DESC) WHERE actor_id IS NOT NULL` —
  admin actor-filtered audit view.
- GIN on `agents(capabilities)`, `agents(groups)`,
  `mcp_servers(groups)` for ACL pattern evaluation.

### 1.3 Constraints

- `tasks.state` validated by CHECK constraint against the enum.
- `tasks.deadline` enforced as `NULL OR > created_at`.
- `acl_rules.caller_pattern` / `callee_pattern` enforced by the
  named CHECKs `acl_rules_caller_pattern_check` /
  `_callee_pattern_check` carrying the Phase-10 prefix-glob-aware
  regex (added via `ALTER … ADD CONSTRAINT` so the relaxation has
  a stable handle — see `0001_initial_schema._ACL_PATTERN_REGEX`).
- FK delete behaviour is **not** uniform: most are the default
  `NO ACTION` (preserve audit history; soft-delete via flags),
  but `password_reset_tokens.user_id` is `ON DELETE CASCADE` and
  `llm_presets.fallback_preset` /
  `pending_user_registrations.submitted_by_service_user_id` are
  `ON DELETE SET NULL`.

### 1.4 Concurrency

The transition function (`state.md` §1.3) uses `SELECT ... FOR UPDATE`
on the `tasks` row to serialise per-task transitions; row-level
locking gives proper concurrency for the common workload.

## 2. File-store backend

The current implementation writes files to a local `PROXYFILE_DIR`
(`router.py:47`). Multi-worker / multi-host deployments break this. The
rewrite defines a pluggable interface:

```python
class FileStore(Protocol):
    async def put(self, sha256: str, src: AsyncIterable[bytes],
                  meta: FileMeta) -> str: ...
    async def open(self, sha256: str) -> AsyncIterator[bytes]: ...
    async def presigned_url(self, sha256: str,
                            ttl_s: int) -> Optional[str]: ...
    async def delete(self, sha256: str) -> None: ...
```

Implementations: `LocalFileStore` (default), `S3FileStore`,
`GCSFileStore`, `R2FileStore`. Selection via config (§5).

### 2.1 Content addressing

Every uploaded file is hashed (sha256) before storage and stored under
its hash. `files.sha256` is unique — duplicate uploads reuse the
existing object. Integrity checks on download verify the hash matches.

### 2.2 Download delivery

`GET /v1/files/{file_id}` either streams the blob through the router
or **302-redirects to a backend presigned URL** when the store
supports it (S3-compatible) — removing the router from the byte path
entirely. Both paths apply the same download hardening: a forced
`attachment` disposition and a MIME allowlist (an off-allowlist type
is downgraded to `application/octet-stream`), so a malicious upload
can't render inline. The presigned redirect pins the same
disposition + content-type into the signed request (it can't carry
`nosniff`).

### 2.3 Lifecycle

- Files default to TTL = `task.created_at + 7 days` (configurable).
- A garbage-collection loop (analogous to current `gc_proxy_files` in
  `router.py`) deletes expired rows and their backend objects.
- User quota (`file_storage_bytes`, see `state.md` §2.3) enforced at
  upload time.
- `DELETE /v1/sessions/{id}` cascades to file expiry for that session.

## 3. HTTP API surface

WebSocket `/v1/agent` carries all agent-runtime traffic. Everything
else is HTTP. All endpoints are versioned under `/v1/`; breaking
changes ship under `/v2/`.

### 3.1 Public (user-facing)

| Method | Path                                  | Purpose                            |
| ------ | ------------------------------------- | ---------------------------------- |
| POST   | `/v1/auth/login`                      | Issue session JWT                  |
| POST   | `/v1/auth/refresh`                    | Refresh session JWT                |
| POST   | `/v1/sessions`                        | Open a session                     |
| DELETE | `/v1/sessions/{id}`                   | Close a session                    |
| GET    | `/v1/sessions/{id}/tasks`             | List tasks in session              |
| GET    | `/v1/tasks/{id}`                      | Read one task (status + events)    |
| POST   | `/v1/tasks/{id}/cancel`               | Cancel a task                      |
| GET    | `/v1/files/{file_id}`                 | Download (router-proxy) or 302 to presigned |
| POST   | `/v1/files`                           | Upload a blob (multipart or chunked)        |
| POST   | `/v1/files/names`                     | Bind an uploaded blob to a stash name       |
| GET    | `/v1/files/names`                     | List stash names in a session/persist scope |
| GET    | `/v1/files/names/resolve`             | Resolve a stash name → file_id (+ fetch key)|

### 3.2 Agent-facing

| Method | Path                              | Purpose                            |
| ------ | --------------------------------- | ---------------------------------- |
| POST   | `/v1/onboard`                     | Register a new external agent      |
| POST   | `/v1/agent/refresh-token`         | Rotate auth token                  |
| WS     | `/v1/agent`                       | Long-lived agent WebSocket         |

### 3.3 Admin

| Method | Path                                          | Purpose                                  |
| ------ | --------------------------------------------- | ---------------------------------------- |
| POST   | `/v1/admin/invitations`                       | Issue agent invitation                   |
| POST   | `/v1/admin/users`                             | Create user                              |
| GET    | `/v1/admin/users`                             | List users (paginated; `?level=` filter) |
| GET    | `/v1/admin/users/{id}`                        | Get one user                             |
| PATCH  | `/v1/admin/users/{id}`                        | Update `level` and/or `suspended`        |
| GET    | `/v1/admin/audit`                             | Query audit log                          |
| GET    | `/v1/admin/acl/rules`                         | List ACL rules                           |
| PUT    | `/v1/admin/acl/rules`                         | Replace ACL ruleset (validated)          |
| POST   | `/v1/admin/acl/rules`                         | Insert one rule                          |
| PATCH  | `/v1/admin/acl/rules/{rule_id}`               | Patch one rule                           |
| DELETE | `/v1/admin/acl/rules/{rule_id}`               | Remove one rule                          |
| POST   | `/v1/admin/acl/rules/reorder`                 | Renumber via `{rule_id: new_ord}` map    |
| POST   | `/v1/admin/acl/rules/simulate`                | Dry-run a `(caller, callee, level)`      |
| GET    | `/v1/admin/agents`                            | List registered agents                   |
| GET    | `/v1/admin/agents/{id}`                       | Agent detail (full AgentInfo)            |
| POST   | `/v1/admin/agents/{id}/suspend`               | Force-disconnect & disable (reversible)  |
| POST   | `/v1/admin/agents/{id}/unsuspend`             | Restore a suspended agent                |
| POST   | `/v1/admin/agents/{id}/evict`                 | Permanent removal (terminal)             |
| GET    | `/v1/admin/agents/{id}/tasks`                 | Recent tasks for an agent (cross-user)   |
| GET    | `/v1/admin/users/{id}/tasks`                  | Recent tasks for a user (cross-session)  |
| GET    | `/v1/admin/invitations`                       | List invitations (`?status=` filter)     |
| DELETE | `/v1/admin/invitations/{token_hash}`          | Revoke an unused invitation              |
| POST   | `/v1/admin/tasks/test`                        | Send a test task as `admin_console`      |

### 3.4 Health

| Method | Path                  | Purpose                                    |
| ------ | --------------------- | ------------------------------------------ |
| GET    | `/healthz`            | Liveness (process up)                      |
| GET    | `/readyz`             | Readiness (DB reachable, storage writable) |
| GET    | `/metrics`            | Prometheus exposition                      |

## 4. Observability

Three pillars, all on by default. None of these are opt-in.

### 4.1 Tracing

OpenTelemetry. Every WebSocket frame carries `trace_id` + `span_id`
(`protocol.md` §2.1). The router creates a span per dispatch, per ACL
check, per state transition, per DB write. Spans link parent → child
across the task tree via the trace context propagated in `NewTask`.
Exporter is OTLP/HTTP, configured via standard `OTEL_*` env vars.

### 4.2 Logs

Structured JSON to stdout. Every log line carries
`{ts, level, trace_id, span_id, user_id?, session_id?, task_id?,
agent_id?, event, ...}`. No `print()` calls anywhere; the linter
forbids them. Log levels are honoured per-module via standard
`logging` config.

### 4.3 Metrics

Prometheus, exposed at `/metrics`. Representative subset — the
canonical, exhaustive set is `docs/observability.md` §4 (single
source of truth: `bp_router/observability/metrics.py`):

- `router_frames_total{direction, type}` (counter) — deliberately
  NOT labelled by `agent_id` (unbounded cardinality).
- `router_task_state_transitions_total{from, to}` (counter)
- `router_task_duration_seconds{terminal_state}` (histogram)
- `router_acl_decisions_total{decision, effect, rule_name}` (counter)
- `router_quota_exceeded_total{counter, level}` (counter)
- `router_ws_connected_agents_count` (gauge)
- `router_db_query_duration_seconds{query}` (histogram)
- `router_storage_bytes_total{backend, op}` (counter)
- `router_db_pool_connections{state}` (gauge),
  `router_redis_health` (gauge) — pool / Redis saturation.

## 5. Configuration

Single `Settings` object using Pydantic Settings. Validated at startup;
typos and missing required values fail fast. No scattered
`os.environ.get(...)`.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTER_")

    # Database / cache
    db_url: str                                       # postgresql://... DSN
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_statement_timeout_ms: int = 30_000
    redis_url: Optional[str] = None                   # required for multi-worker

    # File storage
    file_store: Literal["local", "s3", "gcs", "r2"] = "local"
    file_store_options: dict[str, Any] = {}
    file_default_ttl_s: int = 604_800

    # Network
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    public_url: str                                   # external base URL

    # Auth / tokens
    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256", "EdDSA"] = "HS256"
    jwt_key_version: int = 1
    session_jwt_ttl_s: int = 900                      # 15 min
    refresh_token_ttl_s: int = 86_400                 # 24 h
    agent_token_ttl_s: int = 86_400                   # 24 h

    # Protocol limits
    heartbeat_interval_ms: int = 20_000
    max_payload_bytes: int = 1_048_576
    per_socket_outbox_max: int = 256
    pending_ack_timeout_s: float = 30.0
    default_task_deadline_s: int = 600
    resume_window_s: int = 30

    # ACL
    acl_max_tier: int = 3                             # catalog probe ceiling
    admin_test_allow_act_as: bool = False             # impersonation in /admin/tasks/test

    # Observability
    otel_endpoint: Optional[str] = None
    otel_service_name: str = "bp_router"
    log_level: str = "INFO"
    deployment_env: Literal["dev", "staging", "prod"] = "dev"
```

The `acl_rules` table is the single source of truth for the firewall
rule list — there is no `acl.yaml` file or auxiliary config model.
Rules are loaded from the table at lifespan start; admin endpoints
under `/v1/admin/acl/rules` (see §3.3) persist mutations and trigger
hot-reload + `CatalogUpdate` fan-out.

## 6. Concurrency model

- Single asyncio event loop per worker.
- DB calls via `asyncpg` — never block the loop.
- CPU-bound work (image resize, hashing) offloaded to a
  `concurrent.futures.ThreadPoolExecutor` via `asyncio.to_thread`.
- One Postgres connection pool per worker; one Redis pool per worker.
- Per-socket send tasks isolate slow consumers from each other.

### 6.1 Multi-worker — planned

**Status: not yet implemented.** Today the router runs as a single
worker; the in-memory socket registry, pending-ack futures, and
correlation maps are process-local. Deployments needing horizontal
scale should run a single instance.

The intended path: an external load balancer terminates TLS and
sticky-routes WebSocket connections by `agent_id` (consistent
hashing). The socket registry moves to Redis so cross-worker
admit can locate a destination; pending-ack futures stay
process-local because the registering worker is also the receiving
worker.

## 7. Implementation sequencing

The recommended build order (each step deliverable in isolation):

1. **Schema + migrations.** Postgres + Alembic. Shipped as a
   single consolidated baseline (`0001_initial_schema`; the
   pre-release incremental migrations were folded in — no
   deployment carries an intermediate schema). Stands up all
   tables in §1.1.
2. **Frame models + transition function.** Pydantic discriminated
   union, single `task_transition()`. Unit-tested in isolation.
3. **WebSocket hub.** `/v1/agent` endpoint, Hello/Welcome handshake,
   in-memory socket registry, heartbeat. Echo-only initially.
4. **Embedded agent dispatch.** Direct-call registry; smoke test with
   a noop embedded agent.
5. **External dispatch.** Real send/recv loops, ack correlation,
   disconnect cleanup.
6. **ACL evaluator.** Firewall-style rule list, deny-by-default. See
   [`acl.md`](../acl.md).
7. **HTTP API.** Onboarding, sessions, tasks, files, admin.
8. **File store + LocalFileStore.** Content-addressed local backend.
   S3/GCS land later behind the same interface.
9. **Observability.** OTel + Prometheus + structured logs from step 1
   onward; this item is a verification milestone, not greenfield work.
10. **First real agent (Gemini).** Drives end-to-end validation and
    pressure-tests the SDK (see [`sdk/core.md`](../sdk/core.md)).

Steps 1–4 are the foundation; nothing else can be tested without them.
Steps 5–9 are independently shippable. Step 10 is the first
deployment milestone.
