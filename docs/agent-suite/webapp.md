# Agent Suite — Webapp Channel (design)

> A browser channel alongside the Telegram bot: log in, manage sessions,
> chat with live progress, configure settings/cron, delegate to specialists,
> and manage the file stash. **Status: shipped** (v1) — implemented in
> `bp_agents/agents/webapp/` and run as a first-class suite service. (This
> doc began as the design and still reads as the rationale of record.) It
> builds on the channel machinery in [`channel.md`](./channel.md),
> [`delegation.md`](./delegation.md), [`sessions.md`](./sessions.md), and the
> distributed session lock ([`sessions.md` §4](./sessions.md)).

## 1. Architecture

The webapp is a **new suite process** (compose service `webapp`, agent_id
`webapp`, group `channel`) wearing three hats:

1. **Channel agent** — a WS connection to the router (like the chatbot),
   used **only** for chat **task injection + progress**
   (`spawn_root_for_user(user_id, session_id)` /
   `await_root_result(on_progress=…)`).
2. **Web server** — FastAPI + Jinja2 + HTMX + Alpine + Tailwind (mirrors
   `bp_admin`), `SessionMiddleware` + CSRF, **SSE** for live progress.
3. **Direct clients** — the suite Postgres pool (read `session_info` /
   `user_config` / `cron_jobs` / history for display; delegation
   bookkeeping; suite-side purge cleanup) **and** a per-user router HTTP
   client carrying *the logged-in user's own token* (sessions lifecycle,
   files).

```
browser ⇄ (HTTPS + SSE) ⇄ webapp server ─┬─ agent WS ──▶ router  (inject turn, stream progress)
                                          ├─ user-token HTTP ▶ router  (/v1/sessions, /v1/files)
                                          └─ asyncpg ▶ bp_suite  (config, cron, delegation, history)
```

**Why a channel agent + browser server (not a browser-only SPA):** root-task
injection is agent-only (`spawn_root_for_user` rides the agent WS). A plain
user JWT can't inject. So the browser talks to the webapp server; the server
(a channel agent) injects on the user's behalf.

**Auth model (confirmed simple):** `admit_task` does **not** gate root
injection on `serviced_by` — it checks the channel is ACL-allowed
(`channel → l0`) and that `(user_id, session_id)` is a real, open, owned
session. And `/v1/sessions` + `/v1/files` are plain `require_authenticated`.
So **no service-principal/minting dance** (unlike the chatbot, which needs it
because Telegram users have no password): the user logs in and the webapp
holds *their* token.

**Concurrency:** webapp + chatbot share `SUITE_VALKEY_URL`, so the
**distributed per-session lock** serializes turns for one session across both
processes. The webapp is the reason that lock exists.

## 2. Decision 1 — channel-core refactor (chosen)

Today `ChatbotGateway` mixes transport (Telegram client, `chat_id` mapping,
`send_message`/`send_document`, poll offset) with channel logic
(`_dispatch_turn`, `delegated_to` maintenance, `/delegate`·`/undelegate`,
summarization, config/cron routing, the session lock, `send_named_file`).

Extract a **transport-agnostic `ChannelCore`** holding the logic, with a thin
**`Channel` frontend protocol** the transports implement:

```
ChannelCore(dispatcher, pool, session_locks, *, delegatable_agents, …)
  .handle_turn(user_id, session_id, text, attachments, *, on_progress, verbose) -> Reply
  .delegate(user_id, session_id, agent) / .undelegate(...)
  .update_delegation_from_result(...)            # §2 result-source maintenance
  # plus the helpers: _summarize_thread, _fold_back, _maybe_summarize

Channel (frontend) provides: identity (chat_id/login → user_id+session_id),
  send_text / send_file / send_progress, and the command surface.
```

- **Telegram frontend** = today's gateway minus the logic (poll → `handle_turn`; `send_*` via the Telegram client; identity via `platform_mappings`).
- **Web frontend** = HTTP handlers + SSE (identity via login session; `send_progress` → SSE; `send_file` → download chip).

