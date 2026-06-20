# Agent Suite — Cron Execution

> Scheduled jobs: the scheduler, the execution path, the report/spam-control
> logic, and routing. Job + log schemas are in [`data-model.md`](./data-model.md).
> Management (creating/editing jobs) is the **config agent's** `cron` mode
> ([agents.md](./agents.md)), reachable two ways: the channel's `/cron`
> command spawns it directly, and — since the mode is tool-visible — the
> **orchestrator** sets reminders conversationally via `call_config_cron`
> ("remind me at 8am"). (It lives on config, not the chatbot, because the
> router denies an agent invoking itself.) The scheduler still lives in the
> chatbot; the two share the `cron_jobs` table.

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

## 6. Channel-agnostic routing

The **assistant result row is the canonical record** — it's appended to the target session's history (the apply step, §2) regardless of which channel, if any, can deliver a live notification. Live delivery is the channel-specific part:

- **Target session is Telegram-reachable** (`channel=chatbot_telegram`, has `chat_id`) → the scheduler sends the full result (text + files) to that chat, as before.
- **Target session is NOT live-reachable by the scheduler** (a `webapp` or released session — no Telegram `chat_id`) → the result is already persisted there; the scheduler routes a **pointer nudge** ("⏰ A scheduled task just ran in your web app. Open it to see the result.") to the user's **Telegram** mapping (`list_platform_mappings_for_user(user_id, platform="telegram")`), so they know to open the session. The nudge carries no content — the full result lives in the webapp thread.
- **No out-of-band channel** (webapp session, user has no Telegram mapping) → the persisted row is the record; the user sees it on their next webapp visit. Nothing is sent (no crash, no spurious delivery).

This is **fire → persist → resolve the user's reachable channel → route the nudge**. The scheduler still runs in the chatbot process and holds the Telegram client; Telegram is currently the only channel with out-of-band push (the webapp delivers via SSE only while a tab is open), so it's the nudge carrier. A future webapp push primitive (notification table / web-push) would slot in as an additional reachable channel without changing this split.

> **Web-first accounts:** a user who registered via the webapp (§3a of [webapp.md]) has no Telegram mapping, so scheduled tasks run but produce no out-of-band notification — the persisted row is the only record until they next open the web app. Linking Telegram (§3b) is what turns notifications on: the link grants `serviced_by` and, when the current `default_session_id` is a non-pushable webapp session, **promotes the new Telegram session to the default** so cron results are delivered in full rather than only nudged. KakaoTalk linking does **not** enable notifications (no push) — it only restores `/password` recovery.
