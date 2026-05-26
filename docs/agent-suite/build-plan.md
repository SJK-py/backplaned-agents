# Agent Suite — Build Plan

> The phased implementation plan for building the suite on top of the
> Backplaned router/SDK. Read [`overview.md`](./overview.md) first for the
> architecture; this doc is about **sequencing the build**. Companion
> design docs: [`agents.md`](./agents.md), [`delegation.md`](./delegation.md),
> [`sessions.md`](./sessions.md), [`channel.md`](./channel.md),
> [`memory.md`](./memory.md), [`cron.md`](./cron.md), [`acl.md`](./acl.md),
> [`data-model.md`](./data-model.md).

## 0. Strategy

- **Sequencing:** thin **vertical slice first** — get one end-to-end path
  (chatbot → orchestrator → reply) live early, then layer outward. This
  de-risks the hardest integrations (B1 root-task injection, session
  management, delegation) before breadth.
- **Topology:** **one external SDK process per agent**, each connecting to
  the router over WS — matches the gateway/agent model in the docs and gives
  clean isolation + independent restart. A dev launcher starts them all.
- **Scope:** **v1** = the full roster except `webapp`. Single channel
  (Telegram); cron scheduler + routing live in the chatbot.

### Platform prerequisites — confirmed present

The router already implements everything the suite assumes (verified against
the codebase):

| Prerequisite | Location |
| --- | --- |
| Root-task injection (parentless admit, lineage skipped when `parent_task_id is None`) | `bp_router/tasks.py`, `db/queries.py` |
| `provisions_service_user` onboarding → `usr_service_{agent_id}` + refresh token | `bp_router/api/onboard.py` |
| `serviced_by` auto-grant + initial session at registration approval | `bp_router/api/admin.py::approve_registration` |
| Session-authed named-store endpoints (`POST`/`GET /v1/files/names[/resolve]`) | `bp_router/api/files.py` |
| `serviced_by` refresh-token + password-reset minting | `bp_router/api/admin.py` |
| Session open/close with close-GC | `bp_router/api/sessions.py` |

**The one gap:** there is no SDK helper for B1 — `peers.spawn` always sets
`parent_task_id = ctx.task_id`. The channel's parentless `outbound_admit` /
`outbound_await_result` must be built (Phase 0).

## 1. Package layout

New suite code lives alongside the vendored platform packages
(`bp_protocol`, `bp_sdk`, `bp_router`, `bp_admin`):

```
bp_agents/
  settings.py            # suite Settings: suite DB URL, router URL, LanceDB root, env caps, telegram token ref
  db/                    # suite Postgres: connection pool, models, queries
  migrations/            # suite Alembic (separate config from the router's alembic.ini)
  lance/                 # per-user LanceDB: knowledge + memory store wrappers
  common/                # shared building blocks (Phase 0)
  agents/
    orchestrator/  computer_use/  research/  deep_reasoning/  config/
    knowledge_base/  memory/  history_summarizer/  md_converter/  sandbox/
    chatbot/             # gateway: telegram poll, session_manager, cron, file I/O, B1 injection
  __main__.py            # `python -m bp_agents.<agent>` entrypoints + dev launcher
```

- Add a `[suite]` extra to `pyproject.toml` (`lancedb`, `croniter`,
  `markitdown`, plus already-present `asyncpg` / `httpx`) and add
  `bp_agents` to `packages`.

## 2. Phases

### Phase 0 — Foundations (just enough for the slice)

- **SDK gap (B1).** Add a supported root-task API to `bp_sdk`:
  `Agent.spawn_root_for_user(dest, payload, *, user_id, session_id, mode,
  timeout_s)` → `task_id`, plus `await_root_result(task_id, on_progress=…)`.
  Builds a parentless `NewTaskFrame` and rides the existing dispatcher
  `transport.send` + `register_for_task` correlation map. Unit-tested against
  `bp_sdk.testing.TestRouter`.
- **Suite DB.** Tables `session_info`, `session_history`, `user_config`,
  `suite_platform_mappings` (+ indexes per [data-model.md](./data-model.md)),
  a suite Alembic config, connection pool, and a `queries` module. Cron tables
  deferred to Phase 4.
- **`common/`.** `AgentOutput` builders; the `current_time` tool; a generic
  `run_llm_loop(ctx, …)` (LLM `generate` → `spawn_from_tool_call` per tool
  call → repeat) shared by the orchestrator + l1 agents; the `LoopProgress`
  progress model; a context-token measurement helper that stamps
  `metadata.context_tokens`; system-prompt composition helpers.
- **Dev tooling.** A suite launcher that mints invitations (reusing
  `scripts/run-test-agents.sh` patterns) and starts the agents.

**Milestone:** B1 helper green under `TestRouter`; suite DB migrates;
`common` importable.

### Phase 1 — Vertical slice: chatbot → orchestrator → reply

No delegation, no subagent tools yet (orchestrator gets only `current_time`).

- **orchestrator** (`message` mode): reload its own incumbent
  `user`/`assistant` rows; system prompt = general + user-config note +
  `history_summary`; run the LLM loop; append its assistant turn; return
  `AgentOutput` with `context_tokens`.
