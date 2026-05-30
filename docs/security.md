# Security — Threat Model, Tokens, Secrets

> Security model for the reworked Backplaned. Prescribes the threat
> model, trust boundaries, authentication/authorization, token
> lifecycle, secrets handling, and audit trail. Companion to
> [`acl.md`](./acl.md) (which covers permission rules) and
> [`observability.md`](./observability.md) (which covers audit
> logging mechanics).

## 1. Threat model

### 1.1 Assets

| Asset                           | Why it matters                                       |
| ------------------------------- | ---------------------------------------------------- |
| User content (prompts, files)   | Privacy; potential PII; possible regulated data      |
| Provider API keys               | Direct cost impact; abuse risk                       |
| Agent auth tokens               | Lateral movement into the agent network              |
| Admin credentials               | Full control over users, agents, ACL                 |
| Audit log integrity             | Regulatory and forensic requirement                  |
| Quota / billing counters        | Financial integrity                                  |

### 1.2 Adversaries

- **External attacker** (no credentials). Goals: exfiltrate data,
  consume budget, disrupt service, pivot to provider keys.
- **Compromised user account**. Goals: exfiltrate other users'
  data, escalate privileges, exhaust shared budget.
- **Compromised agent process**. Goals: as above, plus impersonate
  legitimate routing, extract API keys.
- **Malicious agent author**. A registered agent intentionally
  abusing its capabilities. Goals: exfiltrate data passing through
  it, escalate via crafted frames.
- **Insider with admin role**. Out of scope for technical controls
  beyond audit logging.

### 1.3 Out of scope

- Kernel- and hypervisor-level attacks.
- Side-channel attacks against in-process embedded agents (sharing
  a process is a stated trust assumption — see §2).
- Coercion of authorised users.

## 2. Trust boundaries

```
   ┌─────────────────────────────── public network ───────────────────────────────┐
   │                                                                              │
   │  ┌────────────┐    HTTPS    ┌───────────┐                                    │
   │  │ User UA    │ ──────────► │  Webapp   │                                    │
   │  └────────────┘             │  (BFF)    │                                    │
   │                             └─────┬─────┘                                    │
   │                                   │  HTTPS / WSS (session JWT)               │
   │                                   ▼                                          │
   │  ┌──────────────────────────────────────────────────────────────────────┐   │
   │  │                              Router                                  │   │
   │  │  trust ⟦ embedded agents ⟧ same-process; isolated only by SDK rules  │   │
   │  └──────────────────────────────────────────────────────────────────────┘   │
   │           ▲                                  ▲                              │
   │           │ WSS (agent JWT)                  │ HTTPS (admin)                │
   │           │                                  │                              │
   │  ┌────────┴─────────┐                ┌───────┴───────┐                      │
   │  │ External agents  │                │   Admin UA    │                      │
   │  └──────────────────┘                └───────────────┘                      │
   │                                                                              │
   └──────────────────────────────────────────────────────────────────────────────┘
```

Boundaries (each is a place where authentication is required):

1. User browser → Webapp BFF.
2. Webapp / Admin → Router HTTP API.
3. External agent → Router WebSocket.
4. Router → Storage backend (S3 / Postgres / Redis).
5. Router → LLM provider APIs.

Embedded agents are inside the router's trust boundary. This is a
deliberate trade for hot-path latency (see overview §P8) and the
reason the SDK forbids known-blocking imports and provider-secret
direct access from embedded handlers.

## 3. Authentication

### 3.1 Users

Every user is classified by exactly one **`level`**: `admin`,
`service`, or `tierN` (with `tier0` most privileged, `tierN` least —
see `docs/router/state.md` §2.4).

- **Password** for human users. The `LoginRequest` model accepts a
  `totp` field but **TOTP enforcement is planned**, not implemented:
  the verifier path is currently a no-op
  (`bp_router/api/auth.py:_no_totp_yet`). Until that lands, password
  is the only factor.
- **OIDC** (Google, Microsoft, GitHub) — planned. Not yet wired.
- **Service principals** (`level=service`) authenticate with a
  long-lived API key (rotatable) carried as
  `Authorization: Bearer <key>`.

Passwords are stored using `argon2id` (replacing the current
PBKDF2 in `helper.py:34-52` — PBKDF2 is acceptable for legacy but
argon2id is the default for greenfield). Hash parameters are
config-tunable; defaults follow OWASP 2024 recommendations.

