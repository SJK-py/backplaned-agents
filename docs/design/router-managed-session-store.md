# Router-managed session store

> **Status:** design proposal — not implemented. Nothing in this document
> has landed; the suite still owns `session_info` / `session_history` in its
> own Postgres (`bp_suite`).

Make the conversation store a **first-class router service**, the way the
named file store already is: agents append turns, reload their thread, and
read/write session state over typed frames whose `(user_id, session_id)`
scope the **router derives** from the task row — never from an
agent-asserted field. The suite stops opening its own Postgres pool for
session data; `bp_suite` shrinks to the things that are genuinely suite
policy (cron, channel identity mappings, LanceDB).

This is the same move `docs/design/router-managed-file-store.md` made for
files, and it reuses that design's primitives verbatim
(`attachments.derive_task_file_scope`, the `FileResult`-style correlated
reply, the session-JWT HTTP path for gateway agents, the
session-close/purge GC hooks).

## 1. The gap today

The suite keeps a **second Postgres database** (`bp_suite`, created in
`deploy/postgres-init/01-create-suite-db.sql:19`, distinct owner) holding
`session_info`, `session_history`, `user_config`, `cron_jobs`,
`cron_executions`, `suite_platform_mappings`. Sessions themselves live in
the router (`sessions`, `bp_router/db/models.py:75`), joined only by
`session_id`. Six concrete costs follow from that seam:

**1.1 Ten of twelve agents hold Postgres credentials.** `open_pool` is
called at agent startup in `orchestrator/agent.py:125`,
`config/agent.py:165`, `research/agent.py:121`,
`deep_reasoning/agent.py:122`, `knowledge_base/agent.py:116`,
`chatbot/agent.py:126`, `computer_use/agent.py:88`, `webapp/agent.py:87`,
`history_summarizer/agent.py:96`, `memory/agent.py:184`. Every one of
them ships `SUITE_DATABASE_URL` and a copy of the schema knowledge. The
platform's whole premise is that an agent holds no infrastructure
credentials — it holds no provider keys and no object-store keys, but it
does hold a database password.

