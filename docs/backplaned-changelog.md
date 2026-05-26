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
