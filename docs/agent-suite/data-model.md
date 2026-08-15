# Agent Suite — Data Model

> Consolidated schema reference. The suite keeps its own **Postgres**
> (cron, chat mappings, and two per-user pointers) and **per-user LanceDB**
> (knowledge + memory), joined to the platform only by `user_id` /
> `session_id`. The conversation itself is the ROUTER's (§1.1), and so are
> the user's settings (§1.2). Types are indicative; tune for your DB.

## 1. Postgres

### 1.1 The conversation is NOT here

`session_info` and `session_history` are gone — dropped by migration
`0005_drop_session_tables`, and since the chain was consolidated they are
simply absent from `0001_suite_initial`. Conversation, its rolling summaries,
`delegated_to`, and a session's channel/title all live in the router's
session store — see [sessions.md §1](./sessions.md) for the mapping and
[`../design/router-managed-session-store.md`](../design/router-managed-session-store.md)
for why. What follows is what the suite still keeps.

### 1.2 The user's settings are NOT here either

`full_name`, `timezone`, `language`, `verbose_default`, `custom_note` and
`max_context_token_limit` are gone from `user_config` (dropped by migration
`0006_user_config_to_user_scope`, now simply absent from the consolidated
`0001_suite_initial`). They are keys in the router's
**user-scoped state** — the `(user_id, key)` namespace the session store
exposes under `scope="user"` — reached through `bp_agents/user_prefs.py`.
Reading them no longer needs a database credential, which is what let the l1
specialists and the orchestrator drop their suite pools entirely.

The split was by **reader**, not by kind:

| where it is read | destination |
| --- | --- |
| inside a task (turn start, to build a system prompt), or by a steward holding a carrier session | **router user scope** |
| outside any task — the sandbox host, the cron scheduler | stays in `user_config` |

### 1.3 `user_config` — one row per user

| Column | Type | Notes |
| --- | --- | --- |
| `user_id` | text PK | |
| `sandbox_uid` | int | maps to the container uid / `/home/{user_id}`; also needs cross-user uniqueness, which a per-key namespace does not give |
| `default_session_id` | text null | cron fallback target ([cron.md](./cron.md)) |

Two fields wide, and that is the point: what is left is exactly what cannot
be read through `ctx.history` or a carrier session.

**No model choice lives here.** Four `preset_*` columns did until migration
`0004_drop_user_config_presets` (likewise absent from the consolidated
baseline); which model a user runs on is now a router
preset **slot** ([`../design/router-resolved-preset-slots.md`]) — the agent
names an opaque slot (`pro` / `balanced` / `lite`, `bp_agents/slots.py`) and
the router resolves it from the user's own preference intersected with their
tier gate. The preference is stored router-side in `user_llm_preferences` and
written only under the user's session JWT, because the router *acts* on it.
That is the same test the settings above pass and the presets fail: user
scope is writable by any agent in the session, so it holds values the router
merely *stores*, never one it *obeys*. The embedding preset is operator
configuration (`SUITE_DEFAULT_PRESET_EMBEDDING`) and deliberately not a slot
— changing it invalidates every vector already written.

### 1.4 `cron_jobs`

| Column | Type | Notes |
| --- | --- | --- |
| `cron_id` | text PK | uid |
| `user_id` | text, indexed | |
| `session_id` | text | landing session (falls back to `default_session_id`) |
| `cron_expression` | text | standard cron |
| `timezone` | text | DST-aware evaluation |
| `report` | enum(`always`,`never`,`case_by_case`) | default `case_by_case` |
| `cron_message` | text | the scheduled prompt |
| `status` | enum(`active`,`inactive`) | |
| `execute_until` | timestamptz null | expiry |
| `created_at` | timestamptz | |
| `last_executed_at` | timestamptz null | **atomic-claim** column ([cron.md §1](./cron.md)) |

### 1.5 `cron_executions` — fire log (audit + "why no ping?")

| Column | Type | Notes |
| --- | --- | --- |
| `id` | bigserial PK | |
| `cron_id` | text, indexed | |
| `user_id` | text | |
| `session_id` | text | resolved target |
| `fired_at` | timestamptz | |
| `reported` | bool | effective_report |
| `reason` | text null | the LLM's reason |
| `message` | text null | sent text (if reported) |
| `error` | text null | populated on C3 failure |

### 1.6 `suite_platform_mappings` — inbound identity (the entry point)

Maps a channel-native chat to a Backplaned user; populated by the **admin approve-registration** flow ([overview §2.1](./overview.md)). Identity resolution is `chat_id → user_id → the chat's own session_id` (falling back to `user_config.default_session_id` — the cron fallback — only until the chat has a session of its own).

