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

The derivation goes one step further than the file store's. A file lives
in a shared per-session namespace any peer may write; a **history row is
an utterance**, so the thread it lands in is derived too — an agent
writes its own thread and no other. Today's suite does not obey that rule
(five cross-thread write patterns, §8.1), so this design carries the
suite rework that makes it true, and that rework is sequenced *first*
(§15, §16).

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

  * A router-owned conversation log where **an agent can only write its
    own thread** — the thread key is derived from the task's active
    executor, so there is no field in which to name another agent (§8).
  * A suite reworked so that rule is achievable without losing behaviour:
    every cross-thread write re-homed to its owner (§8.1).
  * A **policy-free** carrier for per-thread session state (the rolling
    summaries, the sticky delegate), so no suite semantics enter the
    platform schema.
  * **Atomic multi-write** ops, preserving the three transactional
    sequences the suite depends on today — minus their cross-thread half.
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
| `demote_incumbent_through` (`:331`) | 2 | **deleted** — the `history.floor` watermark (§8.3) |
| `demote_thread` (`:355`) | 2 | **deleted** — the `delegation.episode` counter (§8.3) |

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

Three router tables, one migration (`bp_router/db/migrations/versions/0010_*`).

**`session_messages`** — the log. (Named for the router, not
`session_history`, so a dual-run window can't confuse the two databases'
tables in a query or a backup.)

| column | type | notes |
| --- | --- | --- |
| `id` | bigserial PK | the cursor for paging, the floor watermark, and recall |
| `user_id` | text, FK `users` | the scope half the router derives |
| `session_id` | text, FK `sessions` ON DELETE CASCADE | purge/retention comes free (§9) |
| `thread_agent_id` | text | the thread key — **router-derived from the task's active executor, never a caller field** (§8) |
| `episode` | int | delegation-episode stamp, immutable at insert; the delegate's reload bound (§8.3) |
| `role` | text CHECK `user\|assistant\|tool_call\|tool_result` | unchanged |
| `message` | text | unchanged |
| `hidden` | bool | unchanged |
| `task_id` | text NULL, FK `tasks` | the input row's idempotency key (§8.4) |
| `created_at` | timestamptz | unchanged |

**There is no `incumbent` column and no `author_agent_id` column.** Both
were in an earlier draft of this design and both are gone for the same
reason: with `thread_agent_id` derived, the author *is* the thread owner
(so recording it separately is noise), and `incumbent` was mutable state
whose only use was bulk-retiring someone's rows (so it becomes a derived
bound — §8.3).

Index `(session_id, thread_agent_id, episode, id)` serves the reload
query; `(session_id, thread_agent_id, role, id DESC)` serves `recall`; a
unique partial index on `(session_id, task_id) WHERE role = 'user'` makes
the input append idempotent under redelivery. The `ON DELETE CASCADE` on
`session_id` is load-bearing: it is what makes §9 one-sided.

**`session_state`** — the policy-free KV.

| column | type | notes |
| --- | --- | --- |
| `user_id` | text, FK `users` | |
| `session_id` | text, FK `sessions` ON DELETE CASCADE | |
| `agent_id` | text NULL | thread-scoped when set (owner-writable only); session-wide when NULL (steward-writable) |
| `key` | text | opaque to the router |
| `value` | text | opaque to the router |
| `updated_at` | timestamptz | |

PK `(session_id, agent_id, key)` — with NULL `agent_id` handled by a
partial unique index, or a `''` sentinel; pin at implementation. Bound
`value` length (suggest 256 KiB) and the row count per session so the KV
can't become an unpoliced blob store; a summary is a few KB.

Known keys, all suite-defined and opaque to the router: thread-scoped
`history.summary` and `history.floor` (§8.3); session-scoped
`delegated_to`, `delegation.episode`, `delegation.instruction`.

**`session_pending`** — the hand-over queue (§8.2).

