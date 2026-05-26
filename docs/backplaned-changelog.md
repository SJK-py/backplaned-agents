# Backplaned Platform — Modification Changelog

> This repository was created from the **Backplaned** template and vendors
> the platform packages (`bp_protocol`, `bp_sdk`, `bp_router`, `bp_admin`)
> plus their tests. Building the agent suite (`bp_agents`, see
> [`agent-suite/`](./agent-suite/)) occasionally requires changing that
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

## 2026-05-26

### Changed — local file-store default dir renamed (drop `proxyfiles` relic)

- **What:** The `LocalFileStore` default path (used when
  `ROUTER_FILE_STORE_OPTIONS` has no `path`) was renamed from `./proxyfiles`
  to `./router_files` (`bp_router/storage/local.py`); the `TestRouter`
  harness default likewise `./.test_proxyfiles` → `./.test_router_files`
  (`bp_sdk/testing.py`).
- **Why:** `proxyfiles` was vestigial naming from the predecessor
  `ProxyFile` file model, which the router-managed file store
  ([`docs/design/router-managed-file-store.md`](./design/router-managed-file-store.md))
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
  intent ([`agent-suite/channel.md` §2](./agent-suite/channel.md),
  [`agent-suite/overview.md` §2.1](./agent-suite/overview.md)).
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
  §4](./agent-suite/channel.md)). `peers.spawn` cannot do this: it is
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
