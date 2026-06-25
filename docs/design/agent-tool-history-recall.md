# On-demand recall of past tool results

> **Status: proposal.** Review of the "let an agent re-read its own
> previous tool turns" idea, plus a concrete plan. No code landed yet.

## 1. The idea under review

Today the live tool-calling loop (`bp_agents/common/loop.py`) keeps the
full tool sequence — `tool_call` + `tool_result` rows — in the in-memory
`messages` list for the duration of **one turn**, and feeds it to the
model. Across turns the persisted context is rebuilt by
`queries.reload_incumbent`, which **only reloads `user`/`assistant`
rows** ([sessions.md] §2.1). Tool rows are deliberately excluded to keep
context bounded; the contract is that the terminal `AgentOutput.content`
must be self-contained.

That contract is brittle. A model occasionally needs a detail from a
prior turn's tool result that it didn't carry forward into its
self-contained answer (the exact rows a query returned, a URL from a
web search three turns ago, a value it summarised away). Proposal: give
l0/l1 agents a **local tool** that fetches the *N* most recent prior
tool exchanges on demand, with a single argument — how many to retrieve.

**Verdict: sound, and a good fit for the architecture** — it is a
strictly-additive, model-controlled escape hatch that preserves the
"omit tool rows from reload" default while letting the model pay the
context cost only when it actually needs the detail. But it rests on a
premise that is **not true in the current code**, and the "one count
argument" needs guardrails to not silently re-introduce the bloat the
omission was designed to prevent. Both are addressed below.

## 2. Blocking precondition: tool rows are not actually persisted yet

The premise "tool results are appended on session history" is only
half-true today:

  * The schema supports it — `session_history.role` allows
    `tool_call` / `tool_result` (`migrations/0001`, `db/models.py`).
  * The reload query explicitly excludes it (`queries.reload_incumbent`).
  * **But no production code writes those rows.** A repo-wide search for
    `role="tool_call"` / `role="tool_result"` finds only one *test*
    fixture (`tests/test_suite_db.py`). `loop.py` appends tool results
    to the in-memory `messages` list and nothing else; `l1_common` and
    the orchestrator only ever `append_history(role="assistant"|"user")`.

So a recall tool would have **nothing to read**. Persisting the tool
rows is therefore step one, not an assumption. This is the larger half
of the work; the tool itself is small.

## 3. Goals / non-goals

**Goals**

  * Persist each turn's `tool_call` / `tool_result` exchanges to
    `session_history` (render/audit value too, per [sessions.md] §2.1's
    stated intent).
  * A single local tool, one integer argument, that returns the *N* most
    recent **prior-turn** tool exchanges for the calling agent's own
    thread.
  * Default behaviour unchanged: tool rows still never enter the
    automatic reload; the model opts in per call.
  * Bounded cost: recall can never dump unbounded bytes back into context.

