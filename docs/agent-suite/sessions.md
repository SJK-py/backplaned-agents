# Agent Suite — Session Model, History & Summarization

> The suite's conversation store and how an agent's LLM context is built,
> kept bounded, and serialized. Schemas are consolidated in
> [`data-model.md`](./data-model.md). Delegation interactions are in
> [`delegation.md`](./delegation.md).

## 1. Session-info (one row per `session_id`)

Owned and written **only** by the channel/session-manager (`session.management`).

| Field | Meaning |
| --- | --- |
| `session_id` | the router session id (join key) |
| `channel` | `chatbot_telegram` \| `webapp` |
| `chat_id` | channel-native id (e.g. Telegram chat) for outbound sends |
| `delegated_to` | `null` \| `agent_id` of the active delegate (channel-maintained — see [delegation.md](./delegation.md)) |
| `history_summary` | rolling summary of the **main** (orchestrator) thread |
| `delegate_summary` | rolling summary of the **current delegation** thread |

> `delegate_prompt` is **not** a session-info field — it's the **seed history row** of the delegate thread (see §6).

## 2. Session-history (the conversation log)

| Field | Notes |
| --- | --- |
| `session_id` | |
| `agent_id` | which agent this row belongs to — **set on user rows too** (= the agent the message was sent to). This is what gives each agent its own thread. |
| `role` | `user` \| `assistant` \| `tool_call` \| `tool_result` |
| `message` | content (assistant rows = the terminal `AgentOutput.content`) |
| `created_at` | |
| `incumbent` | include in the LLM-context reload |
| `hidden` | render in the webapp |

**Write ownership is split:** the **channel is the sole writer of `user` turns** (one `append_user` per inbound message, stored **verbatim — no timestamp prefix**, which would pollute prompt-cache hit rates and litter the transcript); each **agent writes only its own `assistant` turn** (and its render/audit-only `tool_*` rows). The terminal `AgentOutput` is the canonical assistant row; produced **files** stay in the stash addressable by name (a later turn `llm_ref`s them). An agent that needs the wall clock calls the **`current_time` tool** registered on every l0/l1 agent — the clock is never baked into stored history.

### 2.1 Reload (building an agent's context at task start)

```
context = system_prompt(general + user-config note + active summary)
        + [ rows WHERE agent_id = self
                  AND incumbent = true
                  AND role ∈ {user, assistant} ]  ordered by created_at
        + current input
```

`tool_call` / `tool_result` rows are **never reloaded** — the live loop holds the full tool sequence in memory; the persisted tool rows exist for render + audit. **Therefore the terminal `AgentOutput.content` must be self-contained** for the next turn (or surface the outcome as a named file).

> **One opt-in exception — `recall_tool_history`.** Stateful turns persist their `tool_call`/`tool_result` rows (`incumbent=false`, `hidden=true`) via `common.tool_history.persist_tool_exchanges`. They still never enter the automatic reload; instead the orchestrator and delegated l1 turns carry a `recall_tool_history(count, skip)` local tool the model calls to re-read its OWN thread's earlier tool results on demand — paging back with `skip`, capped per-result and in total so recall can't re-bloat context. The default stays "self-contained content"; recall is the escape hatch when a prior detail wasn't carried forward. See [`../design/agent-tool-history-recall.md`](../design/agent-tool-history-recall.md).

### 2.2 The `(incumbent, hidden)` matrix

`incumbent` is a firm rule; `hidden` is a UX choice where marked `*`.

| Row | `incumbent` | `hidden` |
| --- | --- | --- |
| user message | T | F |
| terminal assistant reply | T | F |
| intermediate narration (text before a tool call) | F | F* |
| `tool_call` / `tool_result` | F | T* (F to show activity) |
| summarized-out turns | F | F |
| "user-attached file saved as {name}" | T | T |
| `end_delegation` recap (into main thread) | T | T |

## 3. Rolling summarization

Summaries live in **session-info** (`history_summary`, `delegate_summary`), are **channel-written**, and are **rendered into the system prompt** — not placed as message rows (so no chronology issue) and not duplicated as history rows (the full history is the durable record). One incumbent summary per thread at a time.