| column | type | notes |
| --- | --- | --- |
| `id` | bigserial PK | drain order |
| `session_id` | text, FK `sessions` ON DELETE CASCADE | |
| `target_agent_id` | text | who may consume it |
| `kind` | text | `input` \| `delegation_seed` \| `handback_recap` \| `summary_apply` — opaque to the router |
| `payload` | jsonb | opaque to the router |
| `created_at` / `consumed_at` | timestamptz | consumed rows are kept briefly for audit, then swept |

A pending item is **not history**: no role, no author, never rendered as
an utterance. Only `target_agent_id` may consume it, and consuming means
the owner writes its own rows (§8.2).

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
    (`attachments.derive_task_file_scope`). That same derivation supplies
    `thread_agent_id` for every row written — which is why no command
    below carries a writable thread field."""
    type: Literal["SessionOp"] = "SessionOp"
    task_id: str
    command: SessionCommand
```

Commands (`kind`-discriminated, `extra="forbid"`, as the file commands are):

  * **`AppendRequest`** `{ role, message, hidden: bool = False,
    once_per_task: bool = False }` — append one row **to the caller's own
    thread**; there is no thread parameter (§8). `once_per_task` engages
    the idempotency index for the turn's input row. Reply: `message_id`.
  * **`ReloadRequest`** `{ thread: str|None, up_to_id: int|None,
    before_id: int|None }` — the `sessions.md` §2.1 reload for one thread,
    chronological, bounded by that thread's `history.floor` and (for a
    delegate) the current `delegation.episode`. `thread` defaults to the
    caller's own; naming another agent's thread is permitted because
    **reads are session-scoped** (§8) — the summarizer needs exactly this.
    `up_to_id` bounds the read to a cutoff window; `before_id` pages
    backwards under the payload budget (§10). Reply: `messages` +
    `truncated_before_id`.
  * **`RecallRequest`** `{ thread: str|None, limit, skip }` — a page of
    past `tool_call`/`tool_result` **pairs**, the read side of
    `recall_tool_history` (`docs/design/agent-tool-history-recall.md`).
    Reply: `exchanges`.
  * **`StateGetRequest`** `{ agent_id: str|None|"*", keys: list[str] }`
    → `state` (a `{key: value}` map, or per-agent maps for `"*"`).
  * **`PendingPutRequest`** `{ target_agent_id, kind, payload }` — enqueue
    a hand-over item for another agent (§8.2). This is the *only* way to
    put something into another agent's context, and it is not history.
  * **`MutateRequest`** `{ ops: list[SessionMutateOp] }` — §6.

```python
class SessionResultFrame(_FrameBase):
    """Router → agent. Correlated response to `SessionOp`. Exactly one
    outcome shape is populated alongside `ref_correlation_id`.
      * `error` → `denied` (unknown task / not the active executor /
        state key not owned by the caller — non-enumerable, as file
        `denied` is), `session_closed`, `invalid_role`,
        `value_too_large`, `state_quota_exceeded`, `rate_limited`.
      * `message_id`      → Append (the existing row's id when
                             `once_per_task` found one — idempotent, not
                             an error).
      * `messages` + `truncated_before_id` → Reload.
      * `exchanges`       → Recall.
      * `state`           → StateGet.
      * `pending`         → Mutate with a ConsumePendingOp: the drained
                             items, so the owner can materialise them.
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

| sequence | writes today | after the rework (§8.1) |
| --- | --- | --- |
| `maybe_summarize` (`core.py:231`) | set summary + demote rows `≤ cutoff` | steward enqueues `summary_apply`; the **owner** applies it as set-summary + set-floor on its own state |
| `delegate` (`core.py:275`) | append seed into the target's thread + set `delegated_to` | set session-scoped `delegation.instruction` + `delegated_to` + bump `delegation.episode` — no thread touched |
| `_fold_back` (`core.py:348`) | append recap + ack into the orchestrator's thread + demote the delegate's thread + clear `delegated_to` | enqueue `handback_recap` + clear `delegated_to` + bump `delegation.episode` |

Note what the rework does to this table: **none of the three still writes
another agent's thread**, so the atomicity requirement survives but the
ownership violation does not. The batch is still needed — a summary
applied without its floor, or a cleared delegation without its episode
bump, is still corruption.