**1.2 `session.history` is a decorative capability.** Seven agents
advertise it (`orchestrator/agent.py:110`, `research/agent.py:108`,
`deep_reasoning/agent.py:110`, `chatbot/agent.py:61`,
`computer_use/agent.py:76`, `webapp/agent.py:47`,
`history_summarizer/agent.py:84`) and **nothing enforces it**, because
history access is raw SQL. `session_history.agent_id` — the
thread key — is whatever the writer types. Any agent with the DSN can
append an `assistant` row to any other agent's thread in any session, for
any user, and no record of who actually wrote it survives. Contrast the
file path: `_handle_file_store` (`bp_router/dispatch.py:1403`) resolves
identity through `attachments.derive_task_file_scope`
(`bp_router/attachments.py:25`) — "the authoritative `(user_id,
session_id)` for `task_id` **iff** `agent_id` is the task's active
executor."

**1.3 GC and erasure are two-sided.** The router's closed-session sweep
hard-deletes its own rows (`closed_session_retention_days`,
`bp_router/settings.py:315`) but cannot reach `bp_suite`, so the suite runs
a mirror reaper (`bp_agents/agents/chatbot/session_gc.py`) on the *same*
default retention (`session_gc_retention_days`, `bp_agents/settings.py:261`)
that asks the router which sessions still exist via a bespoke admin probe,
`GET /v1/admin/sessions/filter-existing`
(`bp_agents/agents/chatbot/credentials.py:290`). The webapp's "remove
session" needs two purges (`bp_router` `purge_session` **and**
`queries.purge_session_suite_data`, `bp_agents/db/queries.py:43`). GDPR
erasure keys off `users.purged_at` as a cross-database signal
(`bp_router/db/models.py:45-50`) driving `purge_user_suite_data`
(`queries.py:82`). `docs/backplaned/router/state.md` §2.2 states the
constraint plainly: "The suite's conversation history (a separate store the
router can't reach)…".

**1.4 Per-session serialization is reimplemented outside the router.**
`sessions.md` §4 requires one in-flight op per `session_id`;
`bp_agents/session_lock.py` provides it as a process-local `asyncio.Lock`
plus an optional Valkey `SET NX PX` lock with a renewal watchdog, and
Valkey is the stated prerequisite for running a second channel instance.
The router is **single-replica** by design (`docs/backplaned/overview.md`
§2) and already serializes per *task*. If it owned session writes, a
per-session in-process lock there would be strictly stronger than the
suite's arrangement, and a second channel instance would need no Valkey at
all.

**1.5 `session_info` shadows the router's session row.** Both are keyed on
`session_id`. The router row already carries `metadata` (jsonb),
`opened_at`, `closed_at`. The suite row adds `channel`, `chat_id`,
`session_name`, `delegated_to`, `history_summary`, `delegate_summary` — and
a `created_at`/`updated_at` pair duplicating `opened_at`. Two rows, two
lifecycles, one entity.

**1.6 The user's preset choice is stored away from the authority that
gates it.** `user_config.preset_{pro,balanced,lite}` holds preset *names*;
the suite reads them per turn (`orchestrator/agent.py:156`, 28
`get_user_config` call sites) and passes an explicit name on every
`LlmRequest`, which the router then tier-checks against the user's level
(`bp_router/llm/service.py:205-257`). The router owns the catalog and the
gate; the suite owns the selection. A `ctx.llm(tier="balanced")` that the
router resolved itself would collapse the round trip.

## 2. Goals / non-goals

**Goals**

  * A router-owned conversation log with a **typed, derived-identity**
    write path — `assistant`/`tool_*` rows attributable to the agent that
    actually wrote them.
  * A **policy-free** carrier for per-thread session state (the rolling
    summaries, the sticky delegate), so no suite semantics enter the
    platform schema.
  * **Atomic multi-write** ops, preserving the three transactional
    sequences the suite depends on today.
  * Frames + SDK for task handlers; **session-JWT HTTP** for gateway
    agents (channel / webapp), mirroring `/v1/files/names`.
  * One-sided GC: session purge and closed-session retention reap history
    with no cross-database reconcile.
  * Per-session serialization owned by the router.

**Non-goals**

  * **Moving cron.** `cron_jobs` / `cron_executions` encode scheduler
    policy (report modes, the unreachable-session nudge, DST-aware
    evaluation). They stay in `bp_suite`. (§3.3)
  * **Moving channel identity.** `suite_platform_mappings`
    (`telegram|web|kakao` × `chat_id` → `user_id`) is transport-specific.
    Stays. (§3.3)
  * **Moving LanceDB.** Per-user memory + knowledge vector stores are not
    a router concern, and their erase path keeps needing a suite-side
    sweep. (§9.4)
  * **Summarization / delegation policy in the router.** The router
    provides atomic primitives; *when* to summarize and *what* a
    delegation episode means stay suite-side. (§6, §17)
  * **Retiring `bp_suite`.** With the companion prefs move (§13) it still
    holds cron + platform mappings, and two agents still open a pool for
    them.
  * **Cross-user history reference.** Same construction as the file
    store: impossible by keying.

## 3. What moves, what reshapes, what stays

### 3.1 Moves as-is — the conversation log

`session_history` is structurally the same object as the file stash: a
per-`(user, session)` namespace of rows owned by an agent, with an
existing router-side lifecycle hook to hang GC on. Its five access
patterns are all narrow and already isolated behind
`bp_agents/db/queries.py`:

| suite query | today | becomes |
| --- | --- | --- |
| `append_history` (`queries.py:211`) | 17 call sites | `append` command |
| `reload_incumbent` (`:238`) | 7 | `reload` command |
| `recent_tool_exchanges` (`:271`) | 1 | `recall` command |
| `demote_incumbent_through` (`:331`) | 2 | `demote` op (in `mutate`) |
| `demote_thread` (`:355`) | 2 | `demote` op (in `mutate`) |

### 3.2 Reshapes — `session_info`

Do **not** give the router `history_summary` / `delegate_summary` /
`delegated_to` / `chat_id` columns. That is suite policy in platform
schema, and it is the mistake the file store deliberately avoided (the
router knows names and byte counts, never contents). Split the six fields
by what they actually are:

| field | carrier | why |
| --- | --- | --- |
| `channel`, `chat_id`, `session_name` | the router session row's existing `metadata` jsonb | descriptive, per-session, already has a home |
| `history_summary`, `delegate_summary` | `session_state` KV, keyed `(session_id, agent_id, key)` | per-**thread** values; the KV shape is exactly right |
| `delegated_to` | `session_state` KV, `agent_id = NULL` (session-wide) | sticky across turns, so it is *not* the router's per-task `active_agent_id` — but it is opaque text to the router |
| `created_at` / `updated_at` | drop | `sessions.opened_at` + the KV's own `updated_at` |

`session_info` as a table disappears; `create_session_info` /
`update_session_info` (`queries.py:142`, `:181`) become metadata patches
and KV sets.

### 3.3 Stays in `bp_suite`

`cron_jobs`, `cron_executions`, `suite_platform_mappings`.

`user_config` is out of scope for the session store itself, but it is
**not** staying: §12 shows why the credential payoff depends on it, and
§13 moves the whole row — presets/timezone/name as typed router fields,
the suite-policy fields as opaque KV values.

## 4. Storage model

Two router tables, one migration (`bp_router/db/migrations/versions/0010_*`).

**`session_messages`** — the log. (Named for the router, not
`session_history`, so a dual-run window can't confuse the two databases'
tables in a query or a backup.)

| column | type | notes |
| --- | --- | --- |
| `id` | bigserial PK | the cutoff/paging cursor the summarizer already keys off |
| `user_id` | text, FK `users` | the scope half the router derives |
| `session_id` | text, FK `sessions` ON DELETE CASCADE | purge/retention comes free (§9) |
| `thread_agent_id` | text | the thread key — today's `session_history.agent_id` |
| `author_agent_id` | text | **net-new, router-stamped.** Who actually wrote the row. Never caller-supplied |
| `role` | text CHECK `user\|assistant\|tool_call\|tool_result` | unchanged |
| `message` | text | unchanged |
| `incumbent` | bool | unchanged |
| `hidden` | bool | unchanged |
| `created_at` | timestamptz | unchanged |

Index `(session_id, thread_agent_id, incumbent, created_at, id)` to serve
the reload query, and `(session_id, thread_agent_id, role, id DESC)` for
`recall`. The `ON DELETE CASCADE` on `session_id` is load-bearing: it is
what makes §9 one-sided.

**`session_state`** — the policy-free KV.

| column | type | notes |
| --- | --- | --- |
| `user_id` | text, FK `users` | |
| `session_id` | text, FK `sessions` ON DELETE CASCADE | |
| `agent_id` | text NULL | thread-scoped when set; session-wide when NULL |
| `key` | text | opaque to the router |
| `value` | text | opaque to the router |
| `updated_at` | timestamptz | |

PK `(session_id, agent_id, key)` — with NULL `agent_id` handled by a
partial unique index, or a `''` sentinel; pin at implementation. Bound
`value` length (suggest 256 KiB) and the row count per session so the KV
can't become an unpoliced blob store; a summary is a few KB.

## 5. Frames

One frame with a typed discriminated `command`, mirroring
`FileManageFrame`, plus one correlated reply mirroring `FileResultFrame`.
Both go in the `bp_protocol/frames.py` `Frame` union.

```python
class SessionOpFrame(_FrameBase):
    """Agent → router. A typed session-store command. `task_id` is NOT
    trusted as proof: the router derives the authoritative
    `(user_id, session_id)` from the task row after verifying the
    connection's authenticated `agent_id` is the task's active executor
    (`attachments.derive_task_file_scope`), and stamps that agent as
    `author_agent_id` on every row it writes."""
    type: Literal["SessionOp"] = "SessionOp"
    task_id: str
    command: SessionCommand
