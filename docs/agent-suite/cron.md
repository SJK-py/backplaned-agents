# Agent Suite — Cron Execution

> Scheduled jobs: the scheduler, the execution path, the report/spam-control
> logic, and routing. Job + log schemas are in [`data-model.md`](./data-model.md).
> Management (creating/editing jobs) is the **config agent's** `cron` mode
> ([agents.md](./agents.md)) — the channel can't host it (the router denies
> an agent invoking itself), so `/cron` is a normal `channel → config` call.
> The scheduler still lives in the chatbot; the two share the `cron_jobs` table.

## 1. The scheduler

Nothing in the platform fires crons — the suite owns a scheduler. **v1:** a background loop in the chatbot. **v2:** a channel-agnostic component (see §6).

- **Tick + claim.** Poll at cron resolution (every minute). For each `active` job with `now ≤ execute_until` whose `cron_expression` is due in its **`timezone`** (DST-aware cron lib), fire it. Multi-worker safety: **atomic claim** —
  `UPDATE cron_jobs SET last_executed_at = :now WHERE cron_id = :id AND last_executed_at < :due RETURNING …`.
  Only one worker wins; no double-fire.
- **Missed firings (downtime).** On restart, fire **at most one** catch-up (or skip to next) — never replay the whole gap. Bounded by `last_executed_at` / `execute_until`.
- **Expiry.** Past `execute_until` → set `status = inactive` and stop.

## 2. Execution path

1. **Resolve the target session** (where the result lands + which channel/`chat_id` to send to): `job.session_id` if open → else the user's `default_session_id` → else a terminal fallback (open a fresh session under the user, move the default pointer there). The cron needs a session only for *landing*, not for context — config/presets come from `user_id`.
2. **Run under the end-user's identity** (`serviced_by` mint), like any dispatch.
3. **Run `orchestrator(cron_message, {prompt: job.cron_message})` _outside_ the session queue.** It builds a **fresh** context — the cron system prompt + user-config (name/tz/note) but **no session history** — and runs the full orchestrator loop (it keeps the toolset: spawn subagents, retrieve memory, etc.). Its intermediate steps are **not** written to the conversation thread. It **never delegates** and **bypasses `delegated_to`** (a cron is not a user message). It terminates with structured output → `AgentOutput(content=message, metadata={report, reason})`.
4. **Apply (a brief session-queue op).** Only this step touches `session_history`, so only this is serialized:
   - **`effective_report`** = `job.report=="always" → true` / `"never" → false` / `"case-by-case" → metadata.report`.
   - **report ⇒** append an assistant conversation row to the **main** thread (`incumbent=T, hidden=F`) and send `message` to the session's `channel`+`chat_id`.
   - **no report ⇒** write a `cron_executions` log row (with `reason`) and drop the message — no conversation row, no context pollution.
5. **Update the job:** `last_executed_at` (claimed in §1); flip `status=inactive` if now past `execute_until`. Always write a `cron_executions` row (fired/report/reason/error) for audit.

> Running the **loop outside the queue** and queuing only the **apply** mirrors summarization: a heavy cron (e.g. a research digest) never blocks the user's next message.

## 3. Report policy (spam control)

The structured `report` bool is the anti-spam mechanism: a job like "tell me if there's important news" emits `report=false` (logged, dropped) most firings and `report=true` only when warranted. The per-job override:

| `job.report` | Effect |
| --- | --- |
| `always` | always send (e.g. a daily reminder) |
| `never` | never notify (background maintenance) |
| `case-by-case` (default) | the LLM's `report` decides |

## 4. Session resolution & the default pointer

`default_session_id` is a per-user pointer (user-config); its initial value is the session opened at registration approval ([overview §2.1](./overview.md)). The chatbot's add-cron tool stamps `session_id = current session`. On `/new` **from the default session**, the default pointer **transfers to the sequel session**; existing jobs keep their original `session_id` and **fall back to the default** when that session later closes (the resolution chain in §2). So a job created in a now-closed session still surfaces in the user's current conversation.

## 5. Failure modes

| # | Failure | Rule |
| --- | --- | --- |
| C1 | scheduler downtime → missed ticks | fire ≤1 catch-up, don't replay the gap |
| C2 | multi-worker double-fire | atomic `UPDATE … WHERE last_executed_at < due RETURNING` claim |
| C3 | `cron_message` task errors | log the error, **mark executed anyway** (no retry-storm), send nothing |
| C4 | both job + default sessions closed | open a fresh session (move the default pointer), route there |
| C5 | report=true but the channel send fails | the conversation row is still appended (user sees it next open); log the send failure |
| C6 | cron fires mid-conversation | the **apply** op serializes with the user's turns — the reminder interleaves correctly |

## 6. v2 — channel-agnostic routing

"Non-chatbot session → notify via the default channel" only matters once `webapp` exists. If the resolved session's `channel ≠` the channel that can reach the user now, the scheduler sends a "your scheduled task ran in {channel}" nudge via the user's **default reachable** channel. For v1 (Telegram-only) every session is Telegram-sendable, so this is moot — but it's why the scheduler should become channel-agnostic in v2: **fire → resolve the user's reachable channel → route**.
