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

- **deferred — LLM metadata generation on `store`** (title from filename
  today vs [agents.md]'s head/tail LLM-gen).
  *Why:* **bounded-scope** — sensible defaults land documents now; LLM
  metadata is polish that adds an extra structured call per store.
- **deferred — `modify` mode** (store/retrieve/list/remove built).
  *Why:* **bounded-scope** — the recall milestone ("answer from stored
  docs") needs store + retrieve; modify is additive management.
- **deferred — non-text ingest routing** through `md_converter.convert`.
  *Why:* **bounded-scope / ordering** — md_converter shipped after KB in
  Phase 2; the call isn't wired into KB `store` yet (text/Markdown ingest
  works today).
- **lean — retrieval is vector + Python-side filters** (vs the [data-model.md
  §2.1] hybrid vector+BM25 index).
  *Why:* **bounded-scope + test-robustness** — vector + filters is
  deterministic and version-independent; LanceDB FTS/hybrid fusion is
  version-finicky and was deferred to keep the path reliable.
- **lean — Markdown chunker** is paragraph-accumulating within
  `[min,max]`+overlap (vs the full header→…→char fallback chain).
  *Why:* **bounded-scope** — the simple splitter is adequate for ingest;
  the full chain is a fidelity refinement.

## Memory

- **lean — phases 3 & 4 are best-effort** (`memory/agent.py`).
  *Why:* **spec-marks-optional** — [memory.md §3] designates relate-out /
  update-propagation as "enhancement, safe to skip"; phases 1–2 (the facts
  + edges that matter) are robust.
- **lean — retrieve uses vector + recency decay** (no BM25 leg; neighbours
  outside the pool ranked by recency alone).
  *Why:* **bounded-scope** — same hybrid-search deferral as the KB; the
  decay + 1-hop expansion is the load-bearing behaviour.
- **deferred — GC scheduling**. `MemoryStore.gc()` exists + cascades, but
  nothing runs the periodic sweep.
  *Why:* **bounded-scope** — GC is a background maintenance loop; the
  decay path already keeps surfaced facts alive, so the sweep is
  non-urgent and best added with the same scheduler treatment as cron.

## Delegation / l1 specialists

- **deferred — deep_reasoning `plan_mode`** ([agents.md]) — the bespoke
  fresh-loop step planner (`add_step`/`execute_step`→`orchestrator(subagent)`/
  `quit_and_report`).
  *Why:* **bounded-scope** — it's a large, agent-specific sub-loop;
  deep_reasoning works as a standard l1 (subagent + delegation) without
  it, so it was separated from the delegation-core milestone.
- **lean — l1 `current_time` uses the default timezone**, not the user's.
  *Why:* **bounded-scope** — minor; the orchestrator's `message` mode
  already uses the user tz, and threading the per-user tz into the l1
  local-tools factory is a small follow-up.
- **deferred — agent-loop agents can't view attached files multimodally**
  ([agents.md] — orchestrator caps `llm.multimodal.image` + `file.full`).
  No suite agent registers the SDK `file_tools` (`read_file` → next-turn
  `file_ref` attachment) or calls `ctx.files.llm_ref(name)`, so the model
  sees only the *"user-attached file saved as `{name}`"* history row, not
  the bytes. (The channel side is complete and correct by design — it's a
  gateway with no `ctx.files` and never feeds the LLM ([channel.md] §7);
  this is purely an orchestrator/l1 loop concern.)
  *Why:* **bounded-scope** — text ingest is the v1 path; wiring `read_file`
  / `llm_ref` into the orchestrator + l1 loops is additive (the
  `gemini_agent` example shows the vision path to copy).
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