```

Commands (`kind`-discriminated, `extra="forbid"`, as the file commands are):

  * **`AppendRequest`** `{ thread_agent_id: str|None, role, message,
    incumbent: bool = True, hidden: bool = False }` — append one row.
    `thread_agent_id=None` means "my own thread" (the common case, and the
    only value an executor may use for `assistant`/`tool_*` — §8). Reply:
    `message_id`.
  * **`ReloadRequest`** `{ thread_agent_id: str|None, up_to_id: int|None,
    before_id: int|None }` — the `sessions.md` §2.1 reload: `incumbent`
    `user`/`assistant` rows for one thread, chronological. `up_to_id`
    bounds the read (the summarizer's cutoff window); `before_id` pages
    backwards under the payload budget (§10). Reply: `messages` +
    `truncated_before_id`.
  * **`RecallRequest`** `{ thread_agent_id: str|None, limit, skip }` — a
    page of past `tool_call`/`tool_result` **pairs** for one thread, the
    read side of `recall_tool_history`
    (`docs/design/agent-tool-history-recall.md`). Reply: `exchanges`.
  * **`StateGetRequest`** `{ agent_id: str|None|"*", keys: list[str] }`
    → `state` (a `{key: value}` map, or per-agent maps for `"*"`).
  * **`MutateRequest`** `{ ops: list[SessionMutateOp] }` — §6.

```python
class SessionResultFrame(_FrameBase):
    """Router → agent. Correlated response to `SessionOp`. Exactly one
    outcome shape is populated alongside `ref_correlation_id`.
      * `error` → `denied` (unknown task / not the active executor /
        thread-write not permitted — non-enumerable, as file `denied` is),
        `session_closed`, `invalid_role`, `value_too_large`,
        `state_quota_exceeded`, `rate_limited`.
      * `message_id`      → Append.
      * `messages` + `truncated_before_id` → Reload.
      * `exchanges`       → Recall.
      * `state`           → StateGet.
      * `applied_counts`  → Mutate (per-op affected-row counts, in order).
    """
