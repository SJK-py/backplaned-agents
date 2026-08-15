# Backplaned Platform — Modification Changelog

> This repository was created from the **Backplaned** template and vendors
> the platform packages (`bp_protocol`, `bp_sdk`, `bp_router`, `bp_admin`)
> plus their tests. Building the agent suite (`bp_agents`, see
> [`agent-suite/`](../agent-suite)) occasionally requires changing that
> vendored platform code. **Every such change is logged here** so the
> suite's footprint on the platform is explicit — for future re-sync with
> an upstream Backplaned, for upstreaming a fix back, and for review.
>
> Scope: changes to **platform/infra code only** (`bp_protocol`, `bp_sdk`,
> `bp_router`, `bp_admin`, and the platform tests under `tests/`). Pure
> suite code (`bp_agents/`) and suite docs (`agent-suite/`) are **not**
> tracked here — they are new, not modifications.
>
> Change types: **Added** (new, backward-compatible surface) ·
> **Fixed** (bug fix) · **Changed** (behavior change) · **Removed**.

---

## 2026-08-17

> A THIRD bridge-provisioned agent kind: operator-authored Python functions
> (`code_<slug>`), run in a uid-dropped subprocess. The runtime ships in
> `bp_mcp_bridge` and is not tracked here; these are the **platform**
> (`bp_router` / `bp_admin`) changes it required, plus two fixes to the
> custom-LLM-agent surface it sits beside. Design:
> [`../design/bridge-python-code-agents.md`](../design/bridge-python-code-agents.md).

### Added — `code_agents` table + admin CRUD