After successful login, the router issues a short-lived **session
JWT** (default 15 min) and a refresh token (default 24 h, single-
use, rotation on refresh).

### 3.2 Agents

External agents authenticate to the router with an **agent JWT** —
short-lived (default 24 h), rotated automatically by the SDK. JWTs
are signed with the router-side `jwt_secret` (HS256). The
`jwt_algorithm` Settings field is restricted to `Literal["HS256"]`
— EdDSA was listed in an earlier draft but never wired (only the
symmetric secret is read on issue / verify); the validator now
rejects the alternative at startup. Asymmetric key support
remains future work.

JWT claims:

```jsonc
{
  "iss": "router",
  "sub": "<agent_id>",
  "iat": 1234567890,
  "exp": 1234654290,
  "kind": "agent",
  "ver": 1,                      // protocol version supported
  "jti": "<uuid>"                // for revocation
}
```

The agent JWT is presented in the `Hello` frame's `auth_token`
field. The router validates signature, expiry, claims, and
revocation list. The handshake calls `is_jti_revoked(redis, jti)`
AFTER `verify_agent_token` — same shape as HTTP authn paths
(`security/jwt._principal_from_request`, `api/onboard.refresh_agent_token`)
— so a rotated / explicitly-revoked agent token is refused at
reconnect, not just at natural exp. A revoked `jti` causes
immediate socket close with `ErrorCode.AUTH_FAILED` /
`reason="auth_failed: revoked"`.

The WS handshake also enforces:
- **Per-IP rate limit** on `/v1/agent` connects (`ws_handshake_rate_limit_per_ip_*`,
  default 5/s burst 20). Bucket consumed BEFORE the JWT verify
  + Redis lookup so a flooding IP doesn't burn auth machinery.
  Saturated bucket closes the WS with code **4029**.
- **Hello frame size cap** at `max_payload_bytes` (default 1 MiB).
  Oversized payload closes with code **1009** before
  `parse_frame` runs — defends against parse-CPU exhaustion
  by unauthenticated clients.

**Co-located service principal (onboarding-provisioned).** An
invitation may carry `provisions_service_user`. When such an invitation
is consumed at `/v1/onboard`, the router — in the same transaction as
the agent insert — also creates a `level=service` user
`usr_service_{agent_id}` and mints it a refresh token, returned once on
the `OnboardResponse` (`service_user_id`, `service_refresh_token`,
`service_token_expires_at`). This lets a channel / gateway agent obtain
its own service identity at first boot without a separate admin
`create_user` + env-seeded refresh token; the admin's trust decision is
the (admin-only, single-use) invitation. The `usr_service_` prefix is
reserved — rejected for caller-supplied user_ids. The runtime privilege
boundary is unchanged: it is an ordinary `level=service` user, so the
`serviced_by` mint endpoint and its guards (§5.5) still gate everything
it can do. Re-onboarding re-mints the refresh token; it refuses a
soft-deleted or non-service row already holding the reserved name.

### 3.3 Optional agent identity (asymmetric)

For higher-assurance deployments, agents may register with an
ed25519 public key at onboarding (POST `/v1/onboard` carries the
public key, which the router stores on the agent row).

> **Status — not yet enforced.** The public key is *stored* but the
> router does **not** currently issue a `Hello` challenge or verify a
> signature; the WS handshake authenticates by JWT alone. The
> challenge/response that would raise the bar from "compromised JWT"
> to "compromised host key" is scaffolding for a future release —
> don't rely on it as a control today.

Loss of the key requires admin re-issuance; the router does not
support self-service key rotation for now.

## 4. Authorization

Three layers, all enforced server-side, in this order:

1. **Authentication.** Reject unauthenticated requests at the edge.
2. **Role / tier check.** Admin endpoints require `admin`. User-
   facing endpoints require `user` or `service`.
3. **ACL check.** For agent-to-agent calls, evaluate the
   capability/tag rules ([`acl.md`](./acl.md)).

User-data access (own files, own tasks, own sessions) is enforced
by foreign-key scoping in every query — the data layer never
returns rows from another user's `user_id` regardless of the
caller. This is a `WHERE user_id = $current_user_id` invariant
checked by a single helper used by every read path.

## 5. Token lifecycle

### 5.1 Issuance

- **Session JWT.** Issued on login or refresh. Carries `user_id`,
  `level`, `iat`, `exp`. Signed by router secret.
