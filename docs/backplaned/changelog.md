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