- **What:** **migration `0013_code_agents`** — one row per operator-authored
  function: `code`, `entrypoint`, TYPED `parameters`, an optional `returns`
  JSON Schema (published as the agent's `produces_schema`), `secret_refs`,
  `timeout_s` / `memory_mb`, and the same groups/capabilities/expose/enabled
  surface `custom_agents` has. `CodeAgentRow` + the `insert/get/list/update/
  delete` query set; `POST/GET/PATCH/DELETE /v1/admin/code-agents` plus
  `/reconnect` and `/connected`, gated exactly as the MCP + custom endpoints
  are (reads admin-or-bridge, writes admin).
- **Why a separate table, not a `kind` column on `custom_agents`:**
  `custom_agents.preset_name` is `NOT NULL REFERENCES llm_presets(name)`. A
  discriminator would force it nullable — dropping a real constraint on every
  existing LLM row for a kind that will never pick a preset.
- **Two validator rules worth naming:** `secret_refs` values must be
  `env://` / `secret://` REFERENCES — a literal is refused, the same posture
  as `mcp_servers.auth_value_ref` — and `returns` is checked as a real JSON
  Schema at write time rather than discovered invalid when a caller reads the
  catalog. The audit payload carries `sha256(code)` and its length, **never
  the body**: an append-only hash chain containing operator code is an
  erasure problem, and the code is already in the row.

### Added — `/admin/code-agents` UI

- **What:** list + form pages mirroring `custom_agents`, with a code
  textarea, a typed-parameter editor, secret-reference rows, and
  timeout/memory inputs. Registered in the nav between MCP servers and the
  audit log.
- **Why the warning banner:** the form states plainly that the code runs on
  the bridge host with that container's unrestricted network access. Per-agent
  egress control needs `CAP_NET_ADMIN`, which the bridge deliberately does not
  have; saying so is the honest alternative to a setting that cannot enforce
  what it implies.

### Fixed — custom-agent PATCH accepted duplicate parameter names

- **What:** `_check_param_names_unique` ran only in `CustomAgentCreate`'s
  model validator. `PATCH /v1/admin/custom-agents/{id}` re-validated prompt
  placeholders against the merged record but never uniqueness, so a duplicate
  name was written, `_accepts_schema` silently collapsed it to one property,
  and the admin UI rendered a row that did nothing. The rule is now applied to
  the merged parameter list on both paths.

### Fixed — bridge-hosted non-MCP agents had no metrics at all

- **What:** every metric in `bp_mcp_bridge.metrics` was incremented only on
  the MCP path, and `active_bridges` was set from the MCP map alone — so a
  deployment running custom or code agents and no MCP servers reported
  `active_bridges 0`, with no call volume, latency or failure signal. Added
  `agent_calls_total`, `agent_call_duration_seconds`,
  `agent_bridge_starts_total`, `agent_bridge_exits_total` and
  `active_agent_bridges`, all labelled by `kind` (`custom` | `code`).
- **Why not a metric per kind:** the label set stays small and bounded, and a
  dashboard can sum across kinds or split by one. `active_bridges` keeps its
  MCP-only meaning so existing dashboards don't silently change.

### Fixed — a flaky rate-limit test the new e2e coverage made likelier

- **What:** `test_bucket_consumes_until_empty` drained a burst at 10 tokens/s,
  so a token refilled every 100 ms — and a loaded machine taking longer than
  that over four Redis round trips saw the fourth call *allowed* and the test
  fail for a reason it is not about. Now 0.5/s, a 2 s refill window, which no
  load this suite produces can outrun.
- **Why now:** the code-agent e2e tests add two router startups and lengthen
  the full run by roughly a third, which raises the odds of exactly this
  race. Leaving a known flake more likely than it was is not a neutral act.

### Changed — `StdioSpawnConfig` gained `rlimit_fsize_bytes`

- **What:** an additive field on the shared spawn struct, applied in
  `_stdio_preexec` alongside NPROC / AS / CPU. Default 0 (disabled), so the
  stdio MCP path is unchanged — an MCP server may legitimately cache large
  artifacts. Code agents set 64 MB.
- **Why:** nothing bounded disk. A runaway write from operator code fills the
  volume every other bridged agent shares.

## 2026-08-16

> The suite's conversation moved into the router's session store. The suite
> no longer has a `session_history` or a `session_info`; agents write only
> their own threads, and the channel — which can write none — drives session
> state, hand-overs and the turn lease as a steward. Design:
> [`../design/router-managed-session-store.md`](../design/router-managed-session-store.md)
> §13 (and §13.3, where the implementation departed from the table).
>
> Almost all of this is suite work. One platform addition, one platform
> nicety — and, when the user's settings followed the conversation across
> (§13.5), one platform test for the path they took.

### Added — `TestRouter.session_messages`

- **What:** read one thread straight out of the session store, as a steward
  (`owner_agent_id` is a parameter; reads take one, writes cannot).
- **Why:** the e2e assertion surface for *"did the agent record its turn?"*
  used to be a suite table a test could query directly. It isn't one any
  more, and an e2e that can see the frames but not the conversation can only
  assert that a reply came back — not that it landed anywhere.

### Changed — the suite no longer needs Valkey for a second channel

- Turn ordering was an in-process `asyncio.Lock` plus an optional
  Valkey lock with a renewal watchdog (`bp_agents/session_lock.py`, deleted).
  It is now the router's per-session FIFO lease, so a webapp and a Telegram
  bot serialize against each other **through the router with nothing shared
  between them**. `SUITE_VALKEY_URL` is now required only by the KakaoTalk
  channel, for its parked-turn registry. `.env.example`, the prod compose and
  the settings docstring all said "needed to run more than one channel
  instance"; they no longer do.

### Added — `tests/test_session_store.py::test_steward_drives_user_scoped_state`

- **What:** pins the steward + user-scope + `session_scoped` state
  combination end-to-end against the real store: a caller with no agent
  identity writes it, it lands with `session_id IS NULL` and
  `owner_agent_id IS NULL`, a session-scoped read cannot see it, an agent in
  any session can, and it survives deletion of the session it was written
  through.
- **Why:** the suite's per-user settings moved onto exactly that path
  ([`../design/router-managed-session-store.md`](../design/router-managed-session-store.md)
  §13.5), and it was the one combination the store's own tests did not
  cover — user scope was proven for messages, not for state, and never from
  a steward. No `bp_router` code changed; the behaviour was already there.

### Note — no `bp_router` change was needed for the cutover

- The store, its steward HTTP surface, and `ctx.history` shipped on
  2026-08-09; the delegate's active-executor race was fixed on 2026-08-15 as
  part of the preset-slot work. Between them the suite side needed nothing
  new from the platform, which is the outcome a platform service is supposed
  to have.

---

## 2026-08-15

> Suite cutover to router-resolved preset slots. The four
> `user_config.preset_*` columns are gone; agents name a SLOT and the router
> resolves it from the user's own preference and their tier gate. Design:
> [`../design/router-resolved-preset-slots.md`](../design/router-resolved-preset-slots.md)
> (§5.1 and §8.1 record where the implementation departed from the text).
>
> Only one platform change was needed, and it was a latent bug the cutover
> surfaced rather than caused.

### Fixed — `_admit_delegation` flips the active executor BEFORE the ack

- **The bug:** the flip of `tasks.active_agent_id` to the delegate ran
  *after* awaiting the delegate's ack. But the SDK acks and then starts the
  handler, so for the width of the router's own commit the delegate is
  running while the task row still names the caller. Every identity the
  router derives from that column — `attachments.derive_task_file_scope` —
  refuses it: preset-slot resolution, tier-gated presets, named-file
  operations, and `SessionOp`, all on the delegate's opening turn, which is
  exactly when a hand-off does its work.
- **How it surfaced:** `tests/test_delegation_e2e.py` began failing ~60% of
  runs with `preset_not_allowed: caller could not be verified for slot
  resolution` the moment the suite's l1 agents started passing `slot=`.
  Nothing about slots is at fault — the previous common path used an ungated
  preset, which skips identity derivation entirely, so the window existed and
  simply had nothing looking through it.
- **The fix:** flip inside the same short-lived transaction that validates
  (with the `delegated` task_event and audit row), commit, refresh the
  per-frame authz cache, *then* deliver — unwinding with a guarded flip-back
  on disconnect / ack-timeout / rejection. This is the shape `admit_task` has
  always used for a fresh task (insert with the destination already active,
  force-fail on rejection); delegation was the inconsistent one. The
  connection is still released across the ack, so the R8 pool-exhaustion fix
  is untouched.
- **Bonus:** the old ordering documented an accepted loss — a cancel landing
  during the ack window left the delegate running orphaned until the deadline
  sweep. Now the cancel path reads the same column, finds the delegate, and
  cancels it.
- **Tests:** `test_review_delegation_pool_release.py` rewritten to pin the
  net effect (success → destination active; any failure → caller active)
  plus a source pin that the flip precedes `deliver_frame`.

### Changed — `bp_sdk` is untouched; the suite does the rest

- No further platform changes. `ctx.llm.generate(slot=…)` and
  `LlmResponse.resolved_preset` / `.preset_downgraded` shipped with the
  router half on 2026-08-11 and needed nothing new.

---

## 2026-08-12

> Deployment: agents run in **groups**, one process each, and the twelve
> single-use invitation tokens become one roster plus the chatbot's. Compose
> goes from 22 services to 11, 13 state volumes to 4, and three ordered
> one-shots to one. Design:
> [`../design/deployment-agent-host.md`](../design/deployment-agent-host.md).
>
> The router half is additive: a NULL `agent_ids` keeps the existing unbound
> single-use invitation behaviour exactly.

### Added — `invitations.agent_ids` roster (`0012_invitation_roster`)

- **What:** an optional `agent_ids` list plus a `consumed` array. With a
  roster set, `consume_invitation` accepts only a listed, untaken name and
  appends to `consumed`; the row stays live until the roster is exhausted, at
  which point `used_at` is stamped so the existing GC sweep still reaps it.
  `POST /v1/admin/invitations` accepts `agent_ids`.
- **Why this TIGHTENS rather than loosens:** `invitations` had no name column
  and `POST /v1/onboard` takes the name from the agent's own `agent_info`, so
  each of the twelve tokens sitting in an env file was an unbound bearer
  credential that could onboard as *any* agent. A roster token can only
  produce the names the operator listed.
- **Robustness:** the roster is read with `row.get("agent_ids")`, so a row
  projection without the column reads as "no roster" — which is exactly what
  a pre-migration row means.

### Added — `bp_agents/host.py` (supervised multi-agent runner)

- **Per-agent config is load-bearing.** `AGENT_STATE_DIR` and
  `AGENT_INVITATION_TOKEN` are process-wide, so hosted agents would share
  both. `load_agent` overrides each: `state_dir/<name>/`, because credentials
  live at `state_dir/credentials.json` and nine agents in one directory would
  overwrite each other's tokens and load someone else's on restart; and a
  token resolved as `<NAME>_INVITATION` first, roster second, because one
  shared token means the first agent to onboard consumes it — and would take
  the chatbot's `provisions_service_user` credential with it. Both were found
  in review, before either shipped anywhere. The compose also pins
  `SUITE_DB_POOL_MAX_SIZE` per group for the same reason: pool size is per
  agent, so nine agents at the suite default would open up to 90 connections
  from one container against a stock `max_connections` of 100.
- **What:** `python -m bp_agents.host --group suite-core` runs a group of
  agents on one event loop, each keeping its own identity, WebSocket and ACL
  position. Per-agent supervision with exponential backoff (reset after a
  healthy run), SIGTERM forwarded to all, and a non-zero process exit when
  *every* agent ends permanently failed — so a supervisor never sees a
  healthy-looking container full of dead agents.
- **Why group, not per agent:** the heavy dependencies load once per
  *process*. Measured: one suite agent imported is ~40 MB RSS; eleven in one
  process is ~96 MB.
- **`sandbox` and `mcp_bridge` are refused by name.** The sandbox runs as
  root with `CAP_SETUID`, `no-new-privileges` and no DB network — those
  capabilities and that network position *are* the isolation, and co-hosting
  anything with it would hand that agent the same. A group naming either
  exits with an error rather than silently dropping it.
- **A clean return from `run_async` stops supervision**, it does not restart:
  the SDK reconnects transient transport failures internally and raises
  `TransportPermanentlyFailed` for the rest, so returning normally is a
  deliberate stop. (Caught by a test before it shipped.)

### Added — `bp_agents/init.py`, one-shot instead of three

- **What:** router schema → suite schema → invitations + ACL in one service,
  with `--step` for running any one of them alone. Replaces the `migrate` →
  `suite-migrate` → `bootstrap` chain and the `depends_on` edge every agent
  service declared. Stops at the first failure — a half-migrated schema with
  a bootstrapped ACL is harder to reason about than a clean stop.
- **The admin credential stays here and the host never holds one.**

### Added — `scripts/gen_env_reference.py` → `docs/env-reference.md`

- **What:** generates the complete variable reference from the settings
  models — **222 variables**, against the 24 `.env.example` documented while
  the README called it complete. A test runs it with `--check`, so it cannot
  drift.
- **Why:** the dict-shaped settings (`file_storage_quota_bytes`,
  `session_store_quota_bytes`, `llm_default_presets`) appeared nowhere, so an
  operator tuning a quota had to read `settings.py` to learn the variable
  existed.

### Changed — `docker-compose.prod.yml` rewritten around groups

- **What:** 22 services → 11 (`suite-core`, `channels`, `sandbox`, `init`,
  plus infra and the two profiles); 13 state volumes → 4; 12 mandatory
  invitation vars → 2 (`SUITE_ROSTER_TOKEN` + `CHATBOT_INVITATION`).
- **Why two and not one:** the chatbot's invitation is flagged
  `provisions_service_user` — a higher-privilege credential yielding a
  minting-capable principal — and the flag is per-invitation, not per-name.
  Bundling it would hand eleven ordinary agents the same.
- **Memory caveat:** `md_converter`'s `mem_limit: 2g` existed so the OOM
  killer would target a ballooning conversion child. In `suite-core` that cap
  covers nine agents, so it is raised to 4g (`SUITE_CORE_MEM_LIMIT`); the
  mechanism holds (conversion still runs in a forked child) but the margin is
  shared. Recorded as an open question in the design.

### Changed — compose-shape tests updated, properties preserved

- **What:** `test_compose_every_suite_agent_sets_name_and_token` now walks
  group services and checks `SUITE_AGENT_GROUP` against `host.GROUPS`;
  `test_bootstrap_compose_env_covers_full_roster` checks the roster token
  plus every service-user var; the custom-env-file least-privilege test names
  the group services. Two invitation tests moved from `args[-1]` to
  positional indices — appending the roster column had silently moved those
  assertions onto the wrong argument.

---

## 2026-08-11

> Preset **slots**: an agent names an opaque preference key ("balanced") and the
> router resolves it to a preset from the user's own choice and the operator's
> default, gate-checked at every step. Joins two halves that never met — the
> router owned the ceiling (`min_user_level`), the suite owned the choice
> (`user_config.preset_*`) — without teaching the router what "balanced" means.
> Design: [`../design/router-resolved-preset-slots.md`](../design/router-resolved-preset-slots.md).
>
> Additive: an agent that never sends `preset_slot` behaves exactly as before.

### Added — `preset_slot` resolution (`bp_protocol`, `bp_router/llm`, `bp_sdk`)

- **What:** `LlmRequestFrame.preset_slot` (mutually exclusive with `preset`,
  rejected at validation rather than silently resolved one way);
  `LlmResultFrame.resolved_preset` + `preset_downgraded`;
  `LlmService.resolve_slot`; `Settings.llm_default_presets` (slot → preset,
  validated against the loaded preset map after every reload);
  `ctx.llm.generate(slot=…)` with the two new fields on `LlmResponse`.
- **Resolution order:** user preference if it passes the gate → else the slot
  default, flagged `preset_downgraded` → else the slot default → refuse if the
  default itself is unreachable → `preset_slot_unknown` for an unknown slot.
- **Why a slot degrades where an explicit `preset=` refuses:** "give me the
  balanced model" is satisfiable by degrading; "give me claude-opus" is not.
  Without the degrade, a user demoted after choosing has *every* turn fail
  with no path that re-resolves — the old behaviour.
- **Why the router never interprets a slot name:** no `Literal["pro",…]`, no
  per-slot branch. "Balanced" means whatever `llm_default_presets` says, so
  the vocabulary lives in operator config rather than platform code — the same
  discipline `role` and `item_kind` follow in the session store.

### Added — `user_llm_preferences` + user-facing LLM endpoints

- **What (`0011_user_llm_preferences`, `bp_router/api/llm.py`):** a
  `(user_id, slot) → preset_name` table, plus `GET /v1/llm/presets` (only
  presets the caller's level satisfies) and `GET|PUT /v1/llm/preferences`
  (gate-checked on write, returning `403 preset_not_allowed` with the required
  level).
- **Why its own table rather than the session store's user-scoped KV:** that
  namespace is writable by any agent in the user's session, and the router
  *acts* on this value — it selects a model, at a cost, under a tier gate. A
  value the router enforces policy on must not be one any agent can overwrite.
  There is no agent-facing write path at all.
- **Why the listing endpoint matters:** it filters with the same
  `user_level_satisfies` the call path gates on, so the menu cannot drift from
  the entitlement. The suite's `selectable_presets_*` was a global list that
  could not be correct for a tier1 and a tier3 user in one deployment, and the
  suite could not compute a correct one because preset enumeration was
  admin-only.
- **Cache:** preferences load with the user's level into the existing
  `_UserLevelCacheEntry` (60 s TTL), so a slot costs no extra round trip; a
  preference write calls `invalidate_user_level`, without which the old model
  would keep running for up to the TTL.

### Changed — two source-pin tests updated for the new gate shape

- **What:** `test_l3_dispatch_tier_lookup_is_task_derived_and_gated` and
  `test_dispatch_user_level_lookup_error_proceeds_for_open_preset` pinned the
  literal `if first_preset_gated:`, now `... and not slot_level_resolved:`.
- **Why this is a refinement, not a weakening:** slot resolution needs the
  user's preference, so a slot request derives identity unconditionally — by
  necessity. The gating assertions now run against the source *after* the slot
  block (so a `*` preset with no slot still pays nothing), and a new assertion
  requires the slot path to derive identity through `_derive_task_scope` before
  reading any preference — an agent must not read a preference for a user it
  merely claimed to be.

---

## 2026-08-10

> Follow-up to the session store: a FIFO bug in the turn lease found by review,
> the transport-layer tests that would have caught it, and an unrelated flaky
> assertion in the token-bucket suite.

### Fixed — turn lease lost FIFO order across a long hold

- **What (`bp_router/session_store.py`):** `_op_acquire_lease` swept stale
  queue rows *before* upserting the caller's own, so a waiter that had been
  queued longer than its own `ttl_ms` — because the holder was mid-long-turn —
  had its row deleted and was re-inserted with a NEW, higher ticket. It then
  sat behind waiters that happened to retry more recently. The upsert now runs
  first; the sweep still reaps genuinely abandoned rows, and a holder resuming
  after its own lease expired keeps the lease it still holds.
- **Why it matters:** the lease's entire contract is that ordering comes from
  the ticket sequence and never from retry timing — that is the reason it is a
  ticket queue rather than a mutex with a backoff loop. Under a long hold the
  shipped code silently violated exactly the property
  `docs/design/router-managed-session-store.md` §6.4 asserts. Reproduced
  against Postgres before the fix (two waiters queued in order; the first aged
  out behind a 10-minute hold; release promoted the **second**);
  `test_lease_keeps_its_place_across_a_long_hold` drives the same sequence.

### Added — transport-layer tests for the session store

- **What (`tests/test_session_store.py`):** the original suite exercised the
  store module, the protocol shapes and the SDK, but nothing drove the router
  handler — which is where the bug above lived. Five pins added: `SessionOp`
  has no `user_id` field to assert, the handler derives scope through
  `_derive_task_scope` and refuses writes to a closed session, promotions are
  pushed after both the commit and the reply, `_session_audit_payload` never
  emits message content for any mutating op, and the built OpenAPI route table
  exposes no message-POST (stronger than the previous source grep).
- **Why:** the audit-hygiene property was a stated security guarantee with no
  test, and "test the module, not the transport" is what let a statement-order
  bug ship green.

### Fixed — flaky wall-clock assertion in the token-bucket tests

- **What (`tests/test_redis_integration.py`):**
  `test_bucket_consumes_until_empty` asserted `0.05 < retry_after_s`, which
  requires four `try_consume` calls to complete in under ~50 ms. Real time
  between calls partially refills the bucket, so the wait is anywhere in
  `(0, 0.1]`; only the ceiling is deterministic. Now asserts that.
- **Why:** it passed two full local runs and failed the third under load —
  the shape that reddens CI intermittently and trains people to re-run.

### Changed — internal cleanup

- **What:** dropped three lint-appeasing `_ = x` no-ops in
  `bp_router/session_store.py` and an unused `_Handle._index` slot in
  `bp_sdk/history.py`.

---

## 2026-08-09

> The router now owns **conversation** — a session-scoped message log, per-thread
> and per-session state, a hand-over queue, and a FIFO turn lease — the way it
> already owns identity, tasks, and files. Agents reach it over one typed frame
> (`SessionOp`) or, for gateways, session-authed HTTP. The load-bearing property
> is structural rather than enforced: **an agent can only write its own threads,
> because no write op has a field in which to name another agent's.** Full design
> in [`../design/router-managed-session-store.md`](../design/router-managed-session-store.md).
>
> Additive throughout: new tables, new frames, new SDK surface. No existing
> frame, endpoint, or table changed shape, and `bp_agents` is untouched — the
> suite keeps its own `session_history` until it is rebuilt on this.

### Added — session-store frames (`bp_protocol/frames.py`)

- **What:** `SessionOpFrame` (agent → router; `task_id`, `scope`, and an ordered
  `ops` list, max 32), `SessionResultFrame` (positional `results`, one per op,
  plus `error`/`error_index` for a refused batch), and `SessionLeaseFrame`
  (router → agent, unsolicited: a waiting turn-lease ticket was promoted). The
  op union is `kind`-discriminated with `extra="forbid"`: 6 writes (`append`,
  `set_floor`, `set_state`, `redact`, `hand_over`, `consume_handovers`), 4 reads
  (`read`, `get_state`, `stat_thread`, `list_threads`), and 4 concurrency ops
  (`assert_thread`, `acquire_lease`, `renew_lease`, `release_lease`). Result
  payload models: `SessionMessage`, `ThreadStat`, `StateValue`, `HandoverItem`,
  `LeaseStatus`, `SessionOpResult`.
- **Why the write ops carry no owner field:** a file lives in a shared
  per-session namespace any peer may write, but a message is attributable
  speech. The router stamps `owner_agent_id` from the task's active executor, so
  writing in another agent's name is *unrepresentable* rather than rejected —
  there is no check to misconfigure. Reads *do* take `owner_agent_id`, because
  reads are session-scoped by design (a summarizer must read the thread it
  summarizes) and reading cannot fabricate an utterance.
- **Why `role` is an opaque string** with no enum and no CHECK: the moment the
  platform knows what "assistant" means, it owns a conversation model. Every
  read names the roles it wants. `metadata` jsonb on messages and state values
  is the extension point that keeps suite-specific fields out of platform
  schema.

### Added — `WelcomeFrame.features` (`bp_protocol/frames.py`)

- **What:** a defaulted `features: list[str]` advertising router capabilities to
  the connecting SDK (e.g. `session_store.v1`).
- **Why:** the existing `capabilities` field cannot serve this — it echoes the
  *agent's* own declared capability list (`ws_hub.py:630`). Defaulted on both
  sides, so an older SDK that never reads it and an older router that never sets
  it are both unaffected.

### Added — `bp_router/session_store.py` + migration `0010_session_store`

- **What (schema):** five tables. `session_messages` (append-only; `content`
  capped at 256 KiB, `redacted_at` tombstone that blanks content but keeps the
  id so cursors stay valid); `session_threads` (one row per
  `(scope, owner, thread_key)` carrying `floor_id` and the denormalised
  above-floor counters); `session_state` (`(scope, owner?, thread, key)` →
  `value` + `version` CAS token); `session_handovers` (the queue, with a partial
  index so a drain never scans consumed rows); `session_turn_queue` (the lease,
  with a partial unique index making "at most one holder per session" a database
  guarantee rather than a code path). `session_id` is nullable — NULL rows are
  the cross-session `user` scope, the conversational analogue of the file
  store's `persist/`. Every table cascades from `sessions`.
- **What (module):** `execute_batch(conn, scope, ops)` applies an ordered op
  list inside a transaction the *caller* owns, so reads observe the writes
  before them and any `SessionStoreError` rolls the whole batch back. One module
  backs both surfaces (WS frame and HTTP), exactly as `file_store` backs the
  file frames and `/v1/files/names`, so they cannot drift.
- **Why no `incumbent` column:** retirement of any shape is a move of one
  monotonic `floor_id` cursor — prefix folding (summarization) and whole-thread
  retirement (floor = last id) are the same operation. A mutable per-row flag
  would be the last remaining way to reach into another agent's thread.
- **Why no `author_agent_id`:** with the owner derived, the author *is* the
  owner; a separate column would record nothing the thread key doesn't already
  say. An earlier draft of the design carried one, for attributing *permitted*
  cross-thread writes — which this design does not have.
- **Why the counters are denormalised:** it makes "should I summarize?" a
  single-row read instead of a thread scan, and gives quota a number to gate on
  without an aggregate. Appends and redactions adjust by delta; a floor move
  pays for one aggregate over the retired range (rare — one per summarization).

### Added — `SessionOp` dispatch handler (`bp_router/dispatch.py`)

- **What:** derives `(user_id, session_id)` from the task row via
  `attachments.derive_task_file_scope` (the same primitive the file frames use,
  which also verifies this socket's agent is the task's active executor), refuses
  writes to a closed session with `session_closed`, applies the batch in one
  transaction, appends a hash-chained audit event per mutating op, and replies
  `SessionResult`. Lease promotions are pushed **after** the commit via the new
  `delivery.notify_lease_promotions`.
- **Why audit payloads carry no `content`:** ids, thread coordinates, role, and
  byte counts only. Conversation text in an append-only hash chain is an erasure
  problem `purge_user` cannot solve.
- **Why promotions are pushed post-commit:** announcing a lease inside the
  transaction would advertise one that a rollback then erased.

### Added — steward HTTP surface (`bp_router/api/sessions.py`)

- **What:** `GET /v1/sessions/{id}/messages` (cursor-paginated transcript),
  `GET /threads`, `POST /handovers`, `GET|PATCH /state` (session-scoped keys),
  `POST /ops` (an arbitrary batch), `POST|DELETE /lease`, and `PATCH
  /v1/sessions/{id}` (shallow-merge into the session's `metadata`, backed by the
  new `queries.Scope.patch_session_metadata`).
- **What is deliberately absent:** there is **no** message-POST endpoint. The
  steward path runs batches with `agent_id=None`, so every thread-writing op
  refuses `denied` inside the store — the endpoint set needs no allowlist of its
  own, and a session JWT cannot append to any thread at any time. A steward that
  wants something in a thread enqueues a hand-over and the owning agent
  materialises it under its own authorship.
- **Why HTTP at all:** a channel or webapp *spawns* tasks and is never a task's
  active executor, so it has no task from which scope can be derived — the same
  gateway case `/v1/files/names` exists for.
- **Lease over HTTP:** `409` + `Retry-After` derived from the current holder's
  TTL, because a steward has no socket for the router to push a promotion to.

### Added — `ctx.history` (`bp_sdk/history.py`, `bp_sdk/context.py`)

- **What:** `SessionHistory` with one-op conveniences, a `batch()` builder whose
  handles resolve positionally when the batch commits, and `turn()` — the
  default path, **ordered by default**: it takes the FIFO lease, renews it on a
  timer for the length of the turn, and releases it even on failure.
  `turn(ordered=False)` opts out. `read()` pages transparently when the router
  truncates against its byte budget. `user_scope` switches to the cross-session
  namespace.
- **Why the signatures are asymmetric:** `append` has no `owner` parameter at
  any level, `read` does. The SDK must not reintroduce what the wire format
  removed — a test asserts exactly this.
- **Why ordered-by-default:** without serialization, concurrent turns interleave
  appends and read stale context. The store stays *safe* either way (thread
  assertions, state CAS, monotonic floors, idempotency keys), but coherence
  needs ordering, so a suite gets it unless it deliberately opts out.
- **Batch results are count-checked:** a short `results` list raises
  `result_count_mismatch` rather than leaving a handle silently unresolved —
  the failure mode if an older router skips an op it doesn't know.

### Changed — SDK frame routing and per-task teardown (`bp_sdk/dispatch.py`)

- **What:** `SessionResultFrame` resolves on the existing `pending_acks` map
  (like `FileResult` / `Ack` / `Pong`); `SessionLeaseFrame` is routed to
  whichever live `ctx.history` handle is waiting on that ticket via a new
  `_session_histories` map, cleared in `_run_handler`'s finally beside the
  `FileStash` inbox teardown.
- **Why the lease push is best-effort:** a handle that has already gone away
  drops it, and the waiter re-acquires on the `holder_expires_at` deadline it
  was handed. That same fallback covers a *holder* dying without releasing —
  which is what lets promotion stay lazy, with no background sweep to fall
  behind.

### Added — `session_store_quota_bytes` (`bp_router/settings.py`)

- **What:** per-user-level ceiling on stored conversation bytes (256 MiB /
  64 MiB / 16 MiB for tier1–3, uncapped for admin/service/tier0), enforced on
  every append. Mirrors `file_storage_quota_bytes`.
- **Why usage counts only the active window:** folding a thread into a summary
  reclaims quota, which is what makes a long-running assistant sustainable.

### Added — `tests/test_session_store.py`

- **What:** 32 tests in three layers. *Structural* (no DB): `AppendOp` has no
  owner field and rejects one, the HTTP module has no message-POST, the steward
  path passes `agent_id=None`, the SDK's `append` has no `owner` parameter.
  *Store* (gated on `TEST_DB_URL`): derived ownership, idempotent appends,
  hand-over enqueue/drain/once-only, thread isolation with cross-thread reads,
  `thread_conflict` on an interleaved append, the summarize-apply CAS with its
  floor rolling back, floor monotonicity, redaction blanked at rest, the
  newest-first read budget and paging, content cap, quota, `user` scope
  surviving session purge, and the lease's FIFO ordering, promotion, expiry and
  `lease_lost`. *SDK*: batch builds one frame in order, turn is ordered by
  default, unordered takes no lease, typed errors carry the op index.

---

## 2026-06-29

> The `openai-compatible` chat adapter now surfaces separated reasoning
> (`message.reasoning_content` / `message.reasoning`) as the response's thought
> summary, so reasoning endpoints (Friendli/vLLM/SGLang/OpenRouter) show their
> "thinking" line instead of silently dropping it.

### Added — surface reasoning from OpenAI-compatible endpoints

- **What (`bp_router/llm/providers/openai_compatible.py`):** `_convert_response`
  now reads `message.reasoning_content` (de-facto field on DeepSeek/vLLM/SGLang/
  Friendli) or `message.reasoning` (OpenRouter & gateway alias) and sets it on
  `LlmResponse.thought_summary`. The streaming path yields the matching
  `delta.reasoning_content` / `delta.reasoning` chunks as
  `LlmDelta(text=..., thought=True)`, which the SDK aggregator concatenates into
  the response's thought summary. Display-only — never round-tripped back as
  model input. Inline `<think>…</think>` left in `content` is not parsed.
- **Why:** these endpoints split reasoning out of `content` (often gated behind
  a server knob passed via `default_provider_options.extra_body`, e.g.
  `parse_reasoning` + `chat_template_kwargs.enable_thinking`). The adapter
  previously read only `content` + `tool_calls`, so the reasoning was dropped.
  Additive and backward-compatible — servers that don't emit these fields are
  unaffected. Documented in `docs/admin-ui.md` (LLM presets →
  `provider_options`) and `bp_router/llm/presets_catalog.jsonc`.

---

## 2026-06-27

> New `DELETE /v1/files/names` lets a session-authed gateway (the webapp) unbind
> a stash name in its own scope — the HTTP counterpart of the WS `FileManage`
> delete the agent path already had.

### Added — `DELETE /v1/files/names` (session-authed named-store delete)

- **What (`bp_router/api/files.py`):** a session principal can now unbind one
  stash NAME (`{file}` session-scoped, or `persist/{file}`) via
  `DELETE /v1/files/names` with `{name, session_id?}`. Mirrors `bind_name`'s
  auth/scope path — `require_authenticated` → `Scope.user` → `_resolve_scope`
  (which enforces session ownership) — and calls the existing
  `Scope.delete_file_name`; the blob is left for the refcount sweep. Returns
  `{"deleted": 0|1}` (idempotent — `0` when the name wasn't bound) and writes a
  `file.delete` audit event.
- **Why:** the webapp file-stash pane gained a per-file Delete button. The named
  store previously exposed delete only over the WS `FileManage` frame (the agent
  path), not to a session-JWT gateway. Additive and backward-compatible (a new
  endpoint; existing routes unchanged).

## 2026-06-26

> `ctx.llm.embed` now **auto-splits** a large input list so neither the request
> nor the inline vectors result exceeds the WS payload cap — fixing a hang when
> an agent embeds many chunks (the binding limit is the result frame, not the
> request).

### Fixed — `embed()` bounds request + result frames by the payload cap

- **What (`bp_sdk/llm.py`):** an embedding result rides INLINE in
  `LlmResultFrame.vectors`; at ~21 bytes/float a 100 × 1536-d batch is ~3 MiB,
  over the ~1 MiB `max_payload_bytes` cap. The router has no outbound chunking,
  so it can't deliver the frame and the caller's `embed()` hangs. `embed()` now
  splits the input list into sub-requests sized so both the request frame
  (input texts) and the result frame (vectors) stay under a fraction of the
  router-negotiated cap. The embedding dim is unknown until the first response,
  so the first batch assumes a worst-case dim (its result is guaranteed to fit)
  and later batches use the dim actually returned. Vectors are returned in
  input order; a call that already fits is a single request (unchanged).
- **Why:** `knowledge_base.store` embeds a document's chunks; a large document
  would otherwise hang the agent. Fixing it in the SDK covers every bulk-embed
  caller (the suite keeps `kb_embed_batch_size` only as the provider
  input/token-limit guard, a separate axis the byte budget can't see).

## 2026-06-26

> `read_file` gains **character windowing** for text files: a bounded slice
> (default first 20000 chars) with a marker + next `offset`, so a large file
> can't flood the model's context and is page-able. Images/PDFs unchanged.

### Changed — `read_file` returns a bounded text window (SDK-side)

- **What (`bp_sdk/file_tools.py`):** the `read_file` tool gains optional
  `max_chars` (default 20000, hard-capped at 500000) + `offset` (default 0).
  For a TEXT file (decided by extension), `dispatch_file_tool` streams the blob
  to a local temp copy (`FileStash.read`) and slices the
  `[offset, offset+max_chars)` CHARACTER window off disk with an incremental
  UTF-8 decoder (`_slice_text_file`), so only the window is ever held in
  memory — a multi-GB file slices without being loaded whole. Result is a plain
  text part: `File: <name> (characters A–B of N)` plus a `…[K more characters —
  call read_file again with offset=B]` marker when truncated. Previously it
  always returned a name `file_ref` and the router inlined the WHOLE file
  (bounded only by the ~5 MiB byte cap), which could overflow context with no
  way to read a slice.
- **What stays:** images / PDFs / unknown types still return a `file_ref`
  (router-resolved, multimodal) — `max_chars`/`offset` are text-only. A file
  that looked textual by extension but isn't valid UTF-8 falls back to the
  `file_ref` path.
- **No whole-file size cap:** because the window is sliced off disk with
  bounded memory, a text file of any size is page-able — the `max_chars` window
  (≤ 500000 chars) is the only bound on what reaches context. (An earlier
  revision refused files over a byte cap; dropped — it defeated paging and the
  window already bounds context.)
- **Why:** the per-read window (`max_chars`) is the context guard; windowing
  lets the model read big logs / CSVs / markdown in bounded, page-able chunks
  instead of all-or-nothing.
- **Limitation:** memory is bounded, but the file store has no RANGE read, so
  the SDK still downloads the whole blob to disk per call — paging a large file
  re-downloads it each page. A range fetch would make paging O(window); future
  work.

## 2026-06-26

> A large `write_file` no longer dies on the WS frame cap: `FileStash.write`
> now routes any payload that wouldn't fit one frame over the HTTP upload path
> instead, and the upload ceiling rises 25 → 50 MiB. Lets agents (e.g. the
> suite's `md_converter`) write multi-MiB Markdown without tripping a
> payload-too-large socket close.

### Fixed — `FileStash.write` streams oversize payloads over HTTP

- **What (`bp_sdk/files.py`):** `write()` previously inlined the whole text in a
  single `FileManageFrame`, so a payload over `max_payload_bytes` (~1 MiB)
  couldn't be written — and failed HARD, because the transport's pre-send
  `FrameTooLargeError` guard only covers `NewTaskFrame` (spawn/delegate), not
  file writes: the oversize frame reached the router and tripped its
  `1009 payload_too_large` socket close, dropping the connection + in-flight
  state. `write()` is now size-aware: it measures the actual serialized frame
  against the **router-negotiated** cap (`transport.welcome.max_payload_bytes`,
  falling back to the 1 MiB default) — the exact bytes the router size-checks on
  receive — and, when it won't fit, streams the UTF-8 bytes over HTTP via
  `store()` (the upload-with-grant path). Small writes are unchanged (one inline
  round-trip). The frame cap itself is deliberately NOT raised — bulk goes over
  HTTP rather than enlarging frames (which would force lockstep
  `ws_max_*`/proxy raises and head-of-line-block the multiplexed socket).
- **Why:** the suite's `md_converter` writes converted Markdown via
  `ctx.files.write`; a conversion over ~1 MiB (large/scanned PDFs, the Datalab
  backend's 200 MiB / 7k-page inputs) would otherwise 1009-disconnect the agent
  instead of saving the file. Centralising the fix in `write()` also covers the
  `write_file` tool and every other caller without touching them.

### Changed — `max_upload_bytes` default 25 → 50 MiB

- **What (`bp_router/settings.py`):** raised the single-upload ceiling (the
  `/v1/files` HTTP endpoint AND the agent `store` / upload-with-grant path —
  bytes that stream over HTTP, enforced mid-stream) from 25 to 50 MiB, with the
  docstring clarifying it's distinct from the ~1 MiB WS frame cap and is what
  now bounds a large `write_file` / conversion output (routed over HTTP).
- **Why:** give the over-HTTP write path real headroom for large conversion
  outputs now that `write()` routes there.

## 2026-06-25

> The router-managed file store gains **metadata access**: a `stat` command for
> one file and a `detail` flag on `list`, so an agent (and the model it drives)
> can see a stash file's type and size without reading it. Additive protocol
> fields only — wire-compatible with peers that predate it.

### Added — `StatFileRequest` + detailed `list` on the named file store

- **What (`bp_protocol/frames.py`):** new `StatFileRequest{name}` file command
  + `FileStatEntry{name, byte_size, mime_type, created_at}`. `ListFileRequest`
  gains `detail: bool=false`; `FileResultFrame` gains optional `stat` +
  `entries`. All new fields are optional/additive, so a peer on the old shape is
  unaffected.
- **What (`bp_router`):** `Scope.stat_file_name` / `Scope.list_file_entries`
  (`db/queries.py`) JOIN the `file_names` directory row to its `files` blob for
  `mime_type` (`byte_size` + `created_at` are already on the directory row),
  user-scoped exactly like `resolve_file_name` / `list_file_names`; new
  `FileEntryRow` model. `dispatch._handle_file_manage` gains a `stat` branch
  (resolve → `not_found`) and a `detail` branch for list.
- **What (`bp_sdk`):** `FileStash.stat(name) -> FileStat` and
  `list_detailed(...) -> [FileStat]` (`files.py`); `FileStat` re-exported from
  `bp_sdk`. `list()` is unchanged (still returns names) for back-compat. The
  `file_tools` bundle adds a `stat_file` tool, and `list_session_file` /
  `list_persist_file` now return each file's name + human size + type.
- **Why:** the suite's text-only vision sidecar (and the model itself) need a
  file's type/size before deciding whether/how to read it; the directory
  previously exposed only names.

## 2026-06-25

> Preset catalogue re-sync becomes **pinned-field**: operators keep control of
> the fields the catalogue doesn't pin. No migration; behaviour change to the
> every-boot upsert, a trim of the bundled catalogue, and a `scripts/prod.sh`
> rework that generates the operator overlay and pins the embedding width.

### Changed — catalogue re-sync overwrites only the fields an entry lists

- **What:** `upsert_managed_preset` (`bp_router/db/queries.py`) now takes a
  `pinned` set and builds its `ON CONFLICT DO UPDATE SET` from it — only the
  columns a catalogue entry actually listed are overwritten on re-sync; an
  omitted field keeps its existing DB value. The INSERT still writes every
  column (defaults for omitted fields), so first-creation is unchanged.
  `pinned=None` preserves the legacy overwrite-all behaviour for direct callers.
- **What:** `Preset` gains a non-comparing `specified_fields` (populated by
  `load_catalog` from the raw JSONC keys; empty for DB-built presets).
  `LlmService.load_presets_from_db` threads it as `pinned`, so a field an
  operator edits in the admin UI survives the every-boot re-sync **unless** the
  catalogue/overlay entry lists (pins) it. Supersedes the previous
  `min_user_level`-only carve-out — the tier gate is now just one of the
  operator-owned-by-default fields.
- **Why:** operators want to set the tier gate, sampling defaults, etc. on a
  catalogue-managed preset and have it stick, while the catalogue stays the
  source of truth for model identity + credential. Presence in the JSONC is the
  signal: list a field to pin it, omit it to leave it operator-owned.

### Removed — `description` / `default_provider_options` from the bundled catalogue

- **What:** the bundled `presets_catalog.jsonc` entries are trimmed to
  `name` / `provider` / `concrete_model` / `api_key_ref`; `description` and the
  embedding presets' `default_provider_options` (`{"output_dimensionality":
  1536}`) are dropped. With the dimension no longer pinned, the Gemini default
  embedding model emits its **native** vector width.
- **Why:** keeps the catalogue minimal (model identity + credential) and lets
  the embedding dimension be set at deploy time rather than hardcoded.
- **CAVEAT (resolved by the `scripts/prod.sh` change below):** with the width
  no longer pinned in the bundled catalogue, `SUITE_EMBEDDING_DIM` must match
  whatever `default_embedding` emits or KB/memory writes fail; `prod.sh` now
  asks for the width, pins it on `default_embedding`, and writes the same value
  to `SUITE_EMBEDDING_DIM`.
- **Verified:** `tests/test_preset_catalog_resync.py` (pinned overwrite +
  omitted-field preservation, against real Postgres),
  `tests/test_llm_preset_catalog.py` (presence capture),
  `tests/test_llm_embed_dimensions.py` (embedding presets no longer pin a width).

### Changed — `scripts/prod.sh` generates the overlay + asks the embedding width

- **What:** for a hosted provider (Anthropic / Gemini / OpenAI), `prod.sh` now
  GENERATES `deploy/presets.custom.jsonc`, repointing `default` at the chosen
  provider's balanced model and `default_embedding` at its embedding model — so
  a single-provider deploy no longer secretly depends on the bundled `default`
  (Gemini). Anthropic (no embeddings) uses OpenAI's `text-embedding-3-small`
  (prompts for `OPENAI_API_KEY`).
- **What:** the concrete models are read FROM the catalogue **by alias** (a new
  `catalog_field` awk helper), not hardcoded in `prod.sh` — `default` takes the
  balanced tier alias's model (`claude` / `gemini` / `gpt`) and
  `default_embedding` the embedding alias's. Two friendly embedding aliases were
  added to `presets_catalog.jsonc` for this: `gemini-embedding` and
  `gpt-embedding`. The models stay in sync as the catalogue moves.
- **What:** `prod.sh` asks for the embedding vector width (default 1536), PINS
  it on `default_embedding`'s `default_provider_options` (Gemini
  `output_dimensionality` / OpenAI `dimensions`) so the model emits exactly that
  width, AND writes the same number to `SUITE_EMBEDDING_DIM` — the two can no
  longer drift.
- **What:** the generator is clobber-safe — it writes the real overlay only when
  the file is absent, an unmodified `.example` seed, or a prior prod.sh output
  (marked by a sentinel header); a hand-edited overlay is left untouched and the
  suggestion goes to `deploy/presets.custom.jsonc.generated` to merge.
- **Why:** closes the embedding-dimension gap from the catalogue trim and makes
  a hosted-provider deploy self-contained, while preserving the operator's
  hand-edited overlay.

## 2026-06-22

> Platform (`bp_router`) surface of webapp SSO. The webapp/BFF halves
> (`/auth/sso/*`, the Settings SSO pane) live in `bp_agents` and aren't
> tracked here. Design: `docs/design/oidc-webapp.md`. Purely additive — off
> unless `ROUTER_OIDC_ENABLED=true`.

### Added — OIDC relying-party (webapp SSO)

- **What:** the router becomes an OIDC RP. New `user_oidc_identities`
  (`(issuer, sub)` PK → `user_id`, **migration 0007**; `OidcIdentityRow` +
  query layer) keeps SSO identities in a child table decoupled from
  `auth_kind`/`auth_secret_hash`, so one account can hold a password AND
  multiple linked OPs. `purge_user` erases them (PII).
- **What:** `bp_router/security/oidc.py` — `OidcProvider`: discovery (with
  issuer-match guard) + cached JWKS (refetch-on-kid-miss), PKCE-S256
  authorize URL, `client_secret_post` code exchange, and `id_token`
  validation (asymmetric algs only; `aud`/`iss`/`exp`/`iat` + nonce). Built
  once in the app lifespan (its httpx client closed on shutdown);
  `AppState.oidc_provider`.
- **What (endpoints, `api/auth.py`):** back-channel JSON APIs the BFF calls
  (it owns the browser redirects + transient state) — `POST
  /v1/auth/oidc/authorize` → `{authorize_url, state, nonce, code_verifier}`
  and `POST /v1/auth/oidc/exchange` → the normal `TokenPair`. Both
  unauthenticated but safe (valid OP code + PKCE verifier + allow-listed
  `redirect_uri` required), per-IP rate-limited (`BUCKET_OIDC`). Provisioning:
  `(issuer, sub)` login → gated auto-link by verified email (never onto
  admin/service) → JIT (group→level, allowed-groups gate, default level) or
  refuse in match-only mode. Optional `link_token` (a bot-minted `/password`
  token) attaches the identity to a pre-existing account (Telegram interop).
  Authenticated management: `GET`/`DELETE /v1/auth/oidc/identities` (list /
  unlink, last-method guard) and `GET /v1/auth/oidc/logout-url`
  (RP-initiated logout).
- **What (settings):** `OIDC_*` block (enabled, issuer, client_id,
  client_secret as a secret-ref-capable `SecretStr`, scopes, redirect-URI
  allowlist, JIT/group/allowed-groups policy, auto-link escape hatch, cache/
  timeout). `model_validator` fails fast when enabled-but-incomplete, requires
  an https issuer + a redirect allowlist, and rejects invalid levels.
- **Why:** humans wanted SSO (Authelia/Keycloak/Google/MS) for the webapp.
  Keeping the router as the identity authority means it issues the SAME
  first-party `TokenPair` after validation, so refresh and every downstream
  consumer (ACL, agents, sessions, cron) are unchanged; only the front-door
  authentication is new. `auth_kind="oidc"` and the OIDC-refusing
  `reset-password` path already anticipated this.

---

## 2026-06-20

> Platform (`bp_router`) surface of suite work whose channel halves live in
> `bp_agents` (the webapp `/register` page, the bot `/link` change, the
> Settings link-code UI — not tracked here). This adds a browser-side
> self-service registration path alongside the channel-submitted one, and
> moves `serviced_by` acquisition to channel-link time.

### Added — public self-service registration (`POST /v1/registrations/public`)

- **What:** new **unauthenticated** route in `api/registrations.py`. An
  anonymous browser visitor submits `{email, password, display_name?}`; the
  router stores the **chosen password as an argon2 hash** on the pending row
  (new `pending_user_registrations.requested_password_hash`, **migration
  0006**; `PendingRegistrationRow` + `upsert_pending_registration` carry it)
  and records **no** `submitted_by_service_user_id`. `channel` is forced to
  `webapp` and `external_id` to the lower-cased email, so `UNIQUE(channel,
  external_id)` makes a re-submit idempotent (bumps `attempts`, lets the user
  correct the password). Rate-limited per-IP (new
  `registration_web_rate_limit_per_ip_*`, bucket `BUCKET_REGISTRATION_WEB`)
  **before** the argon2 hash, plus the existing per-`(channel, external_id)`
  bucket. Audited `registration.submitted` with `actor_id=None` + `self_service`.
- **What (approval):** `approve_registration` (`api/admin.py`) now seeds the
  new user's `auth_secret_hash` from `requested_password_hash` when present
  (admin `initial_password` override still wins; random fallback otherwise).
  `ApproveRegistrationResponse.initial_password` is now **nullable** — `null`
  for a web signup, since the user already knows their password.
- **Why:** there is no email-delivery channel to send a reset link to, so the
  user picks a password at signup and the hash rides the pending row → they can
  sign in the instant an admin approves. Because there's no service submitter,
  approval grants **no** `serviced_by` (the webapp authenticates as the user
  and needs no per-user minting). Enumeration-safe: a duplicate email returns
  the same `201 pending`.

### Added — self-service link tokens + serviced-on-link (`/v1/auth/link-*`)

- **What:** `POST /v1/auth/link-tokens` (`require_authenticated`) mints a
  single-use token **for the caller's own account** (reuses the
  `password_reset_tokens` table; new `link_token_ttl_s` +
  `link_token_mint_rate_limit_per_user_*`, bucket `BUCKET_LINK_TOKEN_MINT`),
  audited `auth.link_token_minted`.
- **What:** `POST /v1/auth/link-channel` (`require_service`) consumes a link
  token, returns `{user_id}`, **and (by default) appends the calling service
  principal to that user's `serviced_by`** (`append_to_serviced_by`). The
  request carries `grant_service: bool = True`; with the default it grants,
  refusing (403) over an admin/service target — same `_PRIVILEGED_LEVELS`
  escalation guard as the F8/F9 mints (the guard sits inside the grant branch,
  since a verify-only bind confers no power). `grant_service=false` is the
  verify-only mode: consume + return `user_id`, no grant, no privileged guard.
  Inactive user → 409; bad token → 401. Audited `auth.channel_linked`
  (`grant_requested` + `serviced_by_granted`; `user.serviced_by_grant_denied`
  on the privileged refusal).
- **Why:** a web-first account has no chat channel and so no service principal
  that can mint it a reset token (the channel-anchored `/password` flow is
  service-gated) — `link-tokens` bootstraps the first link, authorised by the
  user's own session. `link-channel` then lets the bot's `/link` acquire
  `serviced_by` at link time, gated on a single-use token the user
  deliberately generated and pasted in, instead of an admin round-trip. This
  is the link-time analogue of the registration-approval auto-grant.

### Removed — `verify-reset-token` (folded into `link-channel`)

- **What:** deleted `POST /v1/auth/verify-reset-token` (added 2026-06-02 for
  the suite's `/link`) and its request/response models. Its verify-only
  behaviour is now `link-channel` with `grant_service=false`, so the duplicate
  route is gone; the shared single-use machinery it used
  (`consume_password_reset_token`, the `BUCKET_RESET_PASSWORD` bucket) stays
  (still used by `reset-password`). Suite-side, the dead
  `credentials.verify_link_token` wrapper was removed too.
- **Why:** with `link-channel` superseding it (the suite `/link` moved over,
  and a linked chat is inert without `serviced_by` so a bind-without-grant has
  no real flow), the endpoint had no live caller. It was a suite-added route,
  not upstream Backplaned, so removing it reverts our own addition rather than
  diverging from the vendored platform. Verify-only stays addressable via the
  `grant_service` flag, so no capability is lost.

---

## 2026-06-06

> Platform (`bp_router` / `bp_admin`) surface of suite work whose executor
> halves live in `bp_agents` (not tracked here): the closed-session /
> permanent-user GC reapers and the admin metrics panel.

### Added — permanent user purge (GDPR erasure)

- **What:** `DELETE /v1/admin/users/{id}` gains `?purge=true`, routing to new
  `queries.purge_user`: the `soft_delete_user` cascade + hard-delete of all
  router-side content (every session via `purge_session`, all `file_names`,
  forced file expiry), a PII scrub (`users.email` / `auth_secret_hash` → NULL),
  and a `users.purged_at` stamp (**migration 0005**; `UserRow` + `UserView`
  carry the field). The row is kept as a tombstone — 8 `ON UPDATE CASCADE` FKs
  + the append-only audit chain forbid a clean `DELETE` — and the purge is
  audited as `user.purged`, retaining `user_id`. Idempotent (re-purge is a
  no-op). New `POST /v1/admin/users/filter-purged` (`require_service`) returns
  which of a batch of user_ids are purged.
- **What (`bp_admin`):** the user detail page gains a danger-zone
  "Permanently erase user…" action with a type-`ERASE`-to-confirm guard (shown
  only when not already purged) and a "purged" badge.
- **Why:** right-to-erasure. The suite store + per-user LanceDB are erased by a
  suite-side reconcile loop keyed off `purged_at` (suite code, not tracked here)
  — so the router holds the marker + the read-only `filter-purged` probe, never
  a cross-store delete from the suite.

### Added — closed-session GC + session-existence probe

- **What:** `tasks.session_gc_loop` also hard-deletes sessions closed past
  `closed_session_retention_days` (new `Settings` field, default 90) with no
  live tasks, one transaction per session via `purge_session` (new
  `_gc_closed_sessions`; audited `session.purged`). New
  `POST /v1/admin/sessions/filter-existing` (`require_service`) returns which
  session_ids still exist.
- **Why:** bound the unbounded growth of closed sessions, and let the suite
  reconcile its own per-session rows for sessions the router has already
  removed — without holding any cross-user delete authority on the router.

### Added — LLM upstream-error metric + admin metrics panel

- **What:** new `router_llm_errors_total{provider, error_code}` counter,
  incremented at the LLM failure boundary (`llm/service.py`) on every failed
  adapter call (unary `_call_with_fallback` + stream-setup). New
  `observability/metrics.snapshot_summary()` (curated JSON read of the
  in-process registry) behind `GET /v1/admin/metrics/summary` (`require_admin`).
  `bp_admin`'s dashboard renders an auto-refreshing "Router metrics" panel
  (LLM errors, chain-exhaustion, calls, tokens, active tasks, redis health).
- **Why:** `router_llm_calls_total` only counts successful responses, so
  upstream errors were only inferable from the fallback counters; this names
  them directly and surfaces them at a glance. (`router_llm_cost_microusd_total`
  is left as plumbing — no adapter populates `cost_microusd`, so the dashboard
  deliberately has no cost card.)

### Changed — quiet routine admin polls in the access log

- **What:** `/v1/admin/mcp-servers` and `/v1/admin/metrics` are added to the
  default `access_log_quiet_paths` (`Settings`), so the admin UI's ~30s polls'
  successful GETs are dropped by `_AccessLogQuietFilter` (errors still log;
  prefix-matched, so the per-server detail GET and `/metrics/summary` are
  covered).
- **Why:** stop routine poll traffic flooding `uvicorn.access`.

---

## 2026-06-05

> The MCP bridge runtime itself ships as a NEW package (`bp_mcp_bridge`) and is
> not tracked here. These are the **platform** (`bp_router` / `bp_admin`)
> modifications the bridge required.

### Added — `service_mcp` bridge identity + admin-minted MCP onboarding

- **What:** the router seeds a fixed `service_mcp` (`level=service`) principal
  for the MCP bridge. `ROUTER_MCP_BRIDGE_SECRET` (new `Settings` field) is armed
  as its refresh token on every startup by `app._bootstrap_mcp_bridge_user`
  (idempotent, recovery-safe), so the bridge authenticates via the normal
  service-token refresh — no admin JWT. New `queries.arm_refresh_token` (upsert
  that resets a fixed-hash token to unused), `principals.MCP_BRIDGE_USER_ID`,
  and two guards in `security/jwt.py`: `require_mcp_bridge` (exact id+level) and
  `require_admin_or_mcp_bridge`.
- **What:** the MCP server endpoints in `api/admin.py` are re-gated — reads are
  admin-OR-bridge, `tools-refreshed` is bridge-only, the rest stay admin. The
  bridge **cannot mint invitations**; instead `create` / `refresh-tools` mint a
  short-TTL `service` invitation and stash it on the row (new
  `mcp_servers.pending_invitation_token` / `_expires_at`, **migration 0002**),
  which the bridge consumes to onboard `mcp_<server>` and the router clears on
  `tools-refreshed`.
- **Why:** make the bridge runnable as a long-lived daemon without holding a
  15-minute admin JWT, and keep invitation-minting (the crown-jewel capability)
  off the standing service credential.

### Added — per-server capabilities + per-tool disable on MCP servers

- **What:** `mcp_servers` gains `capabilities` and `disabled_tools` (**migration
  0003**); `McpServerCreate/Update` accept + validate them (capabilities against
  the dotted `CAPABILITY_PATTERN`), `_mcp_row_to_view` and the queries carry
  them. The admin UI (`bp_admin/pages/mcp_servers.py` + templates) adds a
  Capabilities input and a per-tool enable/disable checkbox grid, plus the
  **Reconnect** action and corrected "one agent per server, one mode per tool"
  wording.
- **Why:** agent-granular ACL targeting (capabilities, like `groups`) and an
  on/off toggle per tool. (Per-*tool* tier control is intentionally not added —
  the ACL has no mode dimension, so a capability gates the whole agent.)

### Added — stdio MCP transport config

- **What:** `mcp_servers.transport` gains `stdio`; `url` becomes nullable and
  `command` / `args` / `env_refs` (jsonb `ENV_NAME → env://|secret://`) are
  added, with a transport-fields CHECK keeping the url and stdio shapes disjoint
  (**migration 0004**). `McpServerCreate/Update` gain
  `_check_transport_consistency` + an `env_refs` validator; new
  `ROUTER_MCP_ALLOWED_LAUNCHERS` setting (default `["uvx"]`) enforced by
  `api/admin.py::_check_mcp_launcher`. The admin form reveals stdio fields per
  transport.
- **Why:** let the bridge run local `uvx <server>` MCP servers (validated +
  launcher-allowlisted at the boundary; the bridge re-checks and sandboxes at
  spawn).

### Fixed — stale MCP admin-UI / docstring messaging

- **What:** corrected the `bp_admin` MCP pages + `bp_router/api/admin.py`
  docstrings that claimed the bridge "ships separately / isn't built" and
  described "per-tool agents" — now: one agent per server, one mode per tool,
  consumed by the `bp_mcp_bridge` runtime.

---

## 2026-06-03

### Added — downscale inlined LLM images (longer-side pixel cap)

- **What:** the router now downscales an image before base64-inlining it into a
  provider request, so its longer side is at most
  `ROUTER_LLM_IMAGE_MAX_LONG_SIDE_PX` (new setting,
  `Settings.llm_image_max_long_side_px`, **default 1568**; `0` disables).
  Aspect ratio is preserved and images are only ever shrunk, in
  `bp_router/llm/attachments.py:_downscale_image` — the single choke point all
  provider adapters consume, so it applies to Anthropic / Gemini / OpenAI
  alike. Best-effort: an undecodable image is fed as-is. Adds a `pillow`
  dependency to the `router` extra.
- **Over-cap rescue:** when resizing is on, an image OVER
  `llm_attachment_inline_max_bytes` is now loaded up to a new
  `ROUTER_LLM_IMAGE_RESCALE_SOURCE_MAX_BYTES` bound (default 20 MiB),
  downscaled, then re-checked against the inline cap on the *resized* result —
  so a large image that fits once shrunk is fed instead of refused (it's
  refused only if it's still over-cap after downscaling, or can't be decoded).
  Documents and resize-disabled images obey the inline cap directly, as before.
- **Why:** multimodal token cost is dimension-based, so a large image burned a
  lot of tokens (and ate headroom under `llm_attachment_inline_max_bytes`).
  1568 px matches Anthropic's own internal long-edge downscale, so the default
  trims tokens with effectively no quality loss; operators can lower it to
  trade detail for cost or set `0` to keep full resolution.
- **Behaviour change:** images with a longer side > 1568 px are now resized by
  default (previously inlined at full resolution). Set
  `ROUTER_LLM_IMAGE_MAX_LONG_SIDE_PX=0` to restore the old behaviour.

### Changed — `read_file` tool description (precise + provider-agnostic)

- **What:** reworded the `read_file` tool description in `bp_sdk/file_tools.py`.
  Dropped the under-the-hood claim that content "is attached on the next turn —
  you do not receive raw bytes here"; it now reads "Show a stash file's content
  so you can read it … text, images, and documents are all supported."
- **Why:** the old wording leaked dispatch internals and was inaccurate for the
  Anthropic and Gemini adapters, which feed image bytes back **in the tool
  result** (same turn), not on a following turn. The description is what the LLM
  reads, so it should describe the effect (you get to see the file), not the
  transport. Behaviour of the tool itself is unchanged.

---

## 2026-06-02

### Added — verify-only password-reset endpoint for channel linking

- **What:** new public route `POST /v1/auth/verify-reset-token` (auth.py).
  It **consumes** a password-reset token (single-use, via the existing
  `consume_password_reset_token`) and returns `{user_id}` **without**
  setting a password or issuing a session — the verify-only sibling of
  `POST /v1/auth/reset-password`. Like reset-password the token IS the auth
  (no Bearer header), and it reuses the **same** per-IP rate-limit bucket
  (`BUCKET_RESET_PASSWORD`, `password_reset_consume_rate_limit_per_ip_*`) so
  the two consumption paths share one enumeration budget. Returns 401 on a
  missing/expired/already-used token and 409 if the user is inactive.
  Audited as `auth.password_reset_token_verified` (payload
  `{"purpose": "link"}`); rejects reuse the existing
  `auth.password_reset_token_invalid` event.
- **Why:** the agent suite needed a way to attach a **new** channel chat
  (e.g. KakaoTalk) to a user's **pre-existing** account. The suite's
  `/link <token>` command verifies a token the user minted on a channel
  they're already on (`/password`), proving ownership, then binds the chat
  to the returned `user_id`. Consuming on verify (rather than a non-
  destructive peek) means a leaked token can't be replayed to hijack a
  link. No password is touched, so this is strictly less powerful than the
  already-public reset-password path. Backward-compatible: purely additive.

---

## 2026-05-29

### Changed — eviction frees the agent_id for reuse (tombstone rename)

- **What:** `POST /v1/admin/agents/{id}/evict` now, after marking the agent
  `removed` and failing its in-flight tasks, **renames the row's PK to a
  tombstone** (`deleted_<id>_<epoch>`) and renames the co-located service
  principal (`usr_service_<id>`) the same way — so the original `agent_id`
  (and a channel agent's service-user id) is freed for a brand-new agent to
  onboard. History is preserved: the consolidated `0001_initial_schema`
  baseline declares `ON UPDATE CASCADE` on all 15 FKs referencing
  `agents(agent_id)` / `users(user_id)`, so dependent `tasks` rows follow
  the rename instead of
  blocking it. New query `rename_evicted_agent` / helper `tombstone_agent_id`
  (CHECK/64-char-safe). Audited as `agent.id_released`. Endpoint response
  gains `tombstone_agent_id` + `id_released`.
- **Why:** previously a `removed` row squatted on the `agent_id` forever (PK
  uniqueness + onboard's `≠ pending` 409), so the only way to reuse an id was
  manual SQL. Reuse still requires a fresh admin invitation, so it stays
  deliberate and audited — never silent.
- **Shape:** **Changed** — `agents.status` enum and the soft-delete (row
  preserved) are unchanged; the row's *id* is now tombstoned rather than left
  on the live name. Migration is constraint-redefinition only (no data
  change). The `reset`/`reprovision`/`unsuspend` "refuse removed" guards are
  unaffected (a tombstone is queried by its new id).


### Added — one-click agent reprovision (admin webUI + router endpoint)

- **What:** a **Reset & reprovision** button on the admin agent-detail page
  (`bp_admin`) and a new `POST /v1/admin/agents/{id}/reprovision` endpoint
  (`bp_router`). It atomically resets the agent to `pending`, mints a fresh
  invitation (7-day TTL), drops the live socket + fails in-flight tasks, and
  reveals the one-time token so the operator can restart the agent with it.
  `provisions_service_user` is **auto-detected** from whether the agent's
  co-located service principal (`usr_service_{id}`) exists, so a channel
  agent's service refresh token is re-minted on re-onboard. Refuses `removed`
  (terminal).
- **Why:** recovers an agent that can't reconnect on its own — e.g. its agent
  JWT expired after >24h downtime, or its state dir was wiped — without hand-
  running SQL + the invitation-mint flow. The button is offered for
  active/suspended/pending agents.
- **Shape:** **Added** — new endpoint + BFF route + `reprovisioned.html`
  reveal template; reuses `reset_agent_to_pending` / `insert_invitation`.
  Audited as `agent.reprovision`.


### Added — generic `lite` / `pro` tier-slot presets in the catalogue

- **What:** added two presets to `bp_router/llm/presets_catalog.jsonc` —
  `lite` (→ `gemini-3.1-flash-lite`) and `pro` (→ `gemini-3.1-pro-preview`).
  Together with the existing `default`, these are stable, generic tier-slot
  names (lite / default / pro) intended to be repointed to any provider/model
  via the admin webUI. They back the prod-init "Custom" provider option, which
  wires `SUITE_DEFAULT_PRESET_{LITE,BALANCED,PRO}` to `lite` / `default` / `pro`.
- **Why:** let an operator stand up a deployment whose tier defaults are stable
  names and configure the actual models/keys later in the admin UI.
- **Shape:** **Added** — seed-data only; catalogue-pinning tests updated.

### Changed — refresh the LLM preset catalogue (opus 4-8, new tier aliases)

- **What:** updated `bp_router/llm/presets_catalog.jsonc`: Claude Opus bumped
  to `claude-opus-4-8` (the `claude-opus` alias now points there, and the
  version-pinned preset is renamed `claude-opus-4-7` → `claude-opus-4-8`).
  Added four friendly tier aliases — `gemini-lite` (→ `gemini-3.1-flash-lite`),
  `gemini-pro` (→ `gemini-3.1-pro-preview`), `gpt-nano` (→ `gpt-5.4-nano`),
  `gpt-pro` (→ `gpt-5.5-pro`).
- **Why:** keep the seed catalogue current as models change; provide stable,
  human-friendly alias names that survive version churn.
- **Shape:** **Changed** — seed-data only (affects fresh seeds / the in-memory
  fallback; already-seeded DBs are admin-managed). Catalogue-pinning tests and
  the preset reference docs were updated to match.

### Changed — `bp_router` preset seed catalogue moved to a commentable JSONC file

- **What:** the built-in LLM preset list that `default_presets()` returns (the
  first-boot seed for `llm_presets` and the in-memory fallback) moved from a
  hardcoded Python list into `bp_router/llm/presets_catalog.jsonc`. Added a
  string-aware JSONC reader (`strip_jsonc_comments` / `load_catalog`) and a
  `Settings.llm_preset_catalog_path` (`ROUTER_LLM_PRESET_CATALOG_PATH`) so a
  deployment can point at its own catalogue outside the package. `.jsonc` is
  added to the wheel artifacts.
- **Why:** models change frequently; a commentable, separately-editable file
  is far easier to maintain than an inline dataclass list, and an env-pointable
  path lets operators keep their model list out of source.
- **Shape:** **Changed** — `default_presets()` keeps the same signature and
  return value (now `default_presets(path=None)`); the bundled catalogue
  reproduces the prior list exactly, so seeding/back-compat is unchanged. A
  malformed catalogue (bad JSON, unknown key, missing required field) fails
  loud at load. Comment stripping preserves `://` inside string values and
  newlines (for accurate parse-error line numbers); trailing commas are not
  supported.

### Changed — raise default task/result timeouts for long agent turns

- **What:** bumped two vendored-platform defaults to fit long multi-round
  turns (e.g. research with several web fetches):
  - `bp_sdk` `AgentConfig.pending_results_timeout_s` 60.0 → **480.0**
  - `bp_router` `RouterSettings.default_task_deadline_s` 300 → **900**
- **Why:** the channel waits on an injected turn's result
  (`dispatch_result_timeout_s`, now 600 in `bp_agents`), and a single turn
  can run multiple `web_fetch_timeout_s` (now 120) fetches across up to
  `max_rounds` LLM rounds. The old 300s router deadline / 60s SDK result
  wait gave up while work was still in flight, surfacing a spurious failure
  to the user. New ordering: SDK result wait (480) < channel dispatch (600)
  < router deadline (900), so the router keeps the task alive past the
  channel's give-up point.
- **Shape:** **Changed** — default-value only; both remain env-overridable.
  Suite-side companions (`bp_agents`, not tracked here):
  `dispatch_result_timeout_s` 180 → 600, `plan_step_timeout_s` 120 → 240,
  `web_fetch_timeout_s` 150 → 120.

### Fixed — `bp_sdk` dispatch buffers/floods progress for wait-only spawns

- **What:** `Dispatcher._handle_progress` now **drops** a ProgressFrame when
  there's no progress subscriber **but a Result is already pending** for that
  `task_id` — i.e. a wait-only `peers.spawn(stream=False)` (the subagent
  tool-call path, `spawn_from_tool_call`). Previously such frames were
  buffered in `_pending_progress_buffer` up to the per-task cap, so a chatty
  subagent (e.g. `research` running web search) flooded
  `progress_buffer_per_task_cap` warnings and churned the buffer for frames
  no one would ever drain. Added `PendingMap.__contains__` to make the
  "Result pending?" check clean.
- **Why:** a `stream=False` spawn explicitly opts out of progress; the
  router still fans progress to the caller (lineage), so the SDK is the right
  place to discard it. The pre-subscribe buffer is retained for the
  **streamed**-spawn race (`subscribe_progress` lands just after the ack).
- **Shape:** **Fixed** — no API change; behaviour change is "drop instead of
  buffer+warn" for progress the caller didn't subscribe to. Streamed spawns,
  the channel's `open_spawn_stream` root, and any manual `subscribe_progress`
  are unaffected (they have a subscriber → delivered).
- **Verified:** `tests/test_review_progress_buffer_before_subscribe.py` —
  wait-only (pending Result, no sub) → dropped; no-sub + no-pending-Result →
  still buffered (race); `PendingMap.__contains__` round-trip.

### Added — agent reset to `pending` (`POST /v1/admin/agents/{id}/reset`)

- **What:** A new admin endpoint moves a registered agent (`active` /
  `suspended`) back to `pending` so it can **re-onboard** with a fresh
  invitation (`bp_router/api/admin.py::reset_agent` +
  `queries.reset_agent_to_pending`). Idempotent on `pending`; refuses
  `removed` (eviction stays terminal); force-closes any live socket + fails
  in-flight tasks; audits `agent.reset`.
- **Why:** an agent whose persisted credentials are lost (e.g. an ephemeral
  state dir wiped on reboot) is *registered* but can't resume, and a fresh
  `POST /v1/onboard` returns `409 already registered` — previously
  unrecoverable without a full router DB reset (there's no de-register path;
  `evict` is terminal). Reset re-opens onboarding's existing `pending` path
  (keeps the row, re-mints a service principal's refresh token, issues a
  fresh agent JWT).
- **Shape:** **Added** — new admin surface; no change to existing flows. The
  `agent_id` is **not** freed for arbitrary reuse: re-onboard still requires
  an admin-issued invitation, and `removed` agents remain irreversibly
  retired. The `agents.status` enum (`active|suspended|pending|removed`) is
  unchanged — no migration.
- **Verified:** `tests/test_agent_reset.py` — query transitions against the
  live schema (active/suspended → pending; pending/removed untouched) + the
  endpoint contract (status guards, `agent.reset` audit, in-flight fail).

### Added — session reopen (`POST /v1/sessions/{id}/reopen`)

- **What:** A new router endpoint clears `closed_at` so a previously closed
  session re-admits task injection (`bp_router/api/sessions.py::reopen_session`
  + `queries.Scope.reopen_session`). Returns the `SessionView`, emits a
  `session.reopened` audit event, is **idempotent** on an already-open session
  (no-op, no audit), and 404s a session that isn't the caller's.
- **Why:** the webapp's "Reopen" action (shown on closed rows in place of
  "Close") needs to resume an archived conversation. `admit_task` already
  gates on `closed_at IS NULL`, so clearing it is the whole mechanism.
- **Shape:** **Added** — new surface; existing behavior unchanged. History,
  metadata, and the suite `session_info` row are retained on close, so reopen
  restores nothing suite-side. Cancelled tasks and the close-time file-name GC
  are **not** restored (close is still destructive for in-flight work). The
  `Scope.reopen_session` query is conditional (`closed_at IS NOT NULL`) and
  user-scoped, returning whether a closed row was actually transitioned.
- **Verified:** `tests/test_session_reopen.py` — Scope round-trip
  (open → close → reopen → idempotent → cross-user-denied) against live
  Postgres, plus webapp handler/template behaviour (resume-into-chat redirect,
  404 on unowned, button toggle).

### Changed — ruff lint cleanup across vendored platform code

- **What:** Brought the repo to a clean `ruff check` (config: `E,F,I,B,UP,
  PLC,PLE,PLW`). The bulk was non-platform (ruff config + test hygiene);
  the **platform-code** touches are all lint-only, behaviour-preserving:
  - **Removed** unused imports (F401): `bp_router/acl.py`,
    `bp_router/llm/presets.py` (`level_satisfies_tier`, `tier_index`),
    `bp_sdk/peers.py` (`AgentInfoUpdateFrame`).
  - **Removed** unused locals (F841): `task_user_id` in
    `bp_router/tasks.py`; the unused `exc` binding on two blind-`except`
    clauses (`bp_router/dispatch.py`, `bp_sdk/dispatch.py`) — the bodies
    never referenced it.
  - **Style:** split `;`-joined statements (E702) in
    `bp_router/db/queries.py::update_mcp_server`; `raise … from None` on the
    409 in `bp_router/api/admin.py::issue_invitation` (B904); renamed a
    shadowing loop var in `bp_admin/pages/mcp_servers.py` (PLW2901);
    `collections.abc.Iterator` import in `bp_router/lru_cache.py` (UP035);
    hoisted drifted mid-file imports to the top of
    `bp_mcp_bridge/mcp_client.py` (E402).
  - **Suppressed (not rewritten):** intentional lazy/deferred imports kept
    as-is with `# noqa: PLC0415` (`bp_router/__main__.py`, `bp_sdk/llm.py`,
    `bp_admin/app.py`, `bp_admin/auth.py`) and a documented `# noqa: E402`
    (`bp_admin/pages/llm_presets.py`); the deliberate `setattr(task,
    "_bp_task_id", …)` on the C-level asyncio.Task kept with `# noqa: B010`
    (direct assignment trips mypy and breaks the cancel-helper source pin).
- **Config (project-level, not platform):** added
  `flake8-bugbear.extend-immutable-calls` for FastAPI's
  `Depends`/`Query`/`Form`/… (B008 is a false positive on every route) and
  `per-file-ignores` for `tests/**` (`PLC0415`, `B017`, `E741`, `B011` —
  idiomatic in tests). See `pyproject.toml`.
- **Why:** keep the lint gate green and the platform diff explicit.
- **Shape:** **Changed** — cosmetic/hygiene only; no API or behaviour
  change (verified: full suite still 2596 passed, 0 failed).

---

## 2026-05-28

### Added — session hard-delete (`DELETE /v1/sessions/{id}?purge=true`)

- **What:** `DELETE /v1/sessions/{id}` gains a `purge` query param. `false`
  (default) is the existing soft **close**; `true` closes-then-**hard-deletes**
  the session and its router-side data (`bp_router/api/sessions.py`,
  `queries.Scope.purge_session`). Refactored the close body into a shared
  `_close_session` helper used by both paths.
- **Why:** the webapp's "remove session" needs a true delete; only soft close
  existed. This is the **only router-side change** the webapp requires.
- **Shape:** **Added** — default behavior unchanged. The purge deletes in FK
  order (`task_events` → file-name directory → `tasks` → `sessions`) inside
  one transaction, and **detaches** `files` rows (`session_id`/`task_id` →
  NULL) rather than deleting them — they're content-addressed, dedup'd per
  `(user, sha256)`, and refcounted by `file_names`, so the reclaim sweep frees
  the blob once unreferenced (same contract as close; a `persist/` name
  sharing the row is preserved). Audits `session.purged`. Suite-side data
  (`bp_suite` `session_history` / `session_info` / `cron_jobs`) is the
  webapp's responsibility to clean — out of router scope.
- **Verified:** `tests/test_session_purge.py` — a real-DB cascade test
  (seeds user→agent→session→task→event→file→file_name; asserts the session +
  dependents are gone, the dedup'd `files` row detached, a `persist/` name
  survives) + source-inspection guards; existing close-GC tests repointed at
  the extracted helper.

### Fixed — broadcast a CatalogUpdate when a handshake refreshes agent info

- **What:** When `_handshake` refreshes a reconnecting agent's published
  info (the prior fix), it now also drops the short-TTL `_CatalogCache`
  (new `clear()`) and calls `push_catalog_update_to_all` — but only when the
  info actually changed (`bp_router/ws_hub.py`).
- **Why:** the handshake refresh updated the DB, but already-connected peers
  hold their catalog from the *last* Welcome and only refresh on a
  `CatalogUpdate` (or their own reconnect). So an agent that gained a
  tool-visible mode (e.g. config's `cron` → `call_config_cron`) stayed
  invisible to the orchestrator's live `peers.visible()` until it
  reconnected. `admit` reads the DB fresh, but tool *visibility* is
  catalog-driven — hence the broadcast.
- **Shape:** **Fixed.** Bounded: the broadcast/clear fire only on an actual
  change (write-on-change refresh), so a normal no-op reconnect — or a fleet
  restart with unchanged code — triggers neither. Best-effort: a broadcast
  failure logs and never fails the handshake.
- **Verified:** `tests/test_handshake_agent_info_refresh.py` (cache `clear()`
  + source guard that `_handshake` broadcasts on change); handshake +
  agent-info suites green.

### Changed — drop the `[capabilities: …]` suffix from tool descriptions

- **What:** `build_tools` (`bp_sdk/tools.py::_description`) no longer appends
  `" [capabilities: …]"` to a tool's description; it emits the per-mode (or
  agent-level) description verbatim.
- **Why:** capabilities are ACL/catalog metadata; echoing them into the
  tool description the model reads is redundant and sometimes misleading
  (capability names like `assistant.rag` aren't usage guidance). Per-mode
  descriptions now carry the actual "what this tool does" text.
- **Shape:** **Behavior change** to generated tool schemas (description
  text only — names/params unchanged). Catalog/admin still expose
  `capabilities` as a structured field.
- **Verified:** `tests/test_per_mode_tool_descriptions.py` asserts verbatim
  descriptions + no suffix; tool/suite suites green.

### Added — per-mode tool descriptions (`AgentInfo.mode_descriptions`)

- **What:** A new optional `AgentInfo.mode_descriptions: dict[str, str]`
  (`bp_protocol`), a `description=` kwarg on `@agent.handler` that publishes
  it (`bp_sdk/agent.py::_republish_schemas`), and `build_tools`
  (`bp_sdk/tools.py`) now prefers the per-mode description over the
  agent-level `description` for each `call_<agent>_<mode>` tool (falling back
  when a mode has none). Threaded through the router: the catalog projection
  (`visibility.available_destinations`) carries it, it's a mutable field on
  `AgentInfoUpdateFrame` + `_AGENT_INFO_MUTABLE_FIELDS` (so edits propagate
  via handshake-refresh / AgentInfoUpdate).
- **Why:** a multi-mode agent's modes each become a distinct tool
  (`call_knowledge_base_store` / `_retrieve` / `_remove`, …) but all shared
  the single agent-level `description`. Per-mode descriptions let the calling
  LLM tell them apart. (`AgentInfo.description` is the agent-level fallback,
  used for single-tool-mode agents and the admin catalog.)
- **Shape:** **Added** — `None` default reproduces the previous
  single-description behaviour; no agent need set it.
- **Verified:** `tests/test_per_mode_tool_descriptions.py` (publish on
  `description=`, `None` when absent, per-mode wins + fallback in
  `build_tools`); `test_phase10e` lockstep guards updated for the new mutable
  field; tool/agent-info/handshake suites green.

### Fixed — refresh a reconnecting agent's AgentInfo on handshake

- **What:** `_handshake` (`bp_router/ws_hub.py`) now re-publishes the
  reconnecting agent's `agent_info` from its `HelloFrame` — merging the
  same self-mutable fields as `AgentInfoUpdate`
  (`_AGENT_INFO_MUTABLE_FIELDS`: `accepts_schema`, `non_tool_modes`,
  `capabilities`, `groups`, `description`, …), `agent_id` pinned to the
  stored record, fully re-validated, and persisted (incl. the denormalised
  `groups`/`capabilities` columns) **only when something changed**.
- **Why:** onboarding was the *only* writer of `agent_info`, so a code
  change that added/changed an agent's modes (e.g. the config agent
  gaining a `cron` mode) never reached the router — `admit_task` validated
  `input_mode` against the stale `accepts_schema` and rejected the new mode
  (`unknown input_mode 'cron'; destination modes: ['message']`). The SDK
  already documents that `run_async()` "publishes the up-to-date snapshot
  on its initial handshake" (`bp_sdk/agent.py`); the router simply wasn't
  honoring it.
- **Shape:** **Fixed.** Mode/capability changes now take effect on the
  agent's next reconnect — no re-onboarding. Consistent with the existing
  `AgentInfoUpdate` trust model (agents already self-declare these fields).
  No full catalog re-broadcast on the hot handshake path — `admit_task`
  reads the DB fresh, and peer-tool visibility refreshes via the existing
  ~5s catalog cache. Manual escape hatch (clear creds → re-onboard) is no
  longer needed.
- **Verified:** `tests/test_handshake_agent_info_refresh.py` (refresh on
  added mode / no-op when unchanged / `agent_id` locked / invalid merge
  raises). Existing handshake + agent-info-update suites green (74 passed).

### Added — access-log quiet filter for routine poll/health endpoints

- **What:** A `Settings.access_log_quiet_paths` knob (default
  `["/healthz", "/metrics", "/v1/admin/serviced-sessions"]`) plus an
  `_AccessLogQuietFilter` attached to the `uvicorn.access` logger in
  `configure_logging` (`bp_router/observability/logging.py`,
  `bp_router/settings.py`). It drops **successful (`<400`) GET** access
  lines whose path matches a configured prefix; errors and all other
  traffic still log.
- **Why:** the suite's chatbot polls `GET /v1/admin/serviced-sessions`
  every 30s for registration approvals, flooding `uvicorn.access` with
  200s. Health/metrics scrapes do the same. The filter removes the noise
  without losing genuine access logs.
- **Shape:** **Added** — opt-out by setting `access_log_quiet_paths=[]`
  (or `ROUTER_ACCESS_LOG_QUIET_PATHS`). Fails open: any record that isn't
  the expected uvicorn.access 5-tuple is kept, so a uvicorn change can't
  silently swallow logs.
- **Verified:** `tests/test_access_log_filter.py` (drop success / keep
  errors+non-GET+other paths / fail-open on foreign records).

## 2026-05-26

### Changed — local file-store default dir renamed (drop `proxyfiles` relic)

- **What:** The `LocalFileStore` default path (used when
  `ROUTER_FILE_STORE_OPTIONS` has no `path`) was renamed from `./proxyfiles`
  to `./router_files` (`bp_router/storage/local.py`); the `TestRouter`
  harness default likewise `./.test_proxyfiles` → `./.test_router_files`
  (`bp_sdk/testing.py`).
- **Why:** `proxyfiles` was vestigial naming from the predecessor
  `ProxyFile` file model, which the router-managed file store
  ([`docs/design/router-managed-file-store.md`](../design/router-managed-file-store.md))
  replaced. The dead name was confusing in `.env.example` and the code.
- **Shape:** **Behavior change (default only).** A `file_store=local`
  deployment that relied on the *implicit* default now reads/writes
  `./router_files` — existing files under `./proxyfiles` would appear
  missing until the dir is moved or `path` is set explicitly. Anyone who
  already set `ROUTER_FILE_STORE_OPTIONS.path` (incl. the prod compose,
  which uses S3) is unaffected. Acceptable pre-release (no back-compat).
- **Verified:** no test pinned `./proxyfiles`; suite + storage tests green.

### Added — `bp_router/llm`: embedding output-dimension via `provider_options`

- **What:** Plumbed an embedding vector-width control through the embed
  path. `ProviderAdapter.embed` (`providers/base.py`) gains a keyword
  `provider_options`; `LlmService.embed` forwards the resolved preset
  `provider_options` to it. `GeminiAdapter.embed` reads
  `output_dimensionality` (passed as a dict `config` to `embed_content`),
  and the OpenAI / OpenAI-compatible embeddings adapters read `dimensions`.
  The other adapters (chat OpenAI / Anthropic / openai-compatible chat)
  take the new kwarg but still raise (no embeddings surface). Seeded
  `default_embedding` + `gemini-embedding-2` carry
  `default_provider_options={"output_dimensionality": 1536}` to match the
  suite's `embedding_dim`.
- **Why:** Gemini's embedding models default to 3072-wide vectors; without
  requesting `output_dimensionality` they wouldn't fit the suite's
  1536-wide LanceDB column. The preset now pins the width, and the adapter
  honours it. (`provider_options` is the existing preset passthrough — the
  preset defaults flow through `_resolve` when call-time options are None,
  so no SDK call-signature change was needed.)
- **Shape:** **Additive** — `embed()` gains an optional keyword; existing
  callers/behaviour unchanged when it's absent. Crosses the embed adapters
  + service + the two embedding presets.
- **Verified:** `tests/test_llm_embed_dimensions.py` (Gemini sends the
  `output_dimensionality` config + width; OpenAI sends `dimensions`;
  `default_embedding` requests 1536); existing alias/preset tests updated
  for the refreshed lineup.

### Added — `bp_router/llm/presets.py`: `default_embedding` seed preset

- **What:** A canonical `default_embedding` seed preset → `provider="gemini"`,
  `concrete_model="gemini-embedding-2"`, mirroring `default` (chat) as the
  catch-all embedding preset.
- **Why:** Give the suite a real embeddings default to point
  `default_preset_embedding` at — `default` is a *chat* model and can't
  embed (the `embed()` path needs an embedding-capable model; see the
  provider split). Gemini serves both chat and embeddings through one
  adapter, so this rides `provider="gemini"`.
- **Shape:** **Additive data/seed change** (empty-table first-boot seed
  only). Name uses `_` (allowed by the `name` CHECK).
- **Verified:** `tests/test_llm_provider_options.py` resolves
  `default_embedding` → `("gemini", "gemini-embedding-2")`.

### Changed — `bp_router/llm/presets.py`: OpenAI lineup trim + nano tiers

- **What:** In `default_presets()` for the OpenAI families:
  - **dropped** `gpt-4o`, `o4-mini` (chat) and `text-embedding-ada-002`
    (embeddings);
  - **added** `gpt-5-4-nano` (`gpt-5.4-nano`) and `gpt-5-nano` (`gpt-5-nano`).
  - The `openai` / `gpt` bare aliases (→ `gpt-5.5`) and the remaining
    `gpt-5*` / `gpt-4-1` / `text-embedding-3-*` entries are unchanged.
- **Why:** Drop retired models and add the nano tiers to the seeded lineup.
- **Shape:** **Data/seed change** (empty-table first-boot seed only). No
  test pinned the dropped names, so no platform-test change was needed.

### Changed — `bp_router/llm/presets.py`: `claude` alias → Sonnet

- **What:** Repointed the bare `claude` seed preset from `claude-opus-4-7`
  to **`claude-sonnet-4-6`**, and updated its description ("General-purpose
  Claude (Sonnet). Open to all tiers."). The version-pinned `claude-opus*` /
  `claude-sonnet*` / `claude-haiku*` aliases are unchanged.
- **Why:** Make the catch-all `claude` alias resolve to the
  general-purpose Sonnet tier rather than top-cost Opus.
- **Shape:** **Data/seed change** (empty-table first-boot seed only).
- **Verified:** `tests/test_llm_anthropic_adapter.py` alias-resolution
  assertion updated (`claude` → sonnet); the rest unchanged.

### Changed — `bp_router/llm/presets.py`: Gemini default-preset lineup refresh

- **What:** Reworked the **Gemini** entries in `default_presets()`:
  - **dropped** `gemini-2-5`, `gemini-2-5-flash`, `gemini-3`;
  - **renamed** `gemini-3-flash` → **`gemini-3-5-flash`** (`concrete_model`
    `gemini-3-flash-preview` → `gemini-3.5-flash`);
  - **added** the bare `gemini` alias (`gemini-3.5-flash`),
    `gemini-3-1-flash-lite` (`gemini-3.1-flash-lite`),
    `gemini-3-1-pro` (`gemini-3.1-pro-preview`), and the embedding preset
    `gemini-embedding-2` (`gemini-embedding-2`);
  - **repointed** `default` from `gemini-2.5-flash` → **`gemini-3.5-flash`**.
- **Why:** Refresh the seeded model lineup to the current Gemini family the
  deployment targets. The embedding preset rides `provider="gemini"` (the
  adapter's `embed()` already uses `concrete_model`), so no new provider
  was needed. `gemini-2-5-pro` and the Anthropic/OpenAI families are
  unchanged.
- **Shape:** **Data/seed change** — only seeded into an *empty*
  `llm_presets` table on first boot; existing deployments are unaffected
  until they reseed. Preset NAMES keep the `-`-for-`.` slug form (DB CHECK);
  `concrete_model` keeps the dotted upstream id.
- **Verified:** `tests/test_llm_provider_options.py` (alias resolutions
  updated) and `tests/test_upstream_bugs_boot_blockers.py` (dotted-form
  spot-check) updated to the new lineup; preset suite green.

### Added — `bp_router`: `GET /v1/admin/serviced-sessions` (service-principal discovery)

- **What:** A new `require_service` endpoint
  (`bp_router/api/admin.py::serviced_sessions`) backed by
  `queries.list_serviced_sessions`, returning the sessions of users the
  **calling service principal** services — `{user_id, session_id,
  external_id, channel, opened_at}`, with `channel` + `since` filters.
- **Why:** The suite's manual-approval flow had no path for a
  **service-level** channel to learn its approved users. Admin approval
  creates the user (`serviced_by=[channel]`) and opens a session whose
  `metadata.external_id` is the channel-native id, then **deletes** the
  pending row and returns the result to the *admin*. But
  `GET /v1/admin/registrations` is `require_admin` (a channel can't call
  it), the only `require_service` endpoint was the token mint (needs a
  `user_id` the channel doesn't have yet), and there was no
  `external_id → user_id` resolution. So the channel could not populate
  `suite_platform_mappings` / `user_config` after approval. This endpoint
  closes that gap, matching the design's "use `serviced_by` rights"
  intent ([`agent-suite/channel.md` §2](../agent-suite/channel.md),
  [`agent-suite/overview.md` §2.1](../agent-suite/overview.md)).
- **Shape:** **Additive** + **security-scoped** — `require_service` plus a
  `$1 = ANY(u.serviced_by)` filter, so a principal sees only its own
  serviced users' sessions, never the whole table. No existing surface
  changed.
- **Verified:** `tests/test_serviced_sessions_discovery.py` — scoping
  (excludes un-serviced users), `channel` + `since` filters, `external_id`
  surfaced from session metadata.

### Added — `bp_sdk/agent.py`: B1 root-task injection helper

- **What:** Two new `Agent` methods — `spawn_root_for_user(dest, payload,
  *, user_id, session_id, mode, …) -> task_id` and
  `await_root_result(task_id, *, timeout_s, on_progress) -> ResultFrame`.
- **Why:** The suite's channel/gateway agent must inject a user turn as a
  **parentless** task carrying the *end user's* `(user_id, session_id)`
  over its own WS (suite prerequisite **B1** — [`agent-suite/channel.md`
  §4](../agent-suite/channel.md)). `peers.spawn` cannot do this: it is
  handler-bound and always inherits `parent_task_id = ctx.task_id`.
- **Shape:** Purely **additive** — no existing signature changed. Reuses
  existing tested machinery (the router's parentless-admit path, the
  `PendingMap` early-resolve buffer, and `dispatcher.open_spawn_stream`,
  the supported out-of-context entry point). **No router change was
  required** for B1.
- **Verified:** `tests/test_b1_root_task_injection.py` (parentless
  round-trip with progress fan-out; unknown-session → `SpawnRejected`).
- **Commit:** *Add B1 root-task injection SDK helper.*

### Fixed — `bp_router/db/migrations/env.py`: Alembic async runner never committed

- **What:** Added an explicit `await connection.commit()` after
  `connection.run_sync(do_run_migrations)` in `run_async_migrations`.
- **Symptom:** `alembic upgrade head` exited **0** and logged
  `Running upgrade -> 0001_initial_schema`, but **no DDL landed** and
  `alembic_version` was never created — a fresh router database stayed
  empty, so the router failed to boot (`relation "acl_rules" does not
  exist`).
- **Root cause:** Under **alembic 1.18 / SQLAlchemy 2.0.50 + asyncpg**, an
  `AsyncConnection` is commit-as-you-go and the `async with
  connectable.connect()` block rolls back on exit unless committed.
  Alembic's `begin_transaction()` runs on the sync facade and does not
  surface a commit to the outer async connection with this driver/version
  combo. (The widely-copied async Alembic template predates this 2.0
  behavior.)
- **Impact:** Without the fix, **no fresh deployment can migrate** on these
  library versions — a hard boot blocker, not suite-specific.
- **Verified:** `alembic upgrade head` against a fresh database now creates
  all 17 tables and stamps `alembic_version = 0001_initial_schema`.
- **Note:** The suite's own Alembic env (`bp_agents/migrations/env.py`)
  carries the same fix from the start.

### Fixed — `tests/test_smoke_e2e.py`: stale flat `accepts_schema` broke admit

- **What:** Removed the explicit
  `accepts_schema={"type": "object", "properties": {…}}` pin from the test
  agent's `AgentInfo`; it now auto-derives from the handler's payload
  model.
- **Symptom:** The e2e round-trip failed at admit with
  `schema_mismatch: destination exposes multiple modes (['properties',
  'type'])`.
- **Root cause:** The router now reads `AgentInfo.accepts_schema` as a
  **per-mode map** `{mode: schema|null}`, so a flat single JSON schema is
  parsed as *mode names* (`type`, `properties`). Admit then sees multiple
  modes and requires `input_mode`, which `TestRouter.call` doesn't set.
  Pre-existing breakage in the platform test (the test wasn't updated when
  `accepts_schema` moved to the per-mode shape); surfaced while running
  the suite's regression subset.
- **Verified:** `tests/test_smoke_e2e.py` passes.

### Added — `tests/conftest.py`: `suite_db_url` fixture

- **What:** A `suite_db_url` pytest fixture (reads `SUITE_DATABASE_URL`,
  skips when unset), alongside the existing `test_db_url`.
- **Why:** Suite DB tests need their own DSN (the suite keeps its own
  Postgres). Purely **additive** to the shared test-infra file — no
  existing fixture or behaviour changed.

---

## Completeness

As of this date, the suite-driven footprint on vendored platform code is:
`bp_sdk/agent.py`; `bp_sdk/testing.py`; `bp_router/db/migrations/env.py`;
`bp_router/api/admin.py` + `bp_router/db/queries.py`;
`bp_router/llm/presets.py` (seed lineup refresh + `default_embedding`);
the **embedding output-dimension** change across
`bp_router/llm/service.py` + `bp_router/llm/providers/`
(`base.py`, `gemini.py`, `openai.py`, `openai_compatible.py`,
`anthropic.py`); and the `proxyfiles`-relic rename in
`bp_router/storage/local.py` + `bp_sdk/testing.py` (default dirs).
Platform tests touched: `tests/test_smoke_e2e.py`,
`tests/conftest.py`, `tests/test_llm_provider_options.py`,
`tests/test_upstream_bugs_boot_blockers.py`,
`tests/test_llm_openai_adapter.py`, `tests/test_llm_anthropic_adapter.py`,
`tests/test_llm_presets.py`, and the new
`tests/test_llm_embed_dimensions.py`. `bp_protocol/` and `bp_admin/` are
unmodified; the suite's own Alembic config lives in a separate
`alembic_suite.ini` (not a change to the router's `alembic.ini`). Verified
by `git diff <template-baseline>..HEAD -- bp_protocol bp_sdk bp_router
bp_admin`.