- **Refresh token.** Issued alongside the session JWT. Stored
  hashed in `auth_refresh_tokens(token_hash, user_id, expires_at,
  used_at, replaced_by)`. Single-use; on refresh, the old row is
  marked `used_at` and a new pair is issued.
- **Agent JWT.** Issued at onboarding and at every refresh
  (`POST /v1/agent/refresh-token`). Refresh requires the agent to
  present its current valid JWT plus its registered identity
  (asymmetric mode) or its long-term shared secret.
- **Co-located service refresh token.** When the consuming invitation
  is flagged `provisions_service_user`, onboarding also mints a refresh
  token for `usr_service_{agent_id}` (§3.2), returned once on the
  `OnboardResponse`. Thereafter it rotates through `/v1/auth/refresh`
  like any user refresh token.

### 5.2 Revocation

- A `jti` revocation list is held in Redis with TTL = JWT
  remaining lifetime. Cheap to check on every frame admit.
- Admin can revoke an agent (`POST /v1/admin/agents/{id}/suspend`)
  or a user session via the same mechanism.
- Admin can **reset** an agent to `pending`
  (`POST /v1/admin/agents/{id}/reset`) — an operational "kick": force-close
  its socket, fail its in-flight tasks, and require re-onboard before it
  serves again (the reversible sibling of `suspend`; the agent comes back on
  its own via re-onboard, no un-reset needed). Recovery itself no longer
  needs this — `POST /v1/onboard` re-onboards an already-`active` row given a
  valid invitation. Re-onboard still requires an admin invitation, so the
  `agent_id` is never freed for silent reuse. (`reprovision` = reset + mint a
  fresh invitation in one click, for an agent with no pending invitation.)
- **Evict** (`POST /v1/admin/agents/{id}/evict`) retires an agent and
  **frees its `agent_id`**: the `removed` row's PK is renamed to a tombstone
  (`deleted_<id>_<epoch>`, with its co-located service principal renamed the
  same way), and `tasks`/`audit` history is preserved under the tombstone via
  FK `ON UPDATE CASCADE`. The freed id is re-onboardable, but only with a
  fresh admin invitation — so reuse is deliberate and audited
  (`agent.id_released`), never silent. The evicted instance itself is
  terminal and never serves again.
- Mass revocation (e.g. signing key rotation) is supported by
  bumping a global `key_version` and rejecting JWTs signed against
  earlier versions.

> **Single-worker dev fallback.** When `ROUTER_REDIS_URL` is unset,
> `revoke_jti` and `is_jti_revoked` silently no-op — single-worker
> deployments accept that revocation is best-effort (the underlying
> JWT's `exp` claim still bounds the replay window to the JWT's
> remaining lifetime, typically ≤15 minutes).
> 
> The `_redis_required_in_non_dev` Settings validator REJECTS this
> shape at startup when `deployment_env` is `staging` or `prod` —
> across multiple workers a logout on one worker would otherwise
> silently fail to invalidate the token on the others. A prod
> deployment with `ROUTER_REDIS_URL` **unset** fails fast at boot
> with a message pointing at it.
>
> **Boot-tolerant for the *unreachable* case.** The validator only
> checks the URL is *set*, not *reachable*. If Redis is configured
> but down at boot, the router no longer crashloops — it starts
> **degraded**: `state.redis = None`, `redis_health` gauge → 0,
> an error log `redis_unreachable_at_boot`, and the same
> per-process fallback the dev path uses (the running router
> already tolerates Redis flakes everywhere — failing boot was the
> one place that didn't, turning a transient blip into a total
> outage). Consequence: **while degraded, JWT revocation fails
> open** — `is_jti_revoked` returns `False` when `redis is None`,
> so a revoked `jti` can reconnect until its `exp`. Operators must
> alert on `router_redis_health == 0` and treat sustained degraded
> operation as a security-relevant incident, not just a
> throughput one. See `docs/design/quota-enforcement.md` §12.

### 5.3 Refresh-token theft mitigation

Single-use refresh tokens with rotation detect token replay: if a
refresh token's `used_at` is set when presented, the entire token
family is invalidated (forces re-login) and an audit event is
emitted. This is the standard OAuth2 refresh-token rotation
pattern.

### 5.4 Soft-delete

