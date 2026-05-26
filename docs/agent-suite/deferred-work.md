# Agent Suite — Deferred Work & Caveats

> A running ledger of **intentional simplifications, deferred refinements,
> and known caveats** accumulated while building the suite phase-by-phase
> (see [`build-plan.md`](./build-plan.md)). Each item is implemented in a
> leaner form than the design docs' full spec, or not yet wired, and is
> safe to revisit later. This is suite-scoped; platform-code modifications
> are tracked separately in [`../backplaned-changelog.md`](../backplaned-changelog.md).
>
> Status legend: **deferred** (planned, not built) · **lean** (built, but
> simpler than spec) · **unverified** (built, not yet exercised end-to-end).

## Verification gaps

- **unverified — `HttpChannelCredentials` HTTP paths** (`agents/chatbot/credentials.py`).
  The service-token refresh/rotation, per-user mint, registration submit,
  serviced-session poll, session open, and task cancel are unit-shaped but
  have **not** been run against a live router. Exercise via the dev
  launcher with a real Telegram bot + an LLM-key'd router.
- **unverified — `scripts/run-suite.sh`** — the dev launcher is not run in
  CI (needs a running router, a Telegram token, and an LLM key in the
  router env).

## Sessions / summarization

- **lean — summarization runs inside the session lock**
  (`agents/chatbot/gateway.py::_maybe_summarize`). [sessions.md §3.1]
  permits this ("a user message arriving during the summarize op waits
  behind it"), but the documented optimization — run the summarizer
  **loop outside** the queue and serialize only the **apply** step — is
  deferred.
- **deferred — hard-limit inline guard** ([sessions.md §3.2]). A turn that
  would breach the provider's *hard* window should summarize inline before
  running. Only the soft-limit proactive path is built.

## Knowledge base

- **deferred — LLM metadata generation on `store`**. Missing
  `title`/`tags`/`description` are currently defaulted (title from the
  filename); [agents.md] specifies LLM-generated metadata from the head/tail
  of the document.
- **deferred — `modify` mode**. `store`/`retrieve`/`list`/`remove` are
  built; `modify` (re-title / move collection / re-tag) is not.
- **deferred — non-text ingest routing**. `store` ingests Markdown/text
  directly; non-`.md` files should route through `md_converter.convert`
  first (the agent exists; the call is not yet wired into KB `store`).
- **lean — retrieval is vector + Python-side filters**. [data-model.md
  §2.1] specifies a hybrid vector + BM25 index; the current path is vector
  search with collection/title/tag filtering in Python. FTS/hybrid fusion
  is deferred.
- **lean — Markdown chunker** (`knowledge_base/chunking.py`) is a
  paragraph-accumulating splitter within `[min,max]`+overlap; the full
  header→…→char fallback chain is a refinement.

## Memory

- **lean — phases 3 & 4 are best-effort** (`agents/memory/agent.py`).
  Extract + reconcile (phases 1–2) land facts + relate edges robustly;
  relate-out / update-propagation (phases 3–4) are implemented but wrapped
  best-effort, as [memory.md §3] designates them.
- **lean — retrieve uses vector + recency decay** (no BM25 leg yet);
  graph-expansion neighbours not in the search pool are ranked by recency
  decay alone (similarity unknown without re-embedding).
- **deferred — GC scheduling**. `MemoryStore.gc()` exists and cascades
  edges, but nothing schedules the periodic sweep yet.

## ACL

- **operator step — rule-set application**. `python -m bp_agents.load_acl`
  applies the suite rule set, but it is a manual/deploy step (not run
  automatically). Tier-gating deny rules ([acl.md §5]) are not included in
  the default set.

## Known unrelated platform test

- `tests/test_docs_cleanup.py::test_acl_doc_pseudocode_includes_admin_service_branch`
  fails in this environment (a pre-existing platform doc test —
  `FileNotFoundError`, fails independently of the suite changes). Not a
  suite caveat; noted here only so it isn't mistaken for a regression.