`MutateRequest.ops` is an ordered list applied in **one router-side
transaction**, all-or-nothing:

```python
AppendOp           { role, message, hidden, once_per_task }   # own thread only
StateSetOp         { agent_id: str|None, key, value: str|None }  # own thread, or session-scoped
PendingPutOp       { target_agent_id, kind, payload }
ConsumePendingOp   { kinds: list[str] | None }   # drain MY queue; replies `pending`
MetadataOp         { patch: dict[str, str|None] }             # session metadata
```

`DemoteOp` is **absent by design** — §8.3 replaces bulk `incumbent` flips
with the `history.floor` watermark (a `StateSetOp` on the owner's own
state) and the `delegation.episode` counter (a session-scoped
`StateSetOp`). Removing the op is what removes the last way to reach into
another agent's thread.

A turn's opening batch is therefore one round trip: consume pending →
materialise each item as an `AppendOp` on the caller's own thread → apply
any `summary_apply` floor. Its closing batch is the assistant row plus the
tool rows.

There is still no `summarize` op and no `delegate` op. The router gets
primitives and a transaction boundary; "fold the oldest 70% of the
thread" and "a delegation episode ends" stay in `ChannelCore`. That line
is the whole reason this design is safe to build — cross it and the
platform starts encoding one suite's conversation model.

Cap `ops` (suggest 16) so a batch can't hold a transaction open
indefinitely.

## 7. HTTP surface — for stewards, not executors

The channel and the webapp **spawn** tasks; they are never the active
executor, so they cannot derive scope from a task and cannot use the
frames. This is exactly the gateway-agent case the file store already
solved with session-JWT endpoints (`POST/GET /v1/files/names`,
`/v1/files/names/resolve`, and the note in that design that supersedes its
own earlier "HTTP rejected" position). Same shape here — with one
deliberate absence:

| endpoint | use |
| --- | --- |
| `GET /v1/sessions/{id}/messages` | render the transcript (webapp chat page), paginated |
| `POST /v1/sessions/{id}/pending` | enqueue a hand-over item for an agent (§8.2) |
| `POST /v1/sessions/{id}/mutate` | the atomic sequences of §6 — session state + pending, never a thread append |
| `GET\|PATCH /v1/sessions/{id}/state` | read the KV; patch **session-scoped** keys only |
| `PATCH /v1/sessions/{id}` | patch session `metadata` (name, channel, chat_id) |

**There is no `POST /v1/sessions/{id}/messages`.** An earlier draft had
one, for the channel's `record_user_turn`. It is exactly the hole the
rework closes: a steward with a session JWT could write any role into any
thread. Removing the endpoint — not gating it — is what makes the rule
structural. A steward that wants something in a thread enqueues it.

Auth is the caller's **session JWT** for the owning user, ownership-checked
against `sessions.user_id` exactly as `POST /v1/files` is. One
implementation under both surfaces (`bp_router/session_store.py`, sibling
to `bp_router/file_store.py`) so frames and HTTP cannot drift.

## 8. Authorization — strict thread ownership

**The rule: a history row is written only by the agent whose thread it
lands in.** No exceptions, no steward override, no "trusted" cross-thread
appends. Anything another component wants to put into an agent's context
becomes a **pending item** the owner materializes under its own
authorship on its next turn (§8.2).

| op | permitted actor |
| --- | --- |
| append into thread T | **T's owner only** — and `thread_agent_id` is not a frame field at all; the router derives it from the task's active executor |
| thread state (`history.floor`, `history.summary`) | T's owner only |
| session state (`delegated_to`, `delegation.*`, the pending queue) | steward, or an executor in the session |
| pending-item put | steward, or an executor in the session |
| reads (reload / recall / state) | any agent acting in the session |