**Non-goals**

  * Reloading tool rows automatically (that is exactly what we keep off).
  * Cross-agent or cross-session recall (an l1 reading the orchestrator's
    tool history, or another session's).
  * Semantic / keyword search over past results — count-based only for v1
    (see §7 for why, and the index extension).

## 4. Design

### 4.1 Persist tool exchanges (prerequisite)

Write `tool_call` / `tool_result` rows for the turn with
`incumbent=false`, `hidden=true` (the matrix in [sessions.md] §2.2
already specifies this row shape). Two placement options:

  * **(Recommended) Batch at turn end.** After `run_llm_loop` returns,
    walk the mutated `messages` list and persist the tool rows produced
    this turn in one pass, inside the same `pool.acquire()` block that
    already writes the terminal `assistant` row (`l1_common` line ~304;
    orchestrator's equivalent). The loop already mutates `messages` in
    place and the caller owns it, so the data is right there.

    Why batch-at-end and not mid-loop: it keeps the write off the
    latency-critical inner loop, and — crucially — it means the **current
    turn's** tool rows are not yet in the DB while the turn is running,
    so recall during a turn naturally sees only *prior* turns. No
    `task_id` column or current-turn filtering needed (there is no
    `task_id` on `session_history` today).

  * Mid-loop, row-by-row in `_dispatch_tool_call`. Rejected: adds a DB
    round-trip per tool call on the hot path, and forces a current-turn
    exclusion mechanism.

Pairing on write: store enough to reconstruct an exchange — for the
`tool_call` row, `{name, args}`; for the `tool_result` row, the
response text. A monotonic `id` already orders them; persist call then
result so `id` ordering reflects causal order.

### 4.2 The recall query

Add to `db/queries.py`:

```python
async def recent_tool_exchanges(
    conn, *, session_id, agent_id, limit
) -> list[SessionHistoryRow]:
    """The most recent `tool_call`/`tool_result` rows for ONE agent's
    thread, newest first, capped at ~2*limit rows (limit exchanges).
    Scoped to (session_id, agent_id) — never another agent or session.
    Tool rows are write-once and never demoted, so `incumbent` is
    ignored here."""
```

`ORDER BY id DESC LIMIT 2*limit`, then re-sort ascending in Python and
group into call→result pairs.

### 4.3 The local tool

A factory next to `make_current_time_tool` / `make_send_file_tool` in
`common/tools.py`. Like `make_send_file_tool(outbound)`, it closes over
turn-local state the handler needs — here the `pool`, `session_id`, and
`agent_id` (the handler signature is fixed to `(ctx, args)`, and `ctx`
exposes `session_id` but not a DB pool, so the pool is injected at
construction, matching the existing injection pattern):

```python
def make_recall_tool_history_tool(pool, *, session_id, agent_id) -> LocalTool:
    name = "recall_tool_history"
    # arg: {"count": int}  (1..MAX_RECALL, default small)
```

  * **One argument**, `count` — number of most-recent prior exchanges to
    return. Clamp to `[1, MAX_RECALL]` (e.g. 10) rather than erroring.
  * Returns a compact, **truncated** text digest, newest-last so it reads
    chronologically:

    ```
    [3 turns ago] web_search(query="…") →
      <result, truncated to PER_RESULT_CHARS with an …(+N chars) marker>
    [2 turns ago] read_file(name="report.csv") →
      (file result — re-open with read_file to view)
    …
    ```

  * Register on every l0/l1 agent that runs `run_llm_loop`, alongside
    `current_time`, by adding it to the `LocalToolset` the orchestrator
    and `l1_common` build per turn.

### 4.4 Bloat guardrails (the core review concern)

The omission exists to prevent bloat; a naive "dump the last N full
results" re-creates exactly that, since a single `web_search` or
`read_file` result can be huge. The single count arg is the right
*ergonomics* but needs bounding so it cannot blow context:

  * **`MAX_RECALL`** caps the count (clamp, don't error).
  * **`PER_RESULT_CHARS`** truncates each result with a `…(+N more)`
    marker — the model sees that detail was elided and can narrow.
  * **`TOTAL_RECALL_CHARS`** budget across the whole digest; stop early
    and say how many exchanges were omitted.
  * Recalled bytes land in the live `messages` and thus count toward
    `metadata.context_tokens`, which can trigger post-turn summarization
    ([sessions.md] §3) — acceptable and self-correcting, but worth noting
    so recall isn't treated as free.

These caps are the reason count-based recall is safe: worst case is
bounded by `min(count, MAX_RECALL) * PER_RESULT_CHARS`, capped again by
`TOTAL_RECALL_CHARS`.

## 5. Scope & safety

  * **Thread-local.** Query filters `session_id = ctx.session_id AND
    agent_id = <this agent>`. An l1 delegate cannot read the
    orchestrator's tool history and vice versa — same isolation the
    reload already enforces.
  * **Prior turns only.** Guaranteed by batch-persist-at-end (§4.1): the
    current turn's tool rows aren't in the DB while the turn runs, and
    the current turn's results are already in `messages` anyway.
  * **No new wire surface.** Pure suite-internal (DB + local tool); no
    router/protocol/SDK change.

## 6. Open questions

  * **Multimodal / `file_ref` results.** A `read_file` tool result is a
    `file_ref` part, not text ([sessions.md] §2). Decision needed for
    what the `tool_result` row stores: recommend the file **name** plus a
    marker, so recall returns "file result — re-open with `read_file`"
    rather than a dead serialization. The file bytes stay in the stash,
    addressable by name.
  * **Failed tool results.** Already rendered to text by
    `_failed_tool_text` before they hit `messages`, so they persist and
    recall cleanly as-is.
  * **Retention.** Tool rows are write-once and never demoted, so a long
    session's `session_history` grows. Recall only ever reads the tail
    (`LIMIT`), so this is a storage/GC question, not a context one —
    punt to existing history retention.

## 7. Alternatives considered

  * **Index-then-fetch (two steps).** First call returns a cheap manifest
    (`[{ordinal, tool, when, size, snippet}]`); a second call fetches one
    ordinal in full. Strictly less bloat, but two tools / two round-trips
    — it fights the user's "keep it one simple argument" goal. The
    single truncating tool gets ~80% of the benefit; keep the manifest as
    a documented future extension if truncation proves too lossy.
  * **Keyword/semantic search over past results.** More powerful, much
    more surface (embedding or FTS over tool rows). Overkill for the
    "I summarised away a detail" case the count tool already covers.
  * **Widen the self-contained contract instead** (force the model to
    dump more into `AgentOutput.content`). Rejected: bloats *every* turn
    unconditionally — the opposite of paying only when needed.

## 8. Implementation plan (ordered)

1. **Persist tool rows.** Add a batch helper that walks the turn's
   `messages` and writes `tool_call`/`tool_result` rows
   (`incumbent=false`, `hidden=true`); call it where the terminal
   `assistant` row is written in `l1_common` and the orchestrator. Decide
   the `file_ref` serialization (§6).
2. **Query.** `queries.recent_tool_exchanges(...)` (§4.2) + a unit test
   asserting thread scoping and newest-first ordering.
3. **Tool.** `make_recall_tool_history_tool(...)` in `common/tools.py`
   with the three caps (§4.4); register it on l0 + l1 toolsets.
4. **Tests.** Round-trip: run a turn that calls a tool, start a new turn,
   call `recall_tool_history`, assert it returns the prior exchange
   truncated and excludes the current turn; assert clamp/budget behaviour.
5. **Docs.** Note the tool in `docs/agent-suite/sessions.md` §2.1 (the
   one exception to "tool rows never reloaded" — they re-enter only by
   explicit model request) and in the agent tool reference.