`DELETE /v1/admin/users/{id}` does NOT remove the user row —
audit history and FK references would dangle. Instead it sets
`users.deleted_at = now()` and runs a four-step cascade:
delete every refresh token, delete every pending password-reset
token, sweep the user out of every other user's `serviced_by`
array, drop the cached level from `LlmService._user_level_cache`.

Every authenticated boundary refuses `deleted_at IS NOT NULL`:
- `login` / `refresh` / `reset_password` (HTTP auth endpoints) —
  routed through `queries.user_is_active(user)`.
- `change_password` — same helper.
- `_principal_from_request` (session-JWT dependency for every
  authenticated route) — cache-hot fast path via
  `LlmService.peek_user_level_cached`; cache miss falls through
  to a `users` lookup that refuses `deleted_at`. Cache invalidation
  on `delete_user` means the soft-delete takes effect on the next
  request, not after 10-min TTL.
- `LlmService.resolve_user_level` — returns `None` for
  deleted (and suspended) users so tier-gates deny.
- `admin.test_task` (admin act-as path).

Net: a soft-deleted user cannot authenticate to any endpoint,
cannot pass LLM tier-gates, and cannot be impersonated via
admin test-task — immediately on the next request.

### 5.5 Per-target rate limits on credential mint

Service principals can mint refresh tokens
(`POST /v1/admin/users/{id}/refresh-tokens`) and password-reset
tokens (`POST /v1/admin/users/{id}/password-reset-token`) for
users in their `serviced_by` list. Both endpoints share a
`_enforce_per_target_mint_rate_limit` helper that consumes a
per-target token bucket BEFORE the mint:

- `service_mint_refresh_token_rate_limit_per_target_*` — default
  ≈12/h per target, burst 5.
- `password_reset_mint_rate_limit_per_target_*` — default
  ≈3/h per target, burst 3.

A compromised service principal otherwise could mint unlimited
24-h refresh tokens against every user it services, each
independently revocable only by hash. Saturation returns 429
with Retry-After and writes an
`auth.<endpoint>_rate_limited` audit row.

## 6. Secrets management

### 6.1 Categories

| Secret                     | Where it lives                                | Who reads it                  |
| -------------------------- | --------------------------------------------- | ----------------------------- |
| JWT signing key            | Env / KMS / HSM                               | Router only                   |
| Provider API keys          | Secrets backend (Vault / AWS SM / GCP SM)     | LLM service (router) only     |
| Database password          | Env / secrets backend                         | Router only                   |
| Redis password             | Same                                          | Router only                   |
| Storage credentials        | Same                                          | Router only                   |
| Per-user provider keys     | DB (encrypted at rest with envelope keys)     | LLM service per request       |
| Agent shared secret / pubkey | DB                                          | Router only                   |
| User password hashes       | DB                                            | Router only (verify)          |

### 6.2 Provider API keys

Critical: provider API keys do not live in agent processes. The LLM
bridge is an SDK service backed by the router; the router holds keys
and enforces quotas. Embedded agents that need provider access call
`ctx.llm.generate(...)`, which fans out to the router-side service.

**Per-user BYO-key — planned.** Once needed, user-supplied provider
keys will be stored encrypted at rest using envelope encryption
(KMS-issued data key per user), decrypted in memory at use time,
and never logged. The current code has no BYO-key path.

### 6.3 Loading secrets

Configuration prefers references over inline values:

```toml
[router]
jwt_secret = { secret_ref = "vault://kv/router/jwt_secret" }
db_url     = { secret_ref = "env://DATABASE_URL" }
```

The Pydantic Settings layer resolves `secret_ref` at startup. A
deployment that hasn't configured a secrets backend can use plain
env vars; production deployments should not.

### 6.4 Rotation

- JWT signing key: rotate via dual-version overlap. New JWTs are
  signed with `key_version=N+1` while the router still verifies
  `key_version=N` for the JWT lifetime.
- Provider API keys: rotated by updating the secrets backend; the
  router refetches on the next request (or on a scheduled
  interval).
- DB / Redis / storage credentials: standard infra rotation;
  router supports config reload via SIGHUP without dropping
  connections.

## 7. Network security

- TLS 1.2+ on every external interface; modern cipher suites only.
  Self-signed certificates rejected in production via a startup
  check.
- WebSocket connections require WSS in production (HTTP plain
  rejected at the edge).
- Network policies (deployment-local): router accepts inbound only
  from the load balancer subnet; outbound to provider endpoints
  is allowlisted; Redis and Postgres are private-subnet only.