```

`Ack` is not reused: like the file ops, these need typed payloads back.

## 6. The `mutate` command — where atomicity goes

Three sequences in `ChannelCore` perform dependent writes inside one
`conn.transaction()`, and `tests/test_review_channel_atomicity.py` pins
all three (a behavioural test for `delegate` against a live suite DB,
source pins for the other two — the review that added them documents
exactly what a crash between statements corrupts):

| sequence | writes | today |
| --- | --- | --- |
| `maybe_summarize` | set summary + demote rows `≤ cutoff` | `core.py:231` |
| `delegate` | append delegate seed row + set `delegated_to` | `core.py:275` |
| `_fold_back` | append recap + append ack + demote the delegate thread + clear `delegated_to` | `core.py:348` |

Frames destroy the caller-owned transaction, so the router must offer the
batch. `MutateRequest.ops` is an ordered list applied in **one router-side
transaction**, all-or-nothing:

```python
AppendOp     { thread_agent_id, role, message, incumbent, hidden }
StateSetOp   { agent_id: str|None, key, value: str|None }   # None deletes
DemoteOp     { thread_agent_id, up_to_id: int|None }        # None = whole thread
MetadataOp   { patch: dict[str, str|None] }                 # session metadata
```

Each of the three suite sequences becomes one `MutateRequest`. Note what
this does **not** do: there is no `summarize` op and no `delegate` op. The
router gets primitives and a transaction boundary; "fold the oldest 70% of
the thread" and "a delegation episode ends by retiring the delegate's
thread" stay in `ChannelCore`. That line is the whole reason this design
is safe to build — cross it and the platform starts encoding one suite's
conversation model.

Cap `ops` (suggest 16) so a batch can't hold a transaction open
indefinitely.

## 7. HTTP surface — for stewards, not executors

The channel and the webapp **spawn** tasks; they are never the active
executor, so they cannot derive scope from a task and cannot use the
frames. This is exactly the gateway-agent case the file store already
solved with session-JWT endpoints (`POST/GET /v1/files/names`,
`/v1/files/names/resolve`, and the note in that design that supersedes its
own earlier "HTTP rejected" position). Same shape here:

| endpoint | use |
| --- | --- |
| `POST /v1/sessions/{id}/messages` | append a turn (the channel's `record_user_turn`, `core.py:101`) |
| `GET /v1/sessions/{id}/messages` | render the transcript (webapp chat page), paginated |
| `POST /v1/sessions/{id}/mutate` | the three atomic sequences of §6 |
| `GET|PATCH /v1/sessions/{id}/state` | read/patch the KV |
| `PATCH /v1/sessions/{id}` | patch session `metadata` (name, channel, chat_id) |

Auth is the caller's **session JWT** for the owning user, ownership-checked
against `sessions.user_id` exactly as `POST /v1/files` is. One
implementation under both surfaces (`bp_router/session_store.py`, sibling
to `bp_router/file_store.py`) so frames and HTTP cannot drift.

## 8. Authorization — two writer roles, honestly

An audit of all 17 `append_history` call sites shows writes are **not**
own-thread-only, so "the router derives `agent_id` from the socket" is too
strong a rule. Two legitimate cross-thread patterns exist:

  * an **executor** writing a `user` row into *another* agent's thread —
    the delegation seed (`orchestrator/agent.py:259`), per `sessions.md`
    §6;
  * the **steward** (channel) writing *any* role anywhere in its session —
    `user` rows into the dispatch target's thread (`core.py:101`,
    `gateway.py:769`, `kakao_gateway.py:614`), and `assistant` rows into
    the orchestrator's thread (the `_fold_back` ack, `core.py:359`; the
    cron report, `chatbot/cron.py:166`).

So the rule is role-and-actor shaped:

| actor | `user` row | `assistant` / `tool_call` / `tool_result` row |
| --- | --- | --- |
| **executor** (frames; is the task's active executor) | any thread in the derived session | **own thread only** (`thread_agent_id` ∈ {`None`, its own id}) |
| **steward** (session-JWT HTTP; holds the session's `session.steward` grant) | any thread in the session | any thread in the session |

`author_agent_id` is stamped from the authenticated principal in both
cases and is never caller-supplied, so even a permitted cross-thread write
is attributable — which nothing is today. Reads are **session-scoped**:
any agent acting in user U's session S may reload or recall any thread of
S. That is deliberate, and it is the same "shared-session reach is
intentional" call the file store made — `history_summarizer` exists
precisely to read the orchestrator's thread
(`history_summarizer/agent.py:145`, `:163`, bounded by the caller's
`up_to_id` cutoff), and l1 delegates read the seed row the orchestrator
wrote for them (`l1_common.py:224`).

The steward grant is the one net-new authorization concept. Model it as an
ACL capability the router checks (`session.steward`), granted to the
channel and webapp agents — which finally makes the advertised
`session.history` capability (§1.2) mean something enforceable, in two
tiers instead of one.

Writes to a **closed** session are refused (`session_closed`), matching
the `NewTask` admit rule in `docs/backplaned/router/state.md` §2.2. Reads
of a closed session are allowed — the webapp renders closed conversations,
and reopen must not lose the transcript.

## 9. Lifecycle & GC

### 9.1 History survives close — unlike the file stash

The file store GCs `scope = session:{id}` rows on close
(`bp_router/api/sessions.py::_close_session`). **History must not.**
Sessions reopen (`POST /v1/sessions/{id}/reopen`), the webapp lists and
renders closed sessions, and `sessions.md` §2 makes the log the durable
record. So:

  * **close** — no history GC. (The `session_state` KV also survives; a
    reopened conversation keeps its summary. `delegated_to` is the one
    value the channel may want to clear, and clearing it is suite policy
    via a `StateSetOp`, not a router-side sweep.)
  * **purge** (`DELETE …?purge=true`, the webapp's "remove") — the FK
    cascade takes `session_messages` + `session_state` with the session
    row. `queries.purge_session_suite_data` loses two of its three tables
    and becomes a cron-only cleanup.
  * **retention** (`session_gc_loop`, `closed_session_retention_days`) —
    same cascade, no change to the loop.

### 9.2 The reconcile loop shrinks

`bp_agents/agents/chatbot/session_gc.py` and the
`/v1/admin/sessions/filter-existing` probe exist only because history is
unreachable from the router. After phase 1 the loop no longer reaps
history or `session_info` — it reaps `cron_jobs` only, which is a small
enough job that folding it into the cron agent is worth considering. The
probe endpoint stays (cron still needs it), so this is a shrink, not a
deletion. Be precise about that in the changelog when it lands.

### 9.3 Per-session serialization moves to the router

With writes centralized, the router can hold a per-`session_id`
`asyncio.Lock` around message/mutate ops — it is single-replica, so that
is a complete guarantee, where the suite's is complete only with Valkey
configured. Two caveats keep `session_lock.py` alive in phase 1: the
suite's lock also spans **dispatch → agent run → result → channel
writes** (a turn, not a write), and it serializes `memory.add`-adjacent
policy the router knows nothing about. So: the router lock protects
*store consistency*; the suite lock keeps protecting *turn ordering*.
Valkey stops being a correctness prerequisite for a second channel
instance only once turn ordering also has a router-side answer — call
that out of scope and don't oversell it.

### 9.4 GDPR erase

`purge_user` cascades `session_messages` / `session_state` in the same
transaction that scrubs the user row, so `users.purged_at` stops being a
cross-database signal for conversation data. It is still the signal for
per-user **LanceDB** erase (memory + knowledge), so
`purge_user_suite_data` and the reconcile path survive — narrower, not
gone.

## 10. Payload budget and paging

`max_payload_bytes` defaults to **1 MiB** per WS frame
(`bp_router/settings.py:269`; the SDK's `ws_max_receive_bytes` is 2 MiB,
`bp_sdk/settings.py:102`). A reload response is the one shape here that
can approach it: at a generous `max_context_token_limit` a thread's
incumbent rows can reach several hundred KB of text. The file store's
answer was to keep bulk bytes off the control pump entirely (§8.1 of that
design: names on the frame, bytes resolved router-side). History has no
equivalent indirection — the agent genuinely needs the text.

So bound it explicitly rather than hoping:

  * The router applies a **byte budget** to `Reload`/`Recall` replies
    (default ~60% of `max_payload_bytes`), fills newest-first back to the
    oldest row that fits, and sets `truncated_before_id` when it stopped
    early.
  * The SDK pages transparently: on `truncated_before_id`, issue the next
    `ReloadRequest(before_id=…)` and prepend. Callers see one list.
  * The **HTTP** transcript endpoint (§7) is the unbounded path, paginated
    by `id` — that is what the webapp render uses, and it has no frame cap.

Recall is already capped per-result and in total by
`agent-tool-history-recall.md`; keep those caps and let the budget be the
outer guard.

## 11. SDK surface

`ctx.history`, shaped like `ctx.files` (`bp_sdk/files.py::FileStash`), in a
new `bp_sdk/history.py`:

```python
class SessionHistory:
    async def append(self, role: str, message: str, *,
                     thread: str | None = None,      # None = own thread
                     incumbent: bool = True,
                     hidden: bool = False) -> int: ...

    async def reload(self, *, thread: str | None = None,
                     up_to_id: int | None = None) -> list[Turn]: ...  # pages internally

    async def recall(self, *, count: int, skip: int = 0,
                     thread: str | None = None) -> list[ToolExchange]: ...

    async def state(self, *keys: str,
                    thread: str | None = None) -> dict[str, str]: ...

    def mutate(self) -> SessionMutation: ...   # builder → one atomic MutateRequest