### 3.1 Trigger + flow (queued, post-turn)

1. After a turn ships its result, the channel reads `AgentOutput.metadata.context_tokens` (the agent measured its context while building it). If over the **soft** `max_context_token_limit` for the relevant `agent_id` thread → enqueue a summarize op in that session's queue.
2. The summarize op computes the cutoff (oldest ~70% of the thread's incumbent turns) and calls `history_summarizer.summarize_incumbent(agent_id, up_to=<cutoff>, previous_summary)`. The summarizer is **read-only**; it returns `AgentOutput(content=<summary>)`.
3. The channel applies it: write the new summary into session-info and set `incumbent=false` on the rows `≤ cutoff`.
4. A user message arriving **during** the summarize op waits behind it in the queue (acceptable — humans spend think-time reading the prior reply). No pending-state machine.

### 3.2 Hard-limit guard

A message that was queued **ahead** of a summarize op runs against the un-summarized (over-soft-limit) context. Keep `max_context_token_limit` with headroom below the provider's real window; if a turn would breach the **hard** window, summarize **inline (blocking)** for just that turn. So the system is proactive in the common case and degrades to reactive only at the true ceiling.

### 3.3 Delegation parity

The same machinery runs on the `agent_id=<delegate>` thread after a `delegated_message` turn, writing `delegate_summary`. Because the **channel** does it, the delegate needs only `session.history`.

## 4. Per-session serialization (the queue)

The channel maintains a **per-`session_id` FIFO queue**. Serialized ops: message turns and summarization. One in-flight op per session.

- **Why:** the router serializes per *task*, never per *session*. Without this, two turns' history appends interleave and summarization's `incumbent` flips race.
- **Scope:** an op spans dispatch → agent runs (and writes its turn rows) → result → channel writes. No other op for that session overlaps, so all reads see a consistent snapshot and all writes are exclusive.
- **Out of the queue:** `memory.add` (per-user, separate store — queuing it would block the next message behind a multi-LLM extraction). Cron's agent **loop** also runs outside the queue; only its apply step is queued ([cron.md](./cron.md)).
- **Multi-worker:** the lock is an in-process `asyncio.Lock` by default (serializes within one channel process). Set **`SUITE_VALKEY_URL`** and it becomes a **distributed lock** (`bp_agents/session_lock.py`: local lock for in-process FIFO + a Valkey `SET NX PX` with a renewal watchdog), so a second channel instance (e.g. a webapp alongside the Telegram bot) serializes turns for the same session across processes. Valkey errors fail open to local-only. Without it, two instances would process one session concurrently and the races return — so today's single Telegram instance is correct as-is.

## 5. System-prompt composition

- **Orchestrator (`message`):** general instruction + user-config note (name, timezone, custom note) + `history_summary`.
- **Delegation (`on_delegation` / `delegated_message`):** general delegation instruction + agent-specific instruction + user note (if needed) + **`delegate_prompt`** (the seed row, §6) + `delegate_summary`.

## 6. `delegate_prompt` as a seed history row

At hand-off the orchestrator generates the delegation context+instruction but no longer holds `session.management`, so `delegate_prompt` is **not** a session-info field. Instead the orchestrator writes it as the **first row of the delegate thread** (`agent_id=<delegate>`, a designated delegation-context row, `incumbent=true`) via `session.history`. The delegate's context-build reads it and renders it into the delegation system prompt each turn. On `end_delegation`, this seed row is flipped `incumbent=false` along with the rest of the episode ([delegation.md](./delegation.md)).

## 7. User-config (one row per `user_id`)

Holds: full name; timezone; **pro / balanced / lite** chat presets (deep_reasoning / orchestrator / summarizer-memory-knowledge respectively); a separate **embedding preset**; `max_context_token_limit`; verbose mode; language preference; sandbox `uid`; `default_session_id` ([cron.md](./cron.md)); custom note. Full schema in [`data-model.md`](./data-model.md).