The prize is the first row's parenthetical. Once the owner is derived,
`AppendRequest` **has no thread field to forge** — the whole class of
"write in another agent's name" is gone at the protocol boundary rather
than policed inside it. An earlier draft of this design carried an
`author_agent_id` column to make cross-thread writes *attributable*;
under this rule it has nothing to record that `thread_agent_id` doesn't
already say, so §4 drops it. Attribution by construction beats
attribution by column.

Reads stay session-scoped, unchanged and deliberate — the same
"shared-session reach is intentional" call the file store made.
`history_summarizer` must read the orchestrator's thread
(`history_summarizer/agent.py:145`, `:163`), and a delegate reads what the
orchestrator left for it (`l1_common.py:224`). Reading cannot fabricate.

Writes to a **closed** session are refused (`session_closed`), matching the
`NewTask` admit rule in `docs/backplaned/router/state.md` §2.2. Reads of a
closed session are allowed — the webapp renders closed conversations, and
reopen must not lose the transcript.

The steward keeps a role, but a much smaller one: session-scoped state and
the pending queue. It can ask for something to appear in a thread; it
cannot make it appear. Model it as an ACL capability (`session.steward`)
on the channel and webapp agents.

### 8.1 What has to change in the suite for that rule to hold

Today's 17 `append_history` sites include five cross-thread patterns. Each
one is re-homeable, and in three of the five the payload **already carries
everything the owner needs** — the cross-thread write is incidental, not
load-bearing:

| # | today | rework |
| --- | --- | --- |
| 1 | the channel writes the `user` row into the dispatch target's thread (`core.py:101` ← `gateway.py:693`, `kakao_gateway.py:508`, `webapp/turns.py:124`), including the `user-attached file saved as …` note (`gateway.py:770`) | the channel **enqueues** an `input` item (pre-dispatch, as today's write is pre-dispatch); **the receiving agent materialises it into its own thread at turn start.** The text also already rides `payload.prompt`, and the orchestrator *already* falls back to it when the row is missing (`orchestrator/agent.py:166-169`) — that branch becomes the fallback for direct invocation |
| 2 | the orchestrator writes the delegation seed into the delegate's thread (`_do_hand_off`, `:259`) | **the delegate composes and writes its own seed on `first_turn`.** `ctx.peers.delegate` already ships all three ingredients — `LLMData(prompt, agent_instruction, context)` (`:263`) — so the seed text is reconstructible at the far end verbatim |
| 3 | the channel's `/delegate` writes a summarized seed into the target's thread (`core.py:276`) | the channel writes **session-scoped** `delegation.instruction`; the delegate materializes it on its first turn, through the same path as #2 |
| 4 | the channel writes recap + ack into the orchestrator's thread on hand-back (`_fold_back`, `core.py:355`, `:359`), and the cron report as an orchestrator `assistant` row (`cron.py:166`) | recap → the orchestrator's **pending queue**, materialized on its next turn. Cron → pass the job's `report` policy in the payload so the orchestrator evaluates `_effective_report` itself and writes its own row (it is already the executor of the `cron_message` task, `cron.py:137`) |
| 5 | cross-thread **demotes**: `_fold_back` and `end_delegation` retire the delegate's whole thread; summarize-apply demotes a prefix of the owner's | replaced by derived reload bounds — §8.3 |

Two of these are net deletions. #2 removes the orphan-seed rollback
(`orchestrator/agent.py:270-277`) that exists *only* because the seed is
written before the reassignment that can fail — write it at the far end
and the failure mode cannot occur. #1 removes `record_user_turn` and its
four call sites, plus the fallback branch it forced.

### 8.2 The pending queue

The honest residue. When one component wants something in another's
context, it enqueues a **pending item** — session-scoped, typed, and
explicitly *not* history:

```
pending(session_id, target_agent_id, kind, payload, created_at, consumed_at)
   kind ∈ { input | delegation_seed | handback_recap | summary_apply }
```

The owner drains its queue at turn start and materializes each item into
its own thread **as its own append**, batched into the same `MutateRequest`
that already opens the turn. Zero extra round trips.

Why this is not the old cross-thread write with a new name: a pending item
has no author, no role, and never renders as an utterance. Until the owner
materializes it, nothing in the transcript claims that agent said or
received anything. The steward can propose; only the owner can speak.

