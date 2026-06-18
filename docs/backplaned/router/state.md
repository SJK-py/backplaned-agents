# Router — Task State, Users, ACL

> Part 2 of the router design. Covers the task state machine, the
> multi-user model (users, sessions, quotas, RBAC), and the
> firewall-style ACL that replaces today's group allowlist. The full
> ACL grammar lives in [`../acl.md`](../acl.md); this document only
> covers the multi-user pieces. See [`protocol.md`](./protocol.md)
> for wire framing and [`storage.md`](./storage.md) for persistence
> and HTTP API.

## 1. Task state machine

Today's router tracks task state implicitly across columns and timeouts.
The rewrite makes it explicit: a small enum, enforced transitions, one
function that performs every transition.

### 1.1 States

```
                ┌──────────┐
                │  QUEUED  │  task row created, not yet sent to agent
                └────┬─────┘
                     │ dispatch
                     ▼
                ┌──────────┐
                │ RUNNING  │  agent acked the NewTask frame
                └────┬─────┘
                     │ spawn / delegate
                     ▼
            ┌────────────────────┐
            │ WAITING_CHILDREN   │  awaiting subtask Result(s)
            └────┬───────────────┘
                 │ all children resolved
                 ▼
                ┌──────────┐
                │ RUNNING  │  (re-entry; one task may flip
                └────┬─────┘    several times)
                     │
        ┌────────────┴────────────┬─────────────┬───────────────┐
        ▼                         ▼             ▼               ▼
   ┌──────────┐             ┌──────────┐  ┌───────────┐  ┌──────────────┐
   │SUCCEEDED │             │ FAILED   │  │ CANCELLED │  │ TIMED_OUT    │
   └──────────┘             └──────────┘  └───────────┘  └──────────────┘
        (terminal — no transitions out of any of these)
```

`SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` are terminal. Once a
row enters a terminal state, only `updated_at` may change.

**`status_code` semantics.** Agent-reported terminals
(SUCCEEDED/FAILED via a `Result` frame) persist the agent-supplied
`status_code`. Router-synthesised terminals do **not** always
persist one: `cancel_task` transitions to CANCELLED without a
`status_code` (the `task_transition` write `COALESCE`s it, leaving
the column NULL) even though the synthetic `Result` fanned to the
caller carries **499**; the deadline sweep's FAILED/TIMED_OUT path
persists **504**. Consequence for idempotent replay: when the
stored `status_code` is NULL, the reconstructed terminal `Result`
uses the faithful per-status default the original fan-out used —
CANCELLED → 499, TIMED_OUT → 504, FAILED → 500, SUCCEEDED → 200 —
never 0 (see `protocol.md` §4.3).

### 1.2 Allowed transitions

Encoded as a static table on the router, validated on every transition:

| From               | To                                              |
| ------------------ | ----------------------------------------------- |
| `QUEUED`           | `RUNNING`, `FAILED`, `CANCELLED`, `TIMED_OUT`   |
| `RUNNING`          | `WAITING_CHILDREN`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` |
| `WAITING_CHILDREN` | `RUNNING`, `FAILED`, `CANCELLED`, `TIMED_OUT`   |
| terminal           | _(none)_                                        |

Any other transition is a programming error and raises a typed exception
that fails the task with `internal_error`.

> **Status — `WAITING_CHILDREN` is reserved, not yet driven.** The
> state and its transitions above are defined in `bp_router/state.py`,
> but **no code path currently transitions a task into or out of
> `WAITING_CHILDREN`** (no `task_transition(…, WAITING_CHILDREN)`
> writer exists; `_state_from_status` has no mapping to it). A parent
> that `spawn`s with `wait=True` stays `RUNNING` while its handler
> blocks on the child `Result`; `wait=False` is detached (no parent
> join). The state, its transitions, and the
> `state IN (… 'WAITING_CHILDREN')` "in-flight" query filters are
> scaffolding reserved for a future parent-join implementation —
> treat the `WAITING_CHILDREN` path in the diagram above as
> aspirational, not current behaviour.

### 1.3 Transition function

A single coroutine `task_transition(task_id, new_state, *, reason, conn)`
is the only code path that mutates `tasks.state`. It:

1. Begins a transaction.
2. Reads the current state with `SELECT ... FOR UPDATE` (Postgres) or
   `BEGIN IMMEDIATE` (aiosqlite).
3. Validates the transition against the allowed table.
4. Updates `state`, `updated_at`, and writes a row to `task_events`
   (audit log, see [`storage.md`](./storage.md)).
5. Commits.
6. Emits an OpenTelemetry span event and a Prometheus counter increment.

No other code may write to `tasks.state` directly. Linted at CI via a
grep rule.

### 1.4 Cancellation propagation

`Cancel` on task `T` causes:

1. `T` transitions to `CANCELLED` (if not already terminal).
2. All descendants of `T` (resolved via `parent_task_id` traversal) are
   transitioned to `CANCELLED` and a `Cancel` frame is forwarded to
   their assigned agents.
3. The agent SDK's handler for the cancelled task receives a
   `CancellationError` from its `await` points (see `sdk.md`).

Cancellation is best-effort: a tight CPU loop in an agent will not be
preempted. Agents that run long-running work must check
`ctx.cancel_token` periodically; the SDK enforces this in
streaming/iterating helpers.

**Failure / timeout cascade.** A task entering `FAILED` or
`TIMED_OUT` (agent-reported failure, or the deadline sweep) ALSO
cascade-cancels its descendant subtree — the same `parent_task_id`
traversal as `Cancel`: each descendant → `CANCELLED` with a
synthetic `Result` to its caller and a `Cancel` frame to its
executor. Before this, a failed / timed-out parent leaked its
children — nothing consumed their Results yet they ran on (burning
compute / provider tokens) until their own deadlines, or forever if
spawned without one. Upward propagation to the parent is unchanged.
Each descendant is transitioned in its own transaction so one
already-terminal / poison child can't abandon its siblings.

### 1.5 Timeouts

Two layers:

- **Per-frame ack timeout** (default 30s, see `protocol.md` §4.1) —
  in-memory Future, fires `failed/ack_timeout`.
- **Per-task deadline** — optional `deadline` field on `NewTask`. The
  `timeout_sweep` background loop scans for tasks whose deadline has
  passed and transitions them to `TIMED_OUT`, emitting a `Cancel`
  frame to the assigned agent.

Default per-task deadline is provided by deployment config (e.g. 5
minutes for normal priority, 30 minutes for low priority).

## 2. Multi-user model

The current stack treats `user_id` as a string buried in the task
payload. The rewrite promotes it to a first-class top-level field on
every frame and every database row.

### 2.1 Entities

**`User`** — a registered human (or service principal). Identified by a
stable `user_id`. Owns sessions, tasks, files, quotas. Carries a
`level` (see §2.4) and an `auth` record (password hash for human
users, API key for service principals). Three lifecycle flags:

- `suspended_at: timestamptz | NULL` — reversible admin lock. Login /
  refresh / change_password / reset_password refuse via the shared
  `queries.user_is_active(user)` predicate.
- `deleted_at: timestamptz | NULL` — terminal soft-delete. The row
  stays so audit history and FK references survive; the four-step
  `soft_delete_user` cascade clears refresh tokens, pending
  password-reset tokens, every other user's `serviced_by` entry for
  this user, and the cached level in `LlmService._user_level_cache`.
  The consolidated `0001_initial_schema` declares the column and a
  partial index `users_active_idx ON (created_at DESC) WHERE
  deleted_at IS NULL` to keep the active-user list fast.
- `serviced_by: text[]` (F8) — list of service-principal `user_id`s
  authorised to mint refresh / password-reset tokens for this user.

**`Session`** — a coherent unit of conversational/task continuity for a
single user. One user may have many concurrent sessions (different
chats, different projects). All tasks spawned within a session share a
`session_id`; agents may use this to scope memory.

**`Task`** — as before, but now `tasks` carries `(user_id, session_id)`
as indexed columns. Cross-user task access is blocked at the query
layer; cross-session is permitted but never default.

**`File`** — file-store blob records carry `(user_id, session_id,
task_id)`, with a `file_names` directory addressing them by name.
Default visibility is "owner only," with explicit sharing primitives
(see [`storage.md`](./storage.md)).

### 2.2 Session lifecycle

Sessions are explicitly opened and closed via the router HTTP API
(`POST /v1/sessions`, `DELETE /v1/sessions/{id}`, and `POST
/v1/sessions/{id}/reopen` to re-admit a closed one). A session is a
container — closing one terminates any in-flight tasks tagged to it
(transition to `CANCELLED` with reason `session_closed`) and stamps
`closed_at`. The row is preserved after close so audit history stays
reachable. `DELETE …?purge=true` also **hard-deletes** the session and its
router-side data (tasks, task events, the file-name directory; `files` rows
are detached for the reclaim sweep) — the webapp's "remove session".

The Tier-0 orchestrator agent typically opens a session per
user-conversation; specialised flows (e.g. a webhook-driven cron
agent) may open ephemeral sessions per fired event.

**Pair-consistency at admit.** Every `NewTask` admitted by the
router runs an extra check: the `(user_id, session_id)` on the frame
must point at a real session row owned by that user, and the session
must still be open. The two failure modes surface as
`AdmitError("session_unknown")` (no row, or wrong owner) and
`AdmitError("session_closed")` (`closed_at` set). Agents should treat
`session_closed` as a signal to drop their cached state and let the
BFF open a fresh session.

**No agent-facing session-open path today.** Sessions are created
only by clients holding a session JWT (end-user / BFF / admin). An
agent that needs a session must have its calling BFF pre-open one,
or — for webhook / cron flows — have the scheduling infrastructure
play the BFF role. A future `POST /v1/agent/sessions` with agent-JWT
auth could close this gap; deferred until a real use case shows up.

**Session GC.** A background loop (`tasks.session_gc_loop`, hourly) runs two
sweeps, both conservative — never touching an open session or one with live
tasks:
- **Closed user sessions** past `ROUTER_CLOSED_SESSION_RETENTION_DAYS`
  (default 90) are hard-deleted via `purge_session` (same router-side reach as
  `?purge=true`), audited `session.purged`. Set the retention to 0 to disable.
  The suite's conversation history (a separate store the router can't reach) is
  reaped by a suite-side reconcile loop that keys off these purges — see the
  `filter-existing` admin probe.
- **Ephemeral admin-test sessions** (`metadata->>'kind' = 'admin_test'`) older
  than `ROUTER_SESSION_GC_TEST_RETENTION_DAYS` (default 30) with no remaining
  tasks (a cheap raw delete — they carry no dependents).

Closed sessions thus no longer accrue indefinitely; an open session is never
GC'd, so a long-lived conversation is unaffected.

**Invitation GC.** Invitations are single-use and short-lived (the
agent suite mints a fresh one per agent on every launch — see
`scripts/prod.sh` `refresh_invitations` — with a 10-min TTL set by
`bp_agents.bootstrap`). A sibling loop (`tasks.invitation_gc_loop`,
hourly, 7-day retention) hard-deletes **terminal** invitations only —
`DELETE FROM invitations WHERE COALESCE(used_at, expires_at) < cutoff`.
A live invitation (unused **and** unexpired) has a future `expires_at`,
so it is never touched; a just-used or just-expired row is kept for the
retention window (audit) and then reaped. Without this the table grows
~one dead row per agent per relaunch forever.

### 2.3 Quotas and budgets — partially shipped

**Status: admit-rate quota is shipped; the broader counter table is
still planned.**

Shipped today: a per-`(user_id, level)` **admit-rate token bucket**
enforced at `NewTask` admit (`bp_router/security/rate_limit.py`
`TokenBucket`; per-tier `quota_admit_rate_per_s` /
`quota_admit_burst` Settings with real defaults; atomic via a Valkey
Lua script, falling back to a bounded per-process LRU when Valkey is
absent). Exceeding it rejects the admit with
`Ack{accepted:false, reason:"quota_exceeded"}` carrying
`retry_after_s`. This is the throughput cap; it is per-user, **not**
per-agent or per-task (see `docs/security.md` §12 for the fairness
boundary and `docs/design/quota-enforcement.md` for the design).

Still planned: the durable per-user **counter table** below
(concurrent-task depth cap, token / cost / storage budgets). Until
that ships, only the admit-rate axis is enforced router-side; the
rest rely on upstream (BFF, provider) limits.

Target shape — every user will carry quota counters tracked in
Postgres (or a Valkey cache for hot-path checks):

| Counter              | Window       | Default              | Enforced where                     |
| -------------------- | ------------ | -------------------- | ---------------------------------- |
| `tasks_started`      | per day      | 1 000                | router on `NewTask` admit          |
| `tasks_concurrent`   | live         | 10                   | router on `NewTask` admit          |
| `llm_input_tokens`   | per day      | 1 000 000            | LLM service on call                |
| `llm_output_tokens`  | per day      | 250 000              | LLM service on call                |
| `provider_cost_usd`  | per month    | per-tier-default     | LLM service on call                |
| `file_storage_bytes` | live         | 1 GiB                | File-store upload                  |

Quota enforcement happens at the latest possible point so that a
declined task does not consume downstream budget. Exceeding a quota
returns `Error{code:"quota_exceeded", retryable: false}` with a
`retry_after` hint in metadata.

### 2.4 RBAC — principal levels

Every user is classified by exactly one `level`. The schema is fixed;
deployments may add as many `tierN` values as they like, but the three
non-tier kinds are reserved.

| Level     | Meaning                                                                              |
| --------- | ------------------------------------------------------------------------------------ |
| `admin`   | The only level that satisfies `require_admin`. Manages users, agents, ACL, audit.    |
| `service` | Automated principal. Equivalent to `tier0` for `require_tier(N)`; not `admin`.       |
| `tierN`   | Human user at tier `N`. **`tier0` is most privileged; `tierN` is least** (matches the agent-tier convention in `docs/acl.md` §3.4). |

Tier ordering is "lower number = more privileged." `require_tier(N)`
admits any level whose tier index is `≤ N`, so `require_tier(2)`
accepts `admin`, `service`, `tier0`, `tier1`, `tier2` and rejects
`tier3+`. `admin` and `service` always satisfy any tier ceiling.

Enforcement primitives (FastAPI deps in `bp_router.security.jwt`):

| Dependency             | Admits                                              |
| ---------------------- | --------------------------------------------------- |
| `require_admin`        | `admin` only                                        |
| `require_service`      | `service` only                                      |
| `require_tier(N)`      | `admin`, `service`, `tier0` … `tierN`               |
| `require_authenticated`| any valid level (no tier gate)                      |

Level is enforced at the router HTTP edge (admin endpoints take
`require_admin`; user-facing endpoints take `require_authenticated`
or `require_tier(N)`) and surfaces in the session JWT as the `level`
claim. The ACL evaluator receives it as the `user_level` argument
to `is_allowed_for(...)` on every `NewTask` admit and matches it
against the rule list's `user_level` field (see [`../acl.md`](../acl.md)).

### 2.5 Per-user agent visibility

Agents may be gated per user level via ACL rules. To make
`@image_generator` callable only by `tier1` and stricter, write
two rules — a deny above an allow:

```
ord  effect  user_level  caller    callee
1    allow   tier1       */*   ->  @image_generator
2    deny    *           */*   ->  @image_generator
```

(Or use the `simulate` admin endpoint to verify intent — see
`acl.md` §12.) Permission is re-evaluated on every `NewTask` admit
against the live rule list, so admin edits take effect immediately.
Catalog projection (visibility) is best-effort: an agent's cached
catalog refreshes when the router pushes a `CatalogUpdate` frame
on rule mutation (`acl.md` §8.1).

## 3. Access control

Agent → agent visibility and permission are governed by an ordered
firewall-style rule list. Each rule is a 4-tuple
`(effect, user_level, caller_pattern, callee_pattern)` evaluated
top-to-bottom; first match wins, default deny. The same rule list
covers both the catalog projection in `Welcome.available_destinations`
and the permission check at `NewTask` admit.

`AgentInfo` carries identity only (`groups`, `capabilities`,
`agent_id`); the rule list is admin-managed via
`/v1/admin/acl/rules`.

Full grammar, evaluation algorithm, schema, admin API, observability,
and worked examples are in [`acl.md`](../acl.md).