This keeps **one source of truth** for delegation/summarization/locking. It
touches the chatbot (acceptable — it's behind tests), and is the bulk of the
webapp's reusable value. *Alternative considered:* a parallel web gateway
duplicating the logic — rejected (drift risk).

## 3. Auth / login

- **Login** (email + password) → `POST /v1/auth/login` → `TokenPair`, stored
  **server-side** in the signed session cookie (as `bp_admin` does); refresh
  via `/v1/auth/refresh`; logout clears it + `POST /v1/auth/logout`.
- HTTP ops (sessions, files, change-password) use the **user's token**.
- Task injection asserts `(user_id, session_id)`; the webapp only injects for
  sessions it listed with the user's token, so it can't reach another user's.
- Mobile: same responsive form.

### 3a. Self-service registration (`/register`)

A public, pre-auth + CSRF-exempt page (like `/login`/`/set-password`): the
visitor supplies email + a **chosen password** + name, which the webapp
proxies to the router's unauthenticated `POST /v1/registrations/public`
(per-IP + per-email rate-limited). The router stores the password **hash**
on the pending row (no email-delivery channel exists to send a reset link
to) and records **no service submitter**, so admin approval creates the user
with that password and grants **no `serviced_by`** — the user can sign in the
moment they're approved. Duplicate emails return the same neutral "request
received" outcome (enumeration-safe).

### 3b. Connecting a chat channel (later)

Web accounts have no email-based recovery, so the user is nudged (signup
disclaimer + a Sessions banner when no Telegram is linked) to connect a chat
channel **while signed in**. Settings → *Connect a chat channel* mints a
single-use token (`POST /v1/auth/link-tokens`, the user's own session is the
auth) that they paste into the bot's `/link`. The bot redeems it via
`POST /v1/auth/link-channel`, which binds the chat **and grants that channel's
service principal `serviced_by`** — enabling `/password` recovery from then on,
and (Telegram only) out-of-band scheduled-task notifications (§6). Linking
Telegram also promotes its session to the cron `default_session_id` when the
current default is a non-pushable webapp session.

## 4. Feature panes (server-rendered, HTMX-swapped)

**Session list (left panel)** — the **collapsible sidebar** (in `base.html`,
present on every authenticated page) hosts the list, loaded as an HTMX partial
(`GET /sidebar/sessions`, `hx-trigger="load, sessionsChanged from:body"`); the
full `/` page renders the same data as a table and self-refreshes on the same
event. Source: `GET /v1/sessions` (user token) enriched with `session_info`
(channel / `delegated_to` / `session_name`). Each row is labelled by its
**`session_name`** (falling back to the raw `session_id` when unset). The name
is **auto-generated from the first user message** — the channel fires a lite
`history_summarizer` (`session_name` mode) post-turn, once, and writes the
title — and **editable** on open rows via **Rename** (`POST …/rename`, the new
name carried in the `HX-Prompt` header). Rows are grouped **Open / Closed**:

  - **Open** — clickable (→ `/chat/{id}`), shows the **channel flag** (a
    **"Telegram"** badge for `channel=chatbot_telegram`), and a **Close**
    button **unless** Telegram-linked. A chatbot-owned session **can't be
    closed from the web app** (the handler returns **409**) — it's retired
    from the chatbot.
  - **Closed** — **not** clickable, no flag; exposes **Reopen**
    (`POST …/reopen` — clears `closed_at`, lands in the chat) and **Remove**
    (`DELETE …?purge=true` + suite-side cleanup of `session_history` /
    `session_info` / `cron_jobs`, which the router purge doesn't reach).
    Remove is **closed-only** (close first, then remove).

New (`POST /sessions`) opens a router session + `session_info` and lands in
the chat. Close/Remove reply `204` + `HX-Trigger: sessionsChanged` to refresh
the panel in place (no full navigation).

**Channel release on chatbot `/new`** — a Telegram session is "owned" by the
chatbot while open; the web app only flags it. When the user runs `/new` in
the chatbot, the gateway **closes the previous session** (router `DELETE`) and
**clears its `session_info.channel`** (now nullable) — *releasing* it. Once
released the row is a plain closed session the web app can **Reopen** or
**Remove** (and a reopened one is webapp-controllable, so closable). This is
the only path that closes a Telegram session.

  - *"progress won't show in chatbot":* whoever injects+awaits a task
    receives its progress/result. A Telegram-origin session continued in the
    web app runs there, and Telegram won't mirror it — the flag is the UX
    warning.

**Main chat pane** — history from `bp_suite` (`reload_incumbent` for the
active thread: orchestrator or current delegate) + input. Send → HTMX
`POST /chat` → `ChannelCore.handle_turn` under the session lock → "pending"
bubble + an **SSE** stream.

**Progress UX (SSE)** — the `on_progress` callback forwards each
`LoopProgress` over SSE; render a **collapsible activity strip** above the
final bubble, reusing the existing rendering: `💭 Thinking…`,
`[Tool] knowledge_base (…)`, `Delegating to a specialist…`, the
`[<Specialist> Agent]` tag — styled rows, not plain text. The terminal
`ResultFrame` closes the stream and renders the answer (tagged when
delegated); `output.files` render as download chips.

**Delegation control** — a **dropdown** of `delegatable_agents` + a
**"Return to assistant"** button → `ChannelCore.delegate/undelegate` (the
deterministic path, [delegation.md §6 (b)]). A persistent **status badge**
("Talking to: Research Agent" / "Main assistant") from
`session_info.delegated_to`.

**Memory pane** (`/memory`) and **Knowledge base pane** (`/knowledge`) —
unlike config/cron (suite-DB forms), memory and the KB live in **per-user
LanceDB owned by their agents**, so these panes **dispatch to the agent** and
render the JSON `AgentOutput` (new `tool:false` modes: `memory.list/delete/
manual_add`, `knowledge_base.browse/delete`). The dispatch goes through a
generic `ChannelCore.call_agent(dest, mode, payload)`. Since memory/KB are
per-**user** but root-task admit needs an **open** session, the page rides a
**carrier session** — `carrier_session()` picks the user's
`default_session_id` if open, else the newest open session; with none open the
pane shows an empty state ("Start a conversation…"). Reaching these agents is
**capability-scoped**: the webapp agent carries `database.*` (KB, via the
existing `*/database.* → l3/database.*` rule) and `memory.*` (memory, reached
via the `channel/* → l3/memory.add` rule it already holds) — so the chatbot,
also `channel`, gains no KB access ([acl.md](./acl.md) §3, §6). ACL is
**agent-level**, so reaching an agent authorizes all of its modes.

- **Memory** — paged, filterable by `kind`, searchable. No query → newest
  first (`last_used_at`); query → ranked by the retrieval formula. A **manual
  add** form (fact + kind) bypasses extraction but still reconciles; a
  **delete** per row (by `uid`). Mutations fire `memoryChanged` to refresh.
- **Knowledge base** — paged, recency-sorted, filterable by title query /
  collection / tag; **delete** per row (by title + collection). Documents are
  added via chat / file upload, not here. Mutations fire `knowledgeChanged`.

## 5. Decision 2 — config & cron as structured forms (chosen)

The webapp is a suite process with the `bp_suite` pool, so the **panes are
structured forms over the DB**, not NL round-trips:

- **Config pane** — read `user_config` directly; write the editable fields
  via `queries.update_user_config` with the **same validation as the config
  agent's `set_config`** (factored into the shared `bp_agents.config_edit`
  helper so the form and the agent agree). The chat pane still handles NL
  ("change my timezone"). The LLM-tier preset fields (`preset_pro` /
  `preset_balanced` / `preset_lite`) are **opt-in and tier-gated**: each
  renders as a `<select>` only when the operator configures a non-empty
  allow-list for that tier (`SuiteSettings.selectable_presets_*`), and a
  submitted value must be one of those names — the same gate the config
  agent applies. With no allow-list the tier stays system-managed and hidden.
- **Cron pane** — list/add/remove over `cron_jobs` (croniter-validated),
  reusing `bp_agents/cron_manage.py` helpers (factor the add/remove/validate
  out of the LLM toolset so the form calls the same code).

Forms give immediate, deterministic UX; the agents stay the NL path.

## 6. Decision 3 — cron delivery via channel-agnostic routing (landed)

Cron **firing** runs in the chatbot's scheduler. A webapp-session cron now
delivers via **channel-agnostic routing** ([cron.md §6]): the result row is
persisted to the webapp session (the canonical record), and — since the
scheduler can't push to a browser — a **pointer nudge** is routed to the
user's Telegram mapping so they know to open the webapp. A user with no
Telegram mapping sees the result on their next webapp visit (the row is the
record). A native webapp push primitive (notification table / web-push)
would slot in later as another reachable channel; until then Telegram is the
out-of-band carrier. (Telegram-session cron — full content to the chat — is
unchanged.)

## 7. File stash pane

List session + persistent stash; **upload** (`POST /v1/files` + name-bind,
user token) and **download** (resolve → stream `GET /v1/files/{id}`),
reusing the `ChannelCredentials` file methods (now with the user's token).
Tabs for `session:` vs `persist/`; size/quota display.

## 8. Design language & mobile

Mirror `bp_admin`: Tailwind (Play CDN → built CSS for prod), HTMX for pane
swaps, Alpine for local interactivity (dropdowns, collapsibles), a shared
`base.html`, `viewport` meta. Layout: sidebar (sessions) + main pane; on
mobile the sidebar collapses to a drawer and the chat goes full-width.

## 9. New surface

- **Done (router):** `DELETE /v1/sessions/{id}?purge=true` (the only router
  change). *Confirm `list_sessions` returns `channel`* (for the badge) — add
  it to `SessionView` if missing.
- **Suite:** `ChannelCore` extraction; shared config-validation + cron
  helpers; `webapp` agent + onboarding/invitation; a `[webapp]` extra
  (fastapi/uvicorn/jinja2/itsdangerous, like `admin`); compose service.
- **Suite-side session purge cleanup** (called by the webapp's "remove").

## 10. Phasing

1. `ChannelCore` extraction (refactor; Telegram still green).
2. Webapp skeleton: process (agent WS + FastAPI), login, base layout,
   session list (read-only) + badges.
3. Chat pane + SSE progress (the core).
4. Delegation control + status; file stash pane.
5. Config + cron structured panes.
6. Session new/close/remove (remove = purge + suite cleanup).
7. Polish: mobile, the chatbot-session flag/notes, prod CSS build.

## 11. Non-goals / open items

- Multi-instance webapp horizontal scale beyond the per-session lock (router
  WS plane is single-process — [deferred-work.md]).
- Cron delivery to webapp sessions (Decision 3 — deferred to [cron.md §6]).
- Real-time push of *unsolicited* messages (cron results, proactive) to an
  open webapp tab — needs the channel-agnostic routing + an SSE/notification
  channel; later.
- Hard "remove" is irreversible; the UI must confirm.
