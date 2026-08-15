# Agent Suite — Data Model

> Consolidated schema reference. The suite keeps its own **Postgres**
> (per-user config / cron / chat mappings) and **per-user LanceDB**
> (knowledge + memory), joined to the platform only by `user_id` /
> `session_id`. The conversation itself is the ROUTER's (§1.1). Types are
> indicative; tune for your DB.

## 1. Postgres

### 1.1 The conversation is NOT here

`session_info` and `session_history` are gone (migration
`0005_drop_session_tables`). Conversation, its rolling summaries,
`delegated_to`, and a session's channel/title all live in the router's
session store — see [sessions.md §1](./sessions.md) for the mapping and
[`../design/router-managed-session-store.md`](../design/router-managed-session-store.md)
for why. What follows is what the suite still keeps.

### 1.3 `user_config` — one row per user

| Column | Type | Notes |
| --- | --- | --- |
| `user_id` | text PK | |
| `full_name` | text | |
| `timezone` | text | IANA tz |
| `max_context_token_limit` | int | soft summarization trigger |
| `verbose_default` | bool | default verbose mode ([channel.md](./channel.md) §5); `verbose` is a reserved word in Postgres |
| `language` | text | preference |
| `sandbox_uid` | int | maps to the container uid / `/home/{user_id}` |
| `default_session_id` | text null | cron fallback target ([cron.md](./cron.md)) |
| `custom_note` | text | injected into system prompts |

**No model choice lives here.** Four `preset_*` columns did until migration
`0004_drop_user_config_presets`; which model a user runs on is now a router
preset **slot** ([`../design/router-resolved-preset-slots.md`]) — the agent
names an opaque slot (`pro` / `balanced` / `lite`, `bp_agents/slots.py`) and
the router resolves it from the user's own preference intersected with their
tier gate. The preference is stored router-side in `user_llm_preferences` and
written only under the user's session JWT, because the router *acts* on it;
`user_config` is agent-writable and therefore the wrong home for a value that
gates spend. The embedding preset is operator configuration
(`SUITE_DEFAULT_PRESET_EMBEDDING`) and deliberately not a slot — changing it
invalidates every vector already written.

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
| `user_config` | `config` agent (+ channel for `default_session_id`) |
| `cron_jobs` / `cron_executions` | chatbot (`cron` mode + scheduler) |
| KB LanceDB | `knowledge_base` |
| memory LanceDB | `memory` (per-user lock) |
| router file store | any agent, for its own task scope (platform-gated) |
| `suite_platform_mappings` | the admin approve-registration flow |

**File model** ([overview §2.4](./overview.md)): the named store everywhere except the sandbox's container workspace. A gateway channel (no `ctx.files`) uses the **session-authed named-store endpoints** (`POST`/`GET /v1/files/names[/resolve]` — [`../design/router-managed-file-store.md` §6](../design/router-managed-file-store.md)) under its per-user session JWT; the sandbox bridges via `stash_to_workspace` / `workspace_to_stash`.