```

```python
async with ctx.history.mutate() as m:          # commits as ONE transaction
    m.append("user", recap, thread=ORCHESTRATOR, hidden=True)
    m.append("assistant", "Acknowledged.", thread=ORCHESTRATOR, hidden=True)
    m.demote(thread=delegate)
    m.state_set("delegated_to", None)
```

The steward side gets the same shape over HTTP in
`bp_agents/agents/chatbot/credentials.py`, alongside the existing named-file
store client — so `ChannelCore` swaps `self._pool` for a client and keeps
its method signatures.

No LLM tool bundle. `recall_tool_history` is already a suite-side local
tool over `common/tool_history.py`; it re-points at `ctx.history.recall`
and needs no protocol-level tool surface.

## 12. Suite-side deletions — and what this phase does *not* buy

`bp_agents/db/queries.py` loses 10 of its 32 functions outright
(`get_session_info`, `list_session_info_for_user`, `list_old_session_ids`,
`create_session_info`, `update_session_info`, `append_history`,
`reload_incumbent`, `recent_tool_exchanges`, `demote_incumbent_through`,
`demote_thread`), `purge_session_suite_data` shrinks to cron-only, and
`bp_agents/db/models.py` loses `SessionInfoRow` + `SessionHistoryRow`.
Roughly **54 call sites** are rewritten: 17 `append_history`, 12
`get_session_info`, 8 `update_session_info`, 7 `reload_incumbent`, 6
`create_session_info`, 2 `demote_incumbent_through`, 2 `demote_thread`, 1
`recent_tool_exchanges`, 1 `list_session_info_for_user`.

**It removes zero Postgres pools.** This is the correction that matters
most for judging the phase, and it is easy to get wrong: `get_user_config`
is the *universal* suite dependency — 9 of the 10 agents in §1.1 call it,
including every agent whose only other suite use is session data. Per-agent
suite query usage today:

| agent | session data | `user_config` | other |
| --- | --- | --- | --- |
| `orchestrator` | ✓ | ✓ | |
| `history_summarizer` | ✓ | ✓ | |
| `computer_use` | ✓ (via `l1_common`) | ✓ (via `l1_common`) | |
| `research`, `deep_reasoning` | | ✓ | |
| `knowledge_base` | | ✓ | LanceDB |
| `memory` | | ✓ | LanceDB, `list_user_ids` |
| `config` | | ✓ (read + write) | |
| `webapp` | ✓ | ✓ | cron, mappings |
| `chatbot` | ✓ | ✓ | cron, mappings |

So after phase 1 every one of those agents still opens `open_pool` — for a
single `SELECT * FROM user_config`. The credential-removal payoff (§1.1)
lands only when `user_config` moves too (§13), and it is worth stating in
that order rather than claiming it here.

What phase 1 *does* buy, on its own: the enforceable `session.history`
capability with real authorship records (§8), one-sided session purge and
retention (§9), and the router-side write lock (§9.3).

## 13. The companion move — `user_config` → router user preferences

Per §12 the credential payoff needs this, and it is **smaller and lower
risk than the session store**: 28 reads, 2 writes, tiny values, no
atomicity requirement, no paging, no authorship subtlety. Recommended
**first**, as the cheap proof of the pattern.

Same policy-free trick as `session_state` — a `user_prefs` KV keyed
`(user_id, key)`, opaque to the router — with three fields promoted to
typed columns on the router side *because the router acts on them*:

| field | shape | why |
| --- | --- | --- |
| `preset_{pro,balanced,lite,embedding}` | typed | the router owns the catalog + the tier gate (`bp_router/llm/service.py:205-257`); the selection belongs with the authority that validates it |
| `timezone`, `full_name` | typed on `users` | no router equivalent today; both are plainly user identity, and cron's DST-aware evaluation already depends on the timezone being right |
| `sandbox_uid`, `custom_note`, `verbose_default`, `max_context_token_limit`, `language`, `default_session_id` | KV | pure suite policy — the router stores and returns them, and interprets none of them |

Note the reversal from §3.3: the suite-policy fields move too, as *opaque
values*. Leaving them in `bp_suite` would mean every agent keeps its pool
for them, which defeats the point. Storing them ≠ knowing what they mean —
exactly the line the file store draws around file contents.

With both phases landed, the agents that hold **no** Postgres pool are
`orchestrator`, `research`, `deep_reasoning`, `computer_use`,
`knowledge_base`, `memory`, `config`, and `history_summarizer` — eight of
ten. `chatbot` and `webapp` keep theirs for cron + platform mappings
(§2 non-goals). `memory.list_user_ids` needs a router-side replacement
(the `users` table already has the data; a service-principal endpoint
serves it).

**Tier resolution is a separate, later refinement.** Once prefs are
router-side, `LlmRequestFrame` *could* carry `tier="balanced"` and let the
router resolve the preset, deleting the read-config prologue from every
turn. Don't bundle that with the storage move: it touches the LLM request
path, which carries a known identity-derivation inconsistency already
flagged as an open question in the file-store design (that path trusts
`frame.user_id` for tier/quota/audit while file ops derive it from the
task). Land that hardening pass first or concurrently — not underneath a
data migration.

## 14. Security

  * **Derived identity, one pattern.** Every frame op resolves
    `(user_id, session_id)` from the task row and verifies active-executor,
    reusing `attachments.derive_task_file_scope`. No new derivation logic
    means no second thing to get wrong.
  * **Attribution is net-new.** `author_agent_id` makes every row
    attributable; today's schema records nothing about the writer.
  * **The steward grant is the sharp edge.** A steward can write any role
    into any thread of a session it holds a JWT for. That is a real
    authority, and it is why it must be an explicit ACL capability rather
    than "whoever has a session token", and why steward writes belong in
    the audit chain.
  * **Audit.** Mutating ops append hash-chain audit events
    (`session.message_append`, `session.demote`, `session.state_set`) the
    way file mutations do. Do **not** put `message` content in the audit
    payload — row id, thread, role, and byte count only. Conversation text
    in an append-only audit chain is a retention and erasure problem
    (`purge_user` must be able to erase it).
  * **DoS surface.** History append is now a router write path on the
    task-admit pool. Bound it: the `value`/`ops` caps of §4/§6, a
    per-session row ceiling, and the existing per-agent rate limiter
    (`rate_limited` is in the error set for this reason). Size
    `db_pool_max_size` for the added per-turn writes before the flip.
  * **Reads are session-wide by design** (§8) — same intentional
    shared-session reach as the stash, and the reason a delegate can read
    its seed row without re-keying.

## 15. Migration — phased, with live data

Unlike the file store (pre-release, hard cutover), this touches a
**deployed** database with real conversations. No hard cutover.

1. **Land the router side** (§4 tables, §5 frames, §7 HTTP, §8 authz).
   Inert — nothing writes to it.
2. **Backfill.** `session_history` → `session_messages`,
   `session_info` → `sessions.metadata` + `session_state`. Both databases
   live in one Postgres instance but are **separate databases with
   separate owners** (`deploy/postgres-init/01-create-suite-db.sql:19`), so
   there is no `INSERT … SELECT` across them: use `COPY … TO STDOUT` piped
   to `COPY … FROM STDIN` (a `scripts/` one-shot), or `postgres_fdw` if the
   operator prefers. `author_agent_id` is unknowable for historical rows —
   backfill it as `thread_agent_id` and note the imprecision in the script,
   or use a `'migrated'` sentinel; pin at implementation. Preserve `id`
   values: `recall`/`demote`/`up_to_id` cursors and the summarizer's cutoffs
   reference them.
3. **Flip the readers/writers behind a suite setting**
   (`SUITE_SESSION_STORE=router|suite`, default `suite`). Quiesce first —
   the existing per-session lock is the clean quiesce point — then backfill
   the tail and flip. One release with the flag; no dual-write (two sources
   of truth for `incumbent` is worse than a short maintenance window).
4. **Soak**, then drop `session_info` / `session_history` in a suite
   migration and delete the dead queries (§12).

Step 4 is the irreversible one; keep 1–3 reversible by leaving the suite
tables in place and untouched during the soak.

## 16. Implementation sequence

**Phase 0 — do the prefs move first (§13).** Smaller, no atomicity, no
paging, and it is what actually starts closing agents' database pools. It
also exercises the KV pattern, the session-JWT HTTP surface, and the
backfill mechanics on a table where a mistake costs a preference, not a
conversation.

Then the session store:

1. Schema + migration (§4), with the FK cascades.
2. `bp_router/session_store.py` — scope keys, authz rule (§8), byte budget
   (§10), audit. One module both surfaces call.
3. `SessionOpFrame` / `SessionResultFrame` + dispatch handlers (§5) +
   `mutate` transaction (§6).
4. Session-JWT HTTP endpoints (§7).
5. `bp_sdk/history.py` (§11) + the steward HTTP client.
6. Backfill script (§15 step 2) + the `SUITE_SESSION_STORE` flag.
7. Cut over `ChannelCore`, the orchestrator, `l1_common`,
   `history_summarizer`, `tool_history`, the webapp pages (§12).
8. Shrink the reconcile loop (§9.2); router-side per-session lock (§9.3).
9. Soak, drop suite tables, delete dead code.

Steps 1–5 are independently testable against the existing suite behaviour;
7 is the behavioural flip. Tier resolution (§13, last paragraph) is a
separate sequence after both.

## 17. What not to do

  * **Don't give the router `history_summary` / `delegated_to` columns.**
    The KV exists so platform schema stays free of one suite's
    conversation model. A column is a one-line change that is very hard to
    take back.
  * **Don't add `summarize` / `delegate` / `end_delegation` ops.** The
    router gets primitives and a transaction boundary (§6). Policy stays
    in `ChannelCore`.
  * **Don't GC history on session close.** Reopen and the webapp both
    depend on it surviving (§9.1). The file store's close-time GC is the
    wrong precedent to copy here.
  * **Don't claim `agent_id` is fully router-derived.** Two legitimate
    cross-thread write patterns exist (§8); the honest model is derived
    *authorship* plus a permitted *thread* target.
  * **Don't dual-write during migration.** Two writers of `incumbent`
    race in exactly the way the per-session queue exists to prevent.
  * **Don't move cron or the platform mappings** to make the suite
    Postgres "go away" (§2 non-goals). The goal is removing the *seam that
    costs*, not table count.
  * **Don't put message text in audit payloads** (§14).
  * **Don't bundle tier resolution** into either data move (§13) — it
    changes the LLM request path, which has its own identity-derivation
    debt to settle first.
  * **Don't claim this phase closes agents' DB pools.** It doesn't; §12
    shows why, and §13 is what does.

## 18. Open questions

  * **Turn-level serialization.** §9.3 leaves the suite lock owning turn
    ordering while the router owns store consistency. Is there a
    router-side "session busy" primitive worth having (it would need to
    span dispatch → result, which is a task-tree concept, not a write), or
    does the suite lock stay indefinitely? This decides whether Valkey
    ever stops being the multi-instance prerequisite.
  * **`author_agent_id` for backfilled rows** — `thread_agent_id`,
    `'migrated'`, or NULL? NULL is the most honest and forces every reader
    to handle it; the other two are convenient and slightly false.
  * **Does `session_state` need a value history?** Summaries are
    overwritten in place today. A one-deep previous value would make a
    failed summarize op recoverable, at the cost of a shape decision the
    router shouldn't be making for the suite.
  * **`session_name` in `metadata` vs. a first-class column.** The webapp
    lists and sorts by it; a jsonb key is fine for a few hundred sessions
    per user and awkward beyond that. Measure before promoting.
  * **Should reads be narrowable?** §8 makes reads session-wide because
    the summarizer and delegates need cross-thread reads. A future
    `session.history.read` scope per agent could narrow it, but nothing in
    the current suite wants that.
  * **Cron's history writes.** `chatbot/cron.py:166` writes an
    `assistant` row as the steward. If cron ever moves out of the chatbot
    process, whatever hosts it needs the steward grant — worth knowing
    before the ACL rule is written.

## 19. Sizing

The file store cost roughly 2,300 LOC of platform code across
`bp_sdk/files.py` (446), `bp_sdk/file_tools.py` (448),
`bp_router/api/files.py` (635), `bp_router/file_store.py` (146), ~371
lines of dispatch handlers and ~246 lines of frames. The session store is
smaller — no blobs, no S3, no dedup, no quota accounting, no LLM tool
bundle:

| piece | estimate |
| --- | --- |
| frames (`SessionOp` + `SessionResult` + command union) | ~150 |
| router `session_store.py` + queries | ~350 |
| dispatch handlers | ~200 |
| HTTP endpoints | ~200 |
| migration | ~80 |
| `bp_sdk/history.py` + steward client | ~350 |
| suite rewrites (54 call sites) + deletions | ~-400 net |
| backfill script | ~150 |

Call it ~1,400 LOC new platform code, a net reduction suite-side, and a
migration. The payoff is not lines. From this phase alone: an enforceable
`session.history` capability with real authorship records, one-sided
session purge and retention, and a router-owned write lock. From this
phase **plus** the companion prefs move (§13, the smaller of the two):
eight of ten agents stop holding a database password.