### 8.3 Retiring `incumbent` — derived bounds instead of bulk flips

`incumbent` is a **mutable** column today, and every mutation of it is a
bulk retirement of someone's rows: a prefix (`demote_incumbent_through`,
`id <= cutoff`) or a whole thread (`demote_thread`). That mutability is the
only reason cross-thread *curation* exists. Both shapes are expressible as
**derived bounds** instead:

  * **`history.floor`** — per-thread state, owner-written. Reload becomes
    `id > floor`. Summarize-apply = set summary + set floor, two writes to
    the owner's own state, atomically, on the owner's next turn.
  * **`delegation.episode`** — a session-scoped counter, steward-written.
    Rows are stamped with the current episode at insert (immutable, like
    `thread_agent_id`); a delegate reloads `episode = current`. Ending an
    episode is a **counter bump** — no write into anyone's thread.

So `session_messages` carries `episode int` and **no `incumbent` column**;
the `(incumbent, hidden)` matrix of `sessions.md` §2.2 collapses to
`hidden` plus two bounds. Tool rows are already excluded from reload by the
`role IN ('user','assistant')` filter, not by `incumbent`
(`queries.py:238`), so nothing else depends on the column.

This is the deepest change in the rework and the one that actually closes
the hole: with no mutable `incumbent` and no `thread_agent_id` field,
there is no way to reach into another agent's thread at all — not to write
it, not to retire it.

### 8.4 What it costs