| Column | Type | Notes |
| --- | --- | --- |
| `platform` | enum(`telegram`,`web`,`kakao`) | channel kind |
| `chat_id` | text | channel-native chat id |
| `user_id` | text, indexed | resolved end-user |
| `session_id` | text null | the chat's CURRENT live session (its own conversation). Seeded at registration; rotated by `/new`; copied onto `default_session_id` by `/setdefault`. NULL ⇒ fall back to `default_session_id` |
| `created_at` | timestamptz | |

PK `(platform, chat_id)`; reverse index on `user_id`. An unmapped `(platform, chat_id)` ⇒ the `/register` prompt ([channel.md §2](./channel.md)). A user with several chats (e.g. Telegram + KakaoTalk via `/link`) has one row per chat, each with its **own** `session_id`, so the conversations don't interleave — they share only the account (memory/files) and the cron-fallback `default_session_id`.

## 2. Per-user LanceDB

One logical store per user (separate db / `user_id`-partitioned). Resolved from the authoritative `user_id` (derived from the task — never asserted).

### 2.1 Knowledge base

**`documents`** (metadata): `doc_id`, `collection` (default `default`), `title`, `tags: list<text>`, `description`, `sha256` (content-addressed dedup), `source_name`, `created_at`, `updated_at`.

**`chunks`** (searchable unit): `chunk_id`, `doc_id`, `collection`, `title`, `tags: list<text>` (denormalized for filter), `chunk_index`, `content: text`, `embedding: vector`. Hybrid index = vector + BM25 over `content`.

**Chunking** (all docs are Markdown first): env-configurable `max_chunk_len`=2000, `min_chunk_len`=1000, `overlap_len`=100; split by the fallback chain header → double-newline → newline → sentence → word → character, within `[min,max]`.

### 2.2 Memory ([memory.md](./memory.md))

**`facts`**: `uid`, `fact: text`, `kind` enum(`preference`,`personal_info`,`event`,`project`), `created_at`, `last_used_at`, `embedding: vector`. Hybrid index = vector + BM25 over `fact`.

**`edges`** (the relation set): `uid_a`, `uid_b` (stored with `uid_a < uid_b` so each undirected edge is one row), `created_at`. Neighbors of X = rows where `uid_a=X OR uid_b=X`; remove X = delete those rows; degree cap ≤10 per fact.

## 3. Metadata conventions (`AgentOutput` + `ProgressFrame`)

`AgentOutput` is the universal result type ([overview §6](./overview.md)). Reserved metadata keys:

| Key | On | Producer | Consumer |
| --- | --- | --- | --- |
| `context_tokens: int` | `AgentOutput` | every agent (measured while building context) | the channel's post-turn summarization check ([sessions.md §3](./sessions.md)) |
| `report: bool`, `reason: str` | `AgentOutput` | `orchestrator.cron_message` | the cron apply step ([cron.md §2](./cron.md)) |
| `LoopProgress` (structured) | `ProgressFrame` | any agent's loop | the channel's verbose `on_progress` → one message per frame ([channel.md §5](./channel.md)) |

No mode defines a bespoke `produces_schema`; all outputs validate as `AgentOutput`.

## 4. State-ownership summary

| Store | Writer(s) |
| --- | --- |
| router session store — threads | each agent, its OWN thread only; the router stamps the owner from the task's active executor, so another agent's thread is unwritable rather than merely forbidden |
| router session store — session state / metadata | the channel, as a steward under the user's token |
| router session store — USER state (settings) | the `config` agent in-task; the webapp settings form and the chatbot `/config` as stewards. Shared, not per-agent — see §1.2 |
| `user_config` | the channel (`default_session_id`); the sandbox host (`sandbox_uid`) |
| `cron_jobs` / `cron_executions` | chatbot (`cron` mode + scheduler) |
| KB LanceDB | `knowledge_base` |
| memory LanceDB | `memory` (per-user lock) |
| router file store | any agent, for its own task scope (platform-gated) |
| `suite_platform_mappings` | the admin approve-registration flow |

**File model** ([overview §2.4](./overview.md)): the named store everywhere except the sandbox's container workspace. A gateway channel (no `ctx.files`) uses the **session-authed named-store endpoints** (`POST`/`GET /v1/files/names[/resolve]` — [`../design/router-managed-file-store.md` §6](../design/router-managed-file-store.md)) under its per-user session JWT; the sandbox bridges via `stash_to_workspace` / `workspace_to_stash`.
