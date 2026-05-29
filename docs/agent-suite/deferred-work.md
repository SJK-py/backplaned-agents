# Agent Suite — Deferred Work & Caveats

> A running ledger of **intentional simplifications, deferred refinements,
> and known caveats** accumulated while building the suite phase-by-phase
> (see [`build-plan.md`](./build-plan.md)). Each item is implemented in a
> leaner form than the design docs' full spec, or not yet wired, and is
> safe to revisit later. Each entry states **why** it was deferred. This
> is suite-scoped; platform-code modifications are tracked separately in
> [`../backplaned-changelog.md`](../backplaned-changelog.md).
>
> Status legend: **deferred** (planned, not built) · **lean** (built, but
> simpler than spec) · **unverified** (built, not yet exercised end-to-end).
> Deferral reasons fall into a few buckets: *needs-live-stack* (can't be
> exercised in CI without a router + provider key + external service),
> *spec-marks-optional* (the design doc itself designates it an
> enhancement), *bounded-scope* (correct lean cut chosen to land the
> milestone; full fidelity is additive), and *operator/v2* (a deploy step
> or a v2-gated feature).
>
> Items that have since been closed are logged under
> [**Resolved**](#resolved) at the bottom for the record.

## Verification gaps

- **unverified — `HttpChannelCredentials` HTTP paths** (`agents/chatbot/credentials.py`).
  Service-token refresh/rotation, per-user mint, registration submit,
  serviced-session poll, session open/cancel, password-reset mint, and the
  named-file store client are unit-shaped but not run against a live router.
  *Why:* **needs-live-stack** — exercising them requires a running router
  with admin creds + a real Telegram bot; the logic is covered by fakes,
  and the one router-side seam (`serviced-sessions`) has its own test.
- **unverified — `scripts/run-suite.sh`** dev launcher.
  *Why:* **needs-live-stack** — needs a router, a Telegram token, and an
  LLM key; bash glue with no unit surface. Syntax-checked only. The launcher
  now: (a) persists per-agent creds under a **durable** `$BP_SUITE_STATE_ROOT`
  (XDG state, was ephemeral `/tmp/bp-suite`) — agents onboard once
  (invitation tokens are single-use) and resume forever after, so a reboot no
  longer strands a router-registered agent with no creds; (b) **re-mints when
  persisted creds are unusable** (`creds_resumable`: missing/empty/expired
  `auth_token`, mirroring the SDK resume check); and (c) **pre-flights the
  router's registration** so a lost-creds-but-already-registered agent fails
  with actionable recovery guidance instead of a cryptic onboard **409**
  (there is no de-register endpoint — `evict` is terminal — so recovery is a
  dev-DB reset). The credential logic is unit-exercised; the full live boot
  is still needs-live-stack.

## Sessions / summarization

- **lean — summarization runs inside the session lock**
  (`gateway.py::_maybe_summarize`).
  *Why:* **spec-marks-optional** — [sessions.md §3.1] explicitly permits
  the in-lock wait ("a user message arriving during the summarize op waits
  behind it"). The loop-outside-queue / apply-only-inside optimization is
  a latency nicety, not correctness.
- **deferred — hard-limit inline guard** ([sessions.md §3.2]).
  *Why:* **bounded-scope** — the soft-limit proactive path (the common
  case) is built; the hard-window inline fallback is a rare-edge guard
  that adds a blocking inline summarize, deferred until a real provider
  window is wired.
- **deferred — `tool_call` / `tool_result` render/audit history rows**
  ([sessions.md §2.1/§2.2]). Agents persist only the terminal `assistant`
  turn; the live loop emits `ProgressFrame`s but writes no hidden `tool_*`
  rows. The `session_history.role` CHECK already admits them, so this is
  purely additive.
  *Why:* **v2 / bounded-scope** — these rows are *never reloaded* (the loop
  holds the tool sequence in memory), so v1 correctness is unaffected; they
  exist only for the webapp's activity render + audit trail, which lands
  with the webapp (v1 is Telegram-only and uses verbose `ProgressFrame`s
  for live activity).

## Memory

- **lean — phases 3 & 4 are best-effort** (`memory/agent.py`).
  *Why:* **spec-marks-optional** — [memory.md §3] designates relate-out /
  update-propagation as "enhancement, safe to skip"; phases 1–2 (the facts
  + edges that matter) are robust.

## Webapp Memory / Knowledge base pages

- **lean — the pages ride a carrier session** (`webapp/pages/_common.py::
  carrier_session`). Memory/KB are per-**user**, but root-task admit requires
  an **open** session, so a management query rides the user's
  `default_session_id` (or the newest open session); with **none open** the
  pane shows an empty state instead of dispatching.
  *Why:* **bounded-scope** — avoids a router admit change for a session-less
  "management" task class. A session-less management capability is the fuller
  fix (router-side), deferred.
- **lean — Memory `list` query mode scores the hybrid pool, not all facts**
  (`memory/agent.py::run_memory_list`). Deep pagination past
  `memory_retrieve_pool` returns empty for a query.
  *Why:* **bounded-scope** — browse, not exhaustive recall; scoring every
  fact would embed the whole graph per page.
- **note — KB-page reach is capability-scoped, not a new rule.** The webapp
  carries `database.*`, so the existing `*/database.* → l3/database.*` rule
  covers the KB page (no broad `channel/* → database` grant; the chatbot can't
  reach the KB). This takes effect when the webapp re-registers its `AgentInfo`
  (handshake) — no ACL change to re-apply. Memory rides `channel/* → memory.add`.

## Cron / channel files

- **deferred — cron C4 fallback** (`cron.py::_resolve_session`): open a
  fresh session when both the job + default sessions are closed.
  *Why:* **bounded-scope (rare edge)** — falls back to the job's
  `session_id` today; the open-fresh-and-move-pointer path is an
  uncommon-case refinement.
- **deferred — v2 channel-agnostic cron routing** ([cron.md] §6).
  *Why:* **v2** — only matters once `webapp` exists (v1 is Telegram-only,
  every session is reachable).
- **lean — cron report decision** is a second lite LLM call after the loop.
  *Why:* **bounded-scope** — works; a single structured-output call would
  be tidier/cheaper.
- **deferred — chatbot `message_to_user` / `file_to_user` push modes**
  ([agents.md]). The channel registers only the `cron` handler mode; the
  two *proactive*-push modes (out-of-band "push text to chat" / "send a
  stash file") are not wired.
  *Why:* **bounded-scope** — the docs flag both as proactive-only ("the
  common path never calls them"); v1's request/reply + cron paths never
  invoke them. Additive when an out-of-band push trigger exists.

## ACL

- **operator step — rule-set application** via `python -m bp_agents.load_acl`
  (not auto-run); tier-gating deny rules ([acl.md §5]) not in the default set.
  *Why:* **operator** — ACL is admin-managed by design (the router owns
  the rule list); the loader is a deliberate deploy step, and tier gating
  is deployment-specific policy.

## Scaling / multi-instance

- **constraint — single router WS process.** The router's `SocketRegistry`
  is in-memory per process and there is **no cross-worker frame bus** (no
  Redis pub/sub); `deliver_frame`/`fanout_frame` only find sockets on the
  local worker. So delegation hand-off, Progress/Result fan-out, and
  CatalogUpdate broadcast assume caller + callee share one process. Redis
  (required in staging/prod) covers **only** JWT revocation + the
  admit/auth rate-limit buckets — **not** horizontal WS scaling. Run **one
  router WS process**; true multi-worker needs a pub/sub routing layer
  (future work).
- **lean — channel per-session lock is Redis-backable, the rest is
  single-instance.** Setting `SUITE_REDIS_URL` makes the per-session turn
  lock distributed (`session_lock.py`) — the prerequisite for a second
  channel (webapp). Still single-instance without further work: the
  chatbot's Telegram poll offset (one poller) and the per-worker in-memory
  caches. The cron scheduler is one loop per chatbot process.

## Known unrelated platform test

- `tests/test_docs_cleanup.py::test_acl_doc_pseudocode_includes_admin_service_branch`
  fails in this environment (a **pre-existing** platform doc test —
  `FileNotFoundError`, fails independently of the suite changes, confirmed
  by stashing the suite work). Not a suite caveat; noted only so it isn't
  mistaken for a regression.

## Resolved

Items previously listed above that have since been built to full spec. Kept
as a short record; the detail lives in the commit/PR history.

**Knowledge base** ([agents.md], [data-model.md] §2.1)
- LLM metadata generation on `store` — head+tail window fills a missing
  `title`/`tags`/`description` (`knowledge_base/agent.py::_generate_metadata`).
- `modify` mode — re-file / retitle / re-tag a doc + its denormalized chunks
  (`KnowledgeStore.modify_document`).
- Non-text ingest routing through `md_converter.convert` before chunking
  (dedup over the original bytes, `knowledge_base/agent.py::_to_markdown`).
- Hybrid / bm25 retrieval — `search_type` ∈ `vector`/`bm25`/`hybrid`;
  LanceDB native FTS + Python reciprocal-rank fusion (no reranker).
- Full recursive Markdown chunker — header → blank → newline → sentence →
  word → char, accumulated into `[min,max]` with overlap (`chunking.py`).

**Memory** ([memory.md] §4–5, [data-model.md] §2.2)
- Hybrid retrieve — vector + BM25-over-`fact` legs fused (RRF), then
  recency-decay re-rank → 1-hop expansion (`MemoryStore.search_bm25`).
- GC scheduling — background sweep on startup (cron-style loop), per-user
  under that user's lock (`memory/agent.py::gc_sweep` / `gc_sweep_loop`).

**Delegation / l1 / files**
- l1 `current_time` uses the user's timezone (threaded through the l1
  local-tools factory).
- File tools + multimodal feed on file-capable loop agents — `run_llm_loop`
  exposes the SDK `file_tools`; `read_file` resolves to multimodal content
  on the next turn (orchestrator + the three l1s).
- F1 hand-off fallback ([delegation.md] §4) — a failed `delegate` admit
  retires the orphan seed row and answers the turn directly
  (`orchestrator/agent.py::_run_hand_off_fallback`).
- Router-level delegation e2e (`test_delegation_e2e`) — real orchestrator →
  deep_reasoning hand-off over a live `TestRouter`.
- deep_reasoning `plan_mode` ([agents.md]) — the bespoke planning sub-loop
  (`add_step`/`modify_step`/`remove_step`/`execute_step`→`orchestrator(subagent)`/
  `quit_and_report`), entered as a terminal tool on delegated turns via the
  `L1Config.extra_terminal` seam (`deep_reasoning/plan.py`). In-memory plan
  state, bounded by `plan_max_steps`/`plan_max_iters`.

**Channel**
- `/password` slash command ([channel.md] §6) — mints a one-time
  password-setup token via the service principal's `serviced_by` rights
  (`gateway.py::_cmd_password` + `credentials.py::mint_password_reset_token`).