Stated plainly, because three of these are behaviour changes, not just
refactors:

  * **`/stop` keeps its property, but through a new mechanism.** Today
    "the user message stays" on cancel (`webapp/turns.py:130-133`) because
    the channel wrote the row pre-dispatch. Enqueueing pre-dispatch
    preserves the durability, but on a cancel-before-start the item is
    never consumed, so the text lives in the queue rather than the
    transcript. The webapp must render un-consumed `input` items to keep
    the behaviour user-visible — deliberate work, not a free carry-over.
    (This is why `input` is a queue kind at all: without it, the user's
    text would exist only in the task payload, and a turn cancelled before
    its agent started would vanish.)
  * **Deferred materialisation can never happen.** A hand-back recap
    (#4) lands only if the orchestrator takes another turn. If the user
    walks away, the orchestrator never learns about the delegation — which
    costs nothing, because it has no next turn to be wrong in. The
    delegate's own rows still hold the record of the work.
  * **Input rows need per-task idempotency.** A redelivered task must not
    double-append. A unique partial index on `(session_id, task_id)` for
    `role='user'` input rows makes the append naturally idempotent.
  * **Transcript timestamps shift** by the dispatch latency — the user row
    is stamped when the agent starts, not when the channel received it.
    Sub-second; the webapp's optimistic echo already covers the live view.
  * **The queue is net-new machinery** — a table, a drain step, and a
    consumed-item GC that rides the session cascade.

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
unreachable from the router. After the store move the loop no longer reaps
history or `session_info` — it reaps `cron_jobs` only, which is a small
enough job that folding it into the cron agent is worth considering. The
probe endpoint stays (cron still needs it), so this is a shrink, not a
deletion. Be precise about that in the changelog when it lands.

### 9.3 Per-session serialization moves to the router

With writes centralized, the router can hold a per-`session_id`
`asyncio.Lock` around message/mutate ops — it is single-replica, so that
is a complete guarantee, where the suite's is complete only with Valkey
configured. Two caveats keep `session_lock.py` alive even then: the
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
in-context rows (those above its `history.floor`) can reach several
hundred KB of text. The file store's
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
new `bp_sdk/history.py`. Note what the signatures cannot express:

```python
class SessionHistory:
    # Writes — no `thread` parameter exists. You write your own thread.
    async def append(self, role: str, message: str, *,
                     hidden: bool = False,
                     once_per_task: bool = False) -> int: ...

    # Reads — `thread` IS a parameter; reads are session-scoped (§8).
    async def reload(self, *, thread: str | None = None,
                     up_to_id: int | None = None) -> list[Turn]: ...  # pages internally
    async def recall(self, *, count: int, skip: int = 0,
                     thread: str | None = None) -> list[ToolExchange]: ...
    async def state(self, *keys: str,
                    thread: str | None = None) -> dict[str, str]: ...

    # Hand-over — the only way to reach another agent's context.
    async def hand_over(self, target: str, kind: str, payload: dict) -> None: ...

    def mutate(self) -> SessionMutation: ...   # builder → one atomic MutateRequest
```

The turn-opening idiom, which every l0/l1 handler now shares:

```python
async with ctx.history.mutate() as m:            # ONE transaction, ONE round trip
    for item in await m.consume_pending():       # my queue, drained
        m.append(*materialise(item))             # ...into MY thread, as MY rows
    m.append("user", payload.prompt, once_per_task=True)
```

and the hand-back that used to be a cross-thread write:

```python
await ctx.history.hand_over(ORCHESTRATOR, "handback_recap",
                            {"summary": summary, "reason": reason})
```

The steward side gets the same shape over HTTP in
`bp_agents/agents/chatbot/credentials.py`, alongside the existing named-file
store client — minus `append`, which it has no endpoint for (§7).
`ChannelCore` swaps `self._pool` for that client and keeps its method
signatures.

No LLM tool bundle. `recall_tool_history` is already a suite-side local
tool over `common/tool_history.py`; it re-points at `ctx.history.recall`
and needs no protocol-level tool surface.

## 12. Suite-side deletions — and what the store move does *not* buy

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

So after the store move every one of those agents still opens
`open_pool` — for a single `SELECT * FROM user_config`. The
credential-removal payoff (§1.1) lands only when `user_config` moves too
(§13), and it is worth stating in that order rather than claiming it here.

What the store move *does* buy, on its own: `session.history` becomes
structurally enforceable (§8), session purge and retention go one-sided
(§9), and the write lock moves to the router (§9.3). The ownership
property itself is bought earlier and separately, by the suite rework
(§8.1) — which needs no router work at all.

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
  * **Attribution is structural, not recorded.** Every row's writer *is*
    its thread owner (§8), so there is nothing to forge and nothing to
    cross-check. Today any holder of the DSN can author a row as any
    agent, and the schema keeps no evidence of it.
  * **The steward's remaining authority is bounded.** It can set
    session-scoped state and enqueue pending items — it can ask for
    something to appear in a thread, never make it appear. That is still
    worth an explicit ACL capability (`session.steward`) rather than
    "whoever holds a session token", and steward ops still belong in the
    audit chain: a malicious steward can spam a queue or flip a session's
    delegation, which is a nuisance, not a forged utterance.
  * **The pending queue is the residual trust surface.** An item is
    consumed by the owner and materialised *as the owner's own row*, so a
    poisoned item becomes a real utterance. The owner is the last check:
    treat pending payloads as input, not instructions — the same posture
    an agent takes toward any task payload.
  * **Audit.** Mutating ops append hash-chain audit events
    (`session.message_append`, `session.state_set`, `session.pending_put`)
    the
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
2. **Do the suite rework first, against the suite's own DB** (§8.1). Every
   re-homing — agent-side input rows, delegate-side seed, pending queue,
   floor/episode instead of `incumbent` — is expressible against
   `session_history` today. Landing it before the store move means the
   cutover is a *transport* change, not a semantics change, and each
   behavioural risk in §8.4 gets its own release to surface in. This is
   the single most useful ordering decision in the plan.
3. **Backfill.** `session_history` → `session_messages`,
   `session_info` → `sessions.metadata` + `session_state`. Both databases
   live in one Postgres instance but are **separate databases with
   separate owners** (`deploy/postgres-init/01-create-suite-db.sql:19`), so
   there is no `INSERT … SELECT` across them: use `COPY … TO STDOUT` piped
   to `COPY … FROM STDIN` (a `scripts/` one-shot), or `postgres_fdw` if the
   operator prefers. Preserve `id` values — recall cursors, floors, and the
   summarizer's cutoffs all reference them. Derive the two new bounds from
   the column they replace:
     * `history.floor` per thread = `MAX(id) WHERE incumbent = false`
       (the demoted rows are always a prefix — both suite demote paths are
       `id <= cutoff` or whole-thread, `queries.py:331`, `:355`).
     * `episode` = 1 for every existing row; a session with a live
       `delegated_to` gets `delegation.episode = 1`, so its delegate's
       thread keeps reloading exactly what it does today.
   Verify the derivation by diffing each thread's reload result before and
   after — same rows, or the backfill is wrong.
4. **Flip the readers/writers behind a suite setting**
   (`SUITE_SESSION_STORE=router|suite`, default `suite`). Quiesce first —
   the existing per-session lock is the clean quiesce point — then backfill
   the tail and flip. One release with the flag; no dual-write (two sources
   of truth for a thread's floor is worse than a short maintenance window).
5. **Soak**, then drop `session_info` / `session_history` in a suite
   migration and delete the dead queries (§12).

Step 5 is the irreversible one; keep 1–4 reversible by leaving the suite
tables in place and untouched during the soak.

## 16. Implementation sequence

**Phase 0 — the prefs move (§13).** Smaller, no atomicity, no paging, and
it is what actually starts closing agents' database pools. It also
exercises the KV pattern, the session-JWT HTTP surface, and the backfill
mechanics on a table where a mistake costs a preference, not a
conversation.

**Phase 1 — the ownership rework, suite-side (§8.1), against
`session_history` as it stands.** In dependency order, each independently
shippable:

1. Input rows: agents append their own; delete `record_user_turn` and its
   four call sites; the orchestrator's fallback branch becomes the path.
   Add the per-task idempotency guard.
2. Delegation seed: composed and written by the delegate on `first_turn`
   from the `LLMData` it already receives; delete the orphan-seed rollback.
3. Pending queue + drain-at-turn-start; move the hand-back recap and the
   cron report onto it (cron also gains `report` in its payload).
4. `history.floor` + `delegation.episode` replacing `incumbent`; delete
   both demote paths. Ship behind a read-path flag and diff reload results
   against the old query in staging.
5. `/delegate` writes session state instead of a seed row.

At the end of phase 1 **no code writes another agent's thread**, and the
suite still runs entirely on its own database. That is the checkpoint
worth pausing on.

**Phase 2 — the store move.**

6. Schema + migration (§4), with the FK cascades.
7. `bp_router/session_store.py` — scope keys, the §8 ownership rule, byte
   budget (§10), audit. One module both surfaces call.
8. `SessionOpFrame` / `SessionResultFrame` + dispatch handlers (§5) +
   `mutate` transaction (§6).
9. Session-JWT HTTP endpoints (§7) — note the absence of a messages POST.
10. `bp_sdk/history.py` (§11) + the steward HTTP client.
11. Backfill script (§15 step 3) + the `SUITE_SESSION_STORE` flag.
12. Cut over `ChannelCore`, the orchestrator, `l1_common`,
    `history_summarizer`, `tool_history`, the webapp pages (§12).
13. Shrink the reconcile loop (§9.2); router-side per-session lock (§9.3).
14. Soak, drop suite tables, delete dead code.

Tier resolution (§13, last paragraph) is a separate sequence after all of
it.

## 17. What not to do

  * **Don't add a steward append endpoint or a `thread_agent_id` field**
    "just for the channel". That is the hole (§7, §8); gating it is not
    the same as not having it. If a component needs something in a
    thread, it enqueues (§8.2).
  * **Don't keep `incumbent` as a mutable column.** It reads like a
    harmless flag and it is the last remaining way to reach into another
    agent's thread (§8.3).
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
  * **Don't do the store move before the ownership rework** (§15 step 2).
    Migrating the semantics and the transport in one step means a
    behavioural regression and a data-layer regression are
    indistinguishable in production.
  * **Don't dual-write during migration.** Two writers of a thread's
    floor race in exactly the way the per-session queue exists to prevent.
  * **Don't move cron or the platform mappings** to make the suite
    Postgres "go away" (§2 non-goals). The goal is removing the *seam that
    costs*, not table count.
  * **Don't put message text in audit payloads** (§14).
  * **Don't bundle tier resolution** into either data move (§13) — it
    changes the LLM request path, which has its own identity-derivation
    debt to settle first.
  * **Don't claim the store move closes agents' DB pools.** It doesn't;
    §12 shows why, and §13 is what does.

## 18. Open questions

  * **How un-consumed `input` items render** (§8.4). The text survives a
    cancel-before-start either way; the question is whether the webapp
    shows it as a pending turn, greys it, or drops it from the transcript
    until consumed. Pin it before phase 1 step 1 — it is the one
    user-visible difference in that step.
  * **Turn-level serialization.** §9.3 leaves the suite lock owning turn
    ordering while the router owns store consistency. Is there a
    router-side "session busy" primitive worth having (it would need to
    span dispatch → result, which is a task-tree concept, not a write), or
    does the suite lock stay indefinitely? This decides whether Valkey
    ever stops being the multi-instance prerequisite.
  * **Should a pending item expire?** A `handback_recap` for a
    conversation the user abandoned sits in the queue forever, and it will
    be materialised — stale — if that user returns weeks later. A TTL, or
    a "drop stale kinds on drain" rule, is suite policy the router should
    carry as a column but not decide.
  * **Episode counter vs. per-delegation id.** A monotonic counter is the
    simplest thing that filters correctly (§8.3). A uuid per episode makes
    the audit trail nicer and costs an extra index; worth deciding when
    the delegation docs are updated rather than now.
  * **Does `session_state` need a value history?** Summaries are
    overwritten in place today. A one-deep previous value would make a
    failed summarize apply recoverable, at the cost of a shape decision
    the router shouldn't be making for the suite.
  * **`session_name` in `metadata` vs. a first-class column.** The webapp
    lists and sorts by it; a jsonb key is fine for a few hundred sessions
    per user and awkward beyond that. Measure before promoting.
  * **Should reads be narrowable?** §8 makes reads session-wide because
    the summarizer and delegates need cross-thread reads. A future
    `session.history.read` scope per agent could narrow it, but nothing in
    the current suite wants that.

## 19. Sizing

The file store cost roughly 2,300 LOC of platform code across
`bp_sdk/files.py` (446), `bp_sdk/file_tools.py` (448),
`bp_router/api/files.py` (635), `bp_router/file_store.py` (146), ~371
lines of dispatch handlers and ~246 lines of frames. The session store is
smaller — no blobs, no S3, no dedup, no quota accounting, no LLM tool
bundle:

| piece | estimate |
| --- | --- |
| **phase 1 — ownership rework, suite-side** (§8.1) | ~-150 net (deletes `record_user_turn`, the orphan-seed rollback, both demote paths; adds the queue + drain) |
| frames (`SessionOp` + `SessionResult` + command union) | ~150 |
| router `session_store.py` + queries | ~350 |
| dispatch handlers | ~200 |
| HTTP endpoints | ~180 |
| migration | ~100 |
| `bp_sdk/history.py` + steward client | ~350 |
| suite rewrites (54 call sites) + deletions | ~-400 net |
| backfill script (incl. floor/episode derivation) | ~200 |

Call it ~1,350 LOC new platform code, a net reduction suite-side, and a
migration. The payoff is not lines:

  * **From phase 1 alone, no router work needed:** no code writes another
    agent's thread, the orphan-seed failure mode is gone, and `incumbent`
    stops being mutable state two components fight over.
  * **From the store move:** `session.history` becomes enforceable — and
    enforceable *structurally*, since `AppendRequest` has no thread field
    to forge — plus one-sided session purge and retention, and a
    router-owned write lock.
  * **From the store move plus the prefs move** (§13, the smaller of the
    two): eight of ten agents stop holding a database password.