- Storage backends (S3 / GCS) accessed via VPC endpoints where
  available; presigned URLs scoped to single objects with short
  TTL.

## 8. Data isolation

### 8.1 Per-user

- `WHERE user_id = ?` invariant on every read of `tasks`,
  `sessions`, `files`, `audit_log` (when scoped). Enforced by a
  single query helper; CI greps for raw queries that bypass it.
- Postgres deployments may additionally enable Row-Level Security
  (RLS) policies as defence-in-depth.

### 8.2 Per-session

- `session_id` foreign keys enable session-scoped memory and file
  visibility. Cross-session reads are explicit (the orchestrator
  may copy state forward at session-open time).
- The router-managed **named file store** keys directory rows by the
  `(user_id, scope, filename)` tuple — there is no per-file capability
  token, so that tuple is the SOLE authority. Every file op (store /
  fetch / manage AND name-`file_ref` resolution in an LLM request)
  DERIVES `(user_id, session_id)` from the task row + active-executor
  check, never an agent-asserted value, so cross-user reference is
  impossible by construction. See
  `docs/design/router-managed-file-store.md` §9.

### 8.3 Inter-agent — `user_id` is agent-asserted

The router does **not** bind agents to users. Frames flowing through the
router carry `user_id` and `session_id` as opaque routing tokens; the
router only validates that the user *exists and is not suspended*
(needed for ACL `level` lookup), never that the calling agent is
"authorised" to act on behalf of that user.

This is an explicit design choice. Session ownership is the agent's
job, not the router's:

- **Agents are trust boundaries.** An external agent authenticates with
  its own JWT (signed `agent_id`); inside that boundary it is responsible
  for binding work to the user identity it received from its upstream
  caller (typically a webapp BFF that established the user's session).
- **The router is a routing fabric.** It moves typed frames between
  agents, applies the firewall ACL with the user's `level`, and
  persists task records keyed by `(user_id, session_id)`. It does not
  inspect *whether* a given agent has the right to claim a given
  `user_id` — that's enforced upstream of the agent.
- **Compromised agents.** An external agent that authenticates
  successfully can therefore submit `NewTask` frames carrying any
  `user_id` and the router will admit them subject to ACL. The
  threat-model assumption is that agent JWTs are protected and agents
  are trusted within their own scope (see §1.2 — "Compromised agent
  process" is a listed adversary, but the mitigation is in the agent
  trust boundary, not in the router's frame validation).

Practical consequences for deployments:

1. The webapp / BFF that hands work to agents is the integrity boundary
   for `user_id` propagation. Sign or otherwise authenticate the user
   identity at that hop.
2. Agent processes themselves should not accept `user_id` from
   untrusted callers without their own check.
3. The router *does* enforce coarse user-state checks at admit time
   (e.g. unknown `user_id` → `AdmitError("user_unknown")`), so basic
   lifecycle changes propagate. Anything finer-grained is the agent's
   responsibility.

This is the boundary; do not assume more.

### 8.3.1 Per-frame (user_id, session_id) consistency

Distinct from the agent → user binding above, the router *does*
validate at admit time that the `(user_id, session_id)` pair on a
`NewTask` frame points at a real, open session row owned by that user:

- If no session row matches `(session_id, user_id)`, admit rejects
  with `AdmitError("session_unknown")`.
- If the session exists but its `closed_at` is non-null, admit rejects
  with `AdmitError("session_closed")`.

This is a data-integrity check, not an authorization check — it
catches cross-user smuggling (agent claims user A but routes through
user B's session) and stale agents that didn't notice a session
close. It does not contradict the M2 trust model: agents still claim
any `user_id` and the router takes it at face value, but each
individual frame must reference a real and open `(user, session)`
row pair.

Sessions are created by end-user / BFF flows via `POST /v1/sessions`
(see `docs/router/state.md` §2.2). Closed sessions are durable
(soft-close via `closed_at` timestamp) so audit history stays
reachable.

## 9. Audit log

`audit_log` is append-only, hash-chained. Each entry references the
hash of the previous entry (Merkle-style); the head is periodically
checkpointed externally for tamper-evidence. The append takes a
per-transaction advisory lock and picks the predecessor by a
monotonic `seq bigserial` (NOT wall-clock `ts` or the random
`event_id`): both are non-insertion-ordered, so selecting on them
could fork the chain under an NTP step or same-microsecond burst.
`seq` is assigned at INSERT under that same lock, so the chain stays
strictly linear.

Recorded events:

```
user.created                user.updated              user.level_changed
user.suspended              user.unsuspended          user.deleted

session.opened              session.closed

agent.onboarded             agent.onboard_rejected
agent.suspended             agent.unsuspended         agent.evicted
agent.reset                 agent.info_updated        agent.service_principal_provisioned

acl.rules_replaced          acl.rule_added            acl.rule_updated
acl.rule_removed            acl.rules_reordered

auth.login_succeeded        auth.login_failed
auth.refresh_replayed       auth.invitation_rejected
auth.logout                 auth.password_changed     auth.password_change_failed
auth.password_change_revoke_jti

auth.password_reset_token_minted
auth.password_reset_token_consumed
auth.password_reset_mint_denied
auth.password_reset_mint_rate_limited

auth.refresh_token_service_minted
auth.refresh_token_mint_denied
auth.refresh_token_service_mint_rate_limited

invitation.issued           invitation.revoked

task.test_dispatched        (admin-only test-task surface)

quota.exceeded              (planned — quotas not yet enforced)
secret.accessed             (planned)
secret.rotated              (planned)
```

Events tagged `(planned)` are reserved names — the schema accepts
them so external tooling can wire dashboards now, but the router
does not currently emit them. Everything above the planned block
is emitted by current code.

**Payload size cap.** Each row's `payload jsonb` is the SHA-256
input on every subsequent append, so an oversized blob inflates
both the row and the chain hash cost. `_maybe_truncate_audit_payload`
caps the payload at 8 KiB; oversized values are replaced with
`{"__bp_audit_truncated__": True, "original_size_bytes": N, "max_bytes": 8192}`
BEFORE hashing so chain integrity stays consistent for readers.

**Actor-filtered queries.** The consolidated `0001_initial_schema`
creates a partial index `audit_log_actor_ts_idx` on
`(actor_id, ts DESC) WHERE actor_id IS NOT NULL` — supports the
admin UI's `/admin/audit?actor_id=...`
and user / agent detail pages without table-scanning as the log
grows. A `UNIQUE` index `audit_log_seq_idx` on `(seq)` enforces the
no-duplicate-position invariant and serves the append's
`ORDER BY seq DESC LIMIT 1` head pick as a backward index scan.

Audit reads require `admin` role. The audit endpoint supports
filtering and time-range queries but not deletion. Operational
cleanup (compaction, archival) is performed via a separate
backend tool, not the API.

## 10. Specific risks and mitigations

| Risk                                           | Mitigation                                                 |
| ---------------------------------------------- | ---------------------------------------------------------- |
| Compromised agent JWT replays old frames       | Frame timestamps within ±60s window; replay rejected.      |
| Compromised user logs in from a new IP         | Session JWT rebinding to IP optional; alert on change.     |
| Slow / malicious agent monopolises a socket    | Per-socket outbox bound; per-frame ack timeout; reaper.    |
| Agent fakes `user_id` in a `NewTask`           | **Not a router control — by design (§8.3).** `user_id` is agent-asserted/opaque; authority is established *upstream* (`/login`, or a `serviced_by`-bounded service mint → `require_authenticated` → `open_session`). Admit only re-validates that `(user_id, session_id)` is a real, owned, open session row (§8.3.1) — a **data-integrity check, not authorization**. Agents are trusted within their own scope; cross-tenant safety rests on the upstream credential boundary, not router frame validation. |
| Embedded agent does sync I/O                   | SDK lints handler at registration; ASYNC_ONLY flag.        |
| Provider API key leaks via prompt injection    | Keys never in agent processes; injection cannot exfiltrate.|
| Refresh token theft                            | Single-use refresh + family invalidation on replay.        |
| Audit log tampering                            | Hash-chained, externally checkpointed.                     |
| Audit log payload bloat                        | 8 KiB cap with truncation marker (preserves chain hash).   |
| Quota counter race in multi-worker             | Postgres advisory locks or atomic UPDATE ... RETURNING.    |
| Onboarding token reuse                         | Single-use; `used_at` on consumption; rejected after.      |
| Cross-tenant file access via path traversal    | Content-addressed (sha256) storage; paths are computed.    |
| Cross-user named-file read via asserted identity| File ops derive `(user_id, session_id)` from the task row + active-executor check; no per-file key, so the `(user_id, scope, filename)` tuple is the sole authority. |
| Large frame DOS                                | `max_payload_bytes` enforced on EVERY frame (incl. Hello). |
| Long-running CPU loop ignores cancel           | Hard deadline timeout; SDK `raise_if_cancelled` in helpers.|
| WS handshake flood from a single IP            | Per-IP token bucket (5/s burst 20); 4029 close on saturation. |
| Soft-deleted user replays a still-valid JWT    | `_principal_from_request` consults `deleted_at` per-request (cached). |
| Stale tier permission for soft-deleted user    | `delete_user` invalidates `LlmService._user_level_cache` synchronously. |
| Compromised service principal mass-mints       | Per-target rate-limit on refresh + password-reset endpoints. |
| Revoked agent JWT reconnects via WS            | Handshake calls `is_jti_revoked` after `verify_agent_token`. **Fails open if Redis is down/unset** (degraded boot or dev) — `exp` then bounds replay. See §5.2. |
| Validator-message echo leaks input fragments   | `safe_validator_message` caps Ack.reason at 200 chars.     |
| `/metrics` exposition leaks agent ID list      | `ROUTER_METRICS_TOKEN` bearer required (mandatory in non-dev). |
| MCP SSE endpoint URL hijack                    | Origin (scheme+host+port) must match SSE base; cross-origin refused. |
| MCP transient upstream errors fail the task    | Bounded retry on httpx + MCP -32603; permanent errors surface immediately. |
| One user's runaway sub-task fan-out starves *that user's* other tasks | **Partial.** Per-`(user_id, level)` admit quota caps throughput, but a task tree's siblings share one bucket — see §12. |
| Chatty / adversarial agent saturates the recv loop | **Partial.** Per-socket outbox bound, per-frame ack timeout, reaper, parent-agent cache. No per-agent inbound-frame cap — see §12. |

## 11. Operational hygiene

- **Backups.** Postgres + storage backend daily. Audit log
  separately, retained ≥1 year.
- **Pen tests.** Annually, with focus on auth flows, ACL bypass,
  and frame injection.
- **Dependency scanning.** SBOM generated per release; CVEs fail
  CI at HIGH+.
- **Secret leak scanning.** Pre-commit + CI, against the full
  repo and recent commits.
- **Incident response.** A documented runbook for: token
  compromise, agent compromise, signing-key rotation, mass
  revocation.

## 12. What this design does **not** protect against

- Operator-level compromise of the router host (host gets you
  everything: signing keys, provider keys, all data).
- Malicious code shipped inside an embedded agent module — the
  embedded trust boundary is the router process.
- Coercion / social engineering of admins.
- Provider-side breaches (provider holding cleartext prompts).
- **Intra-user and per-agent fairness.** Rate-limiting is scoped
  per-`(user_id, level)` (the admit-quota token bucket), *not*
  per-agent or per-task. Two consequences operators must size for
  before opening the router to real / multi-tenant agent
  development:
  - **Sibling starvation within a user.** A task tree's subtasks
    all draw on the *same* user bucket. A runaway fan-out (an
    agent loop spawning children) consumes that user's admit
    budget and starves the *same user's* unrelated tasks.
    Cross-*user* isolation is unaffected — a noisy tenant cannot
    starve a different tenant — but one tenant's own workload is
    not internally fair.
  - **No per-agent inbound-frame cap.** A chatty or adversarial
    agent can pump frames at `parse_frame` speed and pressure the
    dispatcher recv loop, the DB pool (Progress fan-out), and the
    per-agent fan-out to upstream LLM providers. The backstops are
    indirect: the per-socket outbox bound, the per-frame ack
    timeout, the deadline reaper, and the parent-agent cache
    (which removed the main DB amplifier). There is no throughput
    cap on the recv path itself, and no cap on concurrent
    in-flight LLM requests per agent. The design and a
    `TokenBucket`-based fix are
    tracked in `docs/design/quota-enforcement.md` §11 (H5),
    durable until a deployment commits to enabling it.

  Operational mitigation: isolate distinct trust domains at the
  *deployment* boundary (a router per domain rather than relying
  on intra-router fairness), provision `db_pool_max_size` for the
  worst-case concurrent fleet rather than the steady state, and
  monitor DB-pool and dispatcher saturation so an unfair workload
  is visible before it becomes an outage.

These are accepted risks; mitigation is operational, not
architectural.
