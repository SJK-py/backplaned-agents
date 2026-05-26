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

## Verification gaps

- **unverified — `HttpChannelCredentials` HTTP paths** (`agents/chatbot/credentials.py`).
  Service-token refresh/rotation, per-user mint, registration submit,
  serviced-session poll, session open/cancel, and the named-file store
  client are unit-shaped but not run against a live router.
  *Why:* **needs-live-stack** — exercising them requires a running router
  with admin creds + a real Telegram bot; the logic is covered by fakes,
  and the one router-side seam (`serviced-sessions`) has its own test.
- **unverified — `scripts/run-suite.sh`** dev launcher.
  *Why:* **needs-live-stack** — needs a router, a Telegram token, and an
  LLM key; bash glue with no unit surface. Syntax-checked only.

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

## Knowledge base

- **done — LLM metadata generation on `store`** ([agents.md]). Missing
  `title` / `tags` / `description` are LLM-generated from the document's
  head (8k) + tail (2k chars, env-configurable) via the user's lite preset;
  supplied fields are respected, and `title` still falls back to the
  filename stem if generation yields none
  (`knowledge_base/agent.py::_generate_metadata`).
- **done — `modify` mode**. Re-files / retitles / re-tags a document and its
  denormalized chunk metadata (`KnowledgeStore.modify_document`).
- **done — non-text ingest routing** through `md_converter.convert`. A
  non-text source is converted to Markdown (full content via a `.md` stash
  file) before chunking; content-addressed dedup is over the *original*
  bytes (`knowledge_base/agent.py::_to_markdown`).
- **done — hybrid / bm25 retrieval** ([data-model.md] §2.1). `search_type`
  selects `vector`, `bm25` (LanceDB native FTS over `content`), or `hybrid`
  (reciprocal-rank fusion of both legs, Python-side — no LanceDB reranker,
  so version-independent). Metadata filters stay Python-side.
- **done — Markdown chunker fallback chain**. Recursive split on
  header → blank line → newline → sentence → word → character, recursing
  into oversized spans, then accumulating into `[min,max]` with overlap
  (`knowledge_base/chunking.py`).

## Memory

- **lean — phases 3 & 4 are best-effort** (`memory/agent.py`).
  *Why:* **spec-marks-optional** — [memory.md §3] designates relate-out /
  update-propagation as "enhancement, safe to skip"; phases 1–2 (the facts
  + edges that matter) are robust.
- **lean — retrieve uses vector + recency decay** (no BM25 leg; neighbours
  outside the pool ranked by recency alone).
  *Why:* **bounded-scope** — same hybrid-search deferral as the KB; the
  decay + 1-hop expansion is the load-bearing behaviour.
- **done — GC scheduling**. The memory agent launches a background sweep on
  startup (same shape as the cron scheduler: run a pass, wait
  `memory_gc_interval_s` (default daily) or until stopped). Each pass
  iterates every user with an existing fact graph and runs `gc()` under
  that user's lock — serialized against `add`
  (`memory/agent.py::gc_sweep` / `gc_sweep_loop`).

## Delegation / l1 specialists

- **deferred — deep_reasoning `plan_mode`** ([agents.md]) — the bespoke
  fresh-loop step planner (`add_step`/`execute_step`→`orchestrator(subagent)`/
  `quit_and_report`).
  *Why:* **bounded-scope** — it's a large, agent-specific sub-loop;
  deep_reasoning works as a standard l1 (subagent + delegation) without
  it, so it was separated from the delegation-core milestone.
- **done — l1 `current_time` uses the user's timezone**. The per-user tz is
  threaded into the l1 local-tools factory (`L1Config.local_tools` now takes
  `(ctx, settings, timezone)`); `run_subagent` / `run_delegated_turn`
  resolve it from `user_config` (falling back to the default).
- **done** — file-capable loop agents now register the SDK `file_tools`
  ([agents.md] — orchestrator/l1 caps `file.full` + `llm.multimodal.image`).
  `run_llm_loop` takes a `file_tools` bundle and dispatches the calls via
  `dispatch_file_tool(ctx.files, …)`; the orchestrator (all loop paths) and
  the three l1s (`deep_reasoning` / `computer_use` / `research`) pass
  `"full"`. `read_file` returns a name `file_ref` the router resolves into
  multimodal content on the next turn, so the model can view user-attached
  / produced images & PDFs — not just the *"file saved as `{name}`"* row.
  (The channel stays a gateway with no `ctx.files`, by design ([channel.md]
  §7).)
- **done (Phase 5)** — router-level delegation e2e (`test_delegation_e2e`):
  real orchestrator → deep_reasoning hand-off over a live `TestRouter`
  (task reassignment + the exactly-one-Result drop).
- **done** — F1 hand-off fallback ([delegation.md] §4). On a failed
  `delegate` admit (rejected / ack-timeout / disconnected) the orchestrator
  now retires the orphan `delegate_prompt` seed row and re-runs its loop
  (no hand-off tool) to answer the turn directly, producing a real Result
  instead of surfacing a generic dispatch error
  (`orchestrator/agent.py::_run_hand_off_fallback`).

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
- **done** — `/password` slash command ([channel.md] §6). The gateway now
  mints a one-time password-setup token for the mapped user via the service
  principal's `serviced_by` rights (router F9 endpoint
  `POST /v1/admin/users/{id}/password-reset-tokens`); see
  `gateway.py::_cmd_password` + `credentials.py::mint_password_reset_token`.

## ACL

- **operator step — rule-set application** via `python -m bp_agents.load_acl`
  (not auto-run); tier-gating deny rules ([acl.md §5]) not in the default set.
  *Why:* **operator** — ACL is admin-managed by design (the router owns
  the rule list); the loader is a deliberate deploy step, and tier gating
  is deployment-specific policy.

## Known unrelated platform test

- `tests/test_docs_cleanup.py::test_acl_doc_pseudocode_includes_admin_service_branch`
  fails in this environment (a **pre-existing** platform doc test —
  `FileNotFoundError`, fails independently of the suite changes, confirmed
  by stashing the suite work). Not a suite caveat; noted only so it isn't
  mistaken for a regression.