- **chatbot gateway:** three identities (agent JWT + onboarding-provisioned
  service principal + per-user token cache); Telegram long-poll loop with
  persisted offset in `on_startup`; identity resolution
  `chat_id → user_id → default_session_id`; per-session FIFO queue; sole
  writer of verbatim `user` rows; dispatch to `orchestrator(message)` via the
  **B1 helper**; relay result text. Slash commands: `/new`, `/register`,
  `/help`, `/stop`.
- **Approval seam (resolve here).** `/register` submits `POST
  /v1/registrations` as the service principal with `metadata={chat_id}`. A
  chatbot background loop polls its submitter-filtered registrations; on
  approval it writes `suite_platform_mappings(chat_id→user_id)` + creates the
  `user_config` row with `default_session_id` = the session the router opened
  at approval. (Router-side approval already creates the user + `serviced_by`
  + initial session.)

**Milestone:** a real Telegram message round-trips to an LLM reply, persisted
in suite history.

### Phase 2 — l3/l4 tools + summarization

- **history_summarizer** (read-only `summarize_incumbent` / `summarize_all`)
  + channel summarization trigger (queued, `context_tokens`-driven, writes
  `history_summary`, flips `incumbent`); hard-limit inline guard.
- **memory** (per-user LanceDB fact graph + edge-set; per-user lock; 4-phase
  `add`, lock-free `retrieve`; embedding preset). Channel fires `memory.add`
  post-turn **outside** the queue.
- **knowledge_base** (per-user LanceDB documents/chunks;
  `store`/`retrieve`/`list`/`modify`/`remove`; chunking chain) and
  **md_converter** (`convert` / `webpage`).
- Wire the orchestrator's `build_tools` catalog + load the suite **ACL rule
  set** ([acl.md](./acl.md)).

**Milestone:** orchestrator recalls memory and answers from stored documents;
long sessions summarize.

### Phase 3 — l1 specialists + delegation + sandbox

- **computer_use / research / deep_reasoning** with `subagent` /
  `on_delegation` / `delegated_message` modes; research's web tools (SearXNG
  `web_search`, `html_fetch`→`md_converter.webpage`, `web_download`);
  deep_reasoning's in-process `plan_mode`.
- **sandbox** (heaviest): per-user Debian container workspace, `bash`,
  `storage_to_workspace` / `workspace_to_storage`. The container/uid model is
  the main risk here.
- **Delegation lifecycle** ([delegation.md](./delegation.md)): hand-off
  (`delegate` + `delegate_prompt` seed row), channel `delegated_to`
  maintenance via result-source observation, hand-back (`end_delegation`),
  failure modes F1–F5, single-level invariant.

**Milestone:** orchestrator delegates a coding task to computer_use,
steady-state turns route to it, then hands back.

### Phase 4 — cron + config + file I/O

- `cron_jobs` / `cron_executions` tables; chatbot `cron` mode (LLM job
  management) + scheduler daemon (atomic claim, ≤1 catch-up, expiry);
  `orchestrator(cron_message)`; apply step + report/spam policy; session
  resolution + default-pointer transfer ([cron.md](./cron.md)).
- **config** (l2) + `/config` slash command.
- Channel file I/O ([channel.md §7](./channel.md)): inbound save → name →
  `(T,T)` history row; outbound resolve names → send (via session-authed
  `/v1/files/names` endpoints).

**Milestone:** scheduled jobs fire and report per policy; users edit config
conversationally; files flow both ways.

### Phase 5 — Hardening & deploy

Verbose `LoopProgress` rendering; quota / tier-gating review; per-agent
Dockerfiles + compose; end-to-end integration tests; deploy docs.

## 3. Cross-cutting decisions

1. **B1 helper lives in `bp_sdk`** (a supported API), not suite-local —
   cleaner than reaching into dispatcher internals, and the design endorses
   promoting it.
2. **Separate suite Alembic config** rather than reusing the router's
   `alembic.ini` — the suite DB is independent.
3. **Per-user memory lock is in-memory single-instance for v1** (Redis
   later), matching the single-replica router.
4. **SearXNG ships in the default `docker-compose` deployment behind a
   compose `profile`** — enabled by default for a batteries-included install,
   but skippable. Operators that run their own SearXNG (or another
   Brave-API-compatible backend) disable the profile and point research's
   `web_search` at the external instance via config (a `SEARXNG_URL` /
   search-backend setting). The agent code is backend-agnostic; only the
   endpoint changes.
5. **Sandbox container model** (Docker-per-user vs shared container + uid
   isolation) — decided in Phase 3; the biggest open unknown.

## 4. Testing approach

- **`bp_sdk.testing.TestRouter`** spins up an in-process router over a real WS
  on a random port (against a Postgres test DB with the schema pre-applied).
  Suite agents run via `Agent.run_async()` against it; drive them by injecting
  synthetic Telegram updates (Phase 1+) or via `TestRouter.call`.
- Each phase ships with integration tests asserting its milestone end-to-end.
- Suite-DB tests follow the platform pattern: truncate-between-tests, schema
  applied via the suite Alembic `upgrade head` in CI.
