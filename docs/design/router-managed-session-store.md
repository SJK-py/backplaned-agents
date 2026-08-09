# Router-managed session store

> **Status:** design proposal — not implemented.
>
> **Scope note.** This specifies a **platform service**: a general
> conversation log + session state store owned by the router, designed
> against the four criteria in §2 rather than against the current agent
> suite. The suite is expected to be rebuilt on top of it, so nothing here
> is shaped by today's `bp_agents` code or by migrating its data. §13 maps
> the suite's known behaviours onto the primitives — as a completeness
> check on this spec, and as a target for that rebuild.

The router already owns identity, tasks, files, and sessions. It does not
own what happens *inside* a session — the conversation. That gap is why
every agent in the suite holds a Postgres password (§1). This closes it
the way the named file store closed the file gap: typed frames, scope the
router derives from the task row, and a session-JWT HTTP surface for
gateways.

**One rule shapes everything below: a message is an utterance, so the
thread it lands in is derived, never named.** A file lives in a shared
per-session namespace any peer may write; a message is attributable
speech. An agent writes its own threads and no others — not because a
check rejects the attempt, but because the wire format has nowhere to put
another agent's name (§7).

## 1. The gap today

The suite keeps a second Postgres database (`bp_suite`, separate owner,
`deploy/postgres-init/01-create-suite-db.sql:19`) holding `session_info`,
`session_history`, `user_config`, cron, and platform mappings. Sessions
themselves live in the router (`sessions`, `bp_router/db/models.py:75`),
joined only by `session_id`. What that seam costs:

  * **Ten of twelve agents hold Postgres credentials** — `open_pool` at
    `orchestrator/agent.py:125`, `config:165`, `research:121`,
    `deep_reasoning:122`, `knowledge_base:116`, `chatbot:126`,
    `computer_use:88`, `webapp:87`, `history_summarizer:96`,
    `memory:184`. The platform's premise is that an agent holds no
    infrastructure credentials: no provider keys, no object-store keys —
    but a database password.
  * **`session.history` is a decorative capability.** Seven agents
    advertise it (`orchestrator/agent.py:110`, `research:108`,
    `deep_reasoning:110`, `chatbot:61`, `computer_use:76`, `webapp:47`,
    `history_summarizer:84`); nothing enforces it, because access is raw
    SQL and the thread key is whatever the writer types.
  * **GC and erasure are two-sided.** The router's closed-session sweep
    (`closed_session_retention_days`, `bp_router/settings.py:315`) cannot
    reach `bp_suite`, so a mirror reaper
    (`bp_agents/agents/chatbot/session_gc.py`) polls a bespoke admin probe
    (`GET /v1/admin/sessions/filter-existing`) on the same retention
    default. GDPR erase keys off `users.purged_at` across the two
    databases (`bp_router/db/models.py:45-50`).
    `docs/backplaned/router/state.md` §2.2 says it outright: "The suite's
    conversation history (a separate store the router can't reach)…".
  * **Per-session serialization is reimplemented outside the router** —
    `bp_agents/session_lock.py`, local lock plus optional Valkey — while
    the router is single-replica and already serializes per task.
  * **`session_info` shadows the router's session row**, keyed on the
    same id, duplicating `created_at`/`opened_at`.

## 2. Design criteria

The four the router side is judged against, with what each rules in or
out. Where they conflict, the order below is the tiebreak.

**Compatibility.** The router interprets *nothing* about conversation
content. Roles are opaque strings, not an enum the platform validates
(§3.3). Every message and every state value carries a `metadata` jsonb
the router stores and never reads. New ops extend a discriminated union;
unknown ones fail as `unsupported_op`, not as a parse error, so an old
router and a new SDK degrade legibly (§5.4). Nothing in the schema
encodes "orchestrator", "delegate", "summary", or "episode".

**Versatility.** A thread is `(agent_id, thread_key)`, so an agent can
partition its own context without ever naming another agent (§3.2).
Scopes are `session` and `user`, mirroring the file store's session /
`persist` split, so cross-session agent context is a first-class case and
not a workaround. Retirement is one cursor, not two special-cased
mechanisms (§3.4). Hand-over is a typed queue whose `kind` values are
suite-defined (§3.5).

**Efficiency.** A turn costs **two round trips** — one batch to open, one
to close — because a batch is an ordered list of *mixed reads and writes*
in one transaction (§6). Read bounds are enforced server-side against a
byte budget with a resumable cursor (§9.2). A `StatThread` op answers
"should I summarize?" without transferring the thread (§9.3). Index
design is specified, not left to the implementer (§9.1).

**Completeness.** The op inventory (§5.2) covers what a conversation
store needs beyond append-and-read: redaction with cursor-stable
tombstones, compare-and-swap on state, thread enumeration, cheap
statistics, and idempotent appends. Absent by deliberate choice:
mutation of stored text, server-side search, and change subscriptions
(§15).

## 3. Model

### 3.1 Scope

A thread lives in one of two scopes, mirroring the file store exactly:

  * **`session`** — the default. Bound to one `session_id`; reaped with it
    (§10).
  * **`user`** — cross-session, user-wide. The conversational analogue of
    `persist/`: an agent's standing context with a user. Survives session
    close and purge; reaped only on user purge.

The router derives `user_id` and `session_id` from the task row; the
caller chooses only which of the two scopes it means. A `user`-scoped op
carries no session and is legal from any of that user's sessions.

### 3.2 Thread

A thread is `(scope, owner_agent_id, thread_key)`.

`owner_agent_id` is **always derived** from the task's active executor —
it is not a wire field. `thread_key` is a short caller-chosen string
(default `""`, the agent's main thread) confined to the caller's own
namespace, so partitioning your own context costs nothing while reaching
another agent's remains impossible. A suite that wants exactly one thread
per agent simply never sets it.

### 3.3 Message

Append-only. Fields: `role`, `content`, `hidden`, `metadata`, plus
router-stamped `id`, `created_at`, thread coordinates, and the
originating `task_id`.

`role` is an **opaque short string**. The router does not enumerate it,
does not validate it against a list, and never filters on it implicitly —
every read states the roles it wants (§5.2). This is the most important
compatibility decision here: the moment the platform knows what
"assistant" means, it owns a conversation model.

`content` is text. Large payloads belong in the file stash; a message
references one by name in `metadata`, exactly as an `LlmRequest` does
(`docs/design/router-managed-file-store.md` §8.1). The router caps
`content` (§9.4) rather than becoming a blob store with a second door.

### 3.4 The floor — one cursor, not two mechanisms

Every thread has a **`floor`**: the message id below which rows are no
longer part of the active context. Reads default to `id > floor`.
Retirement of any shape is a floor move:

  * summarization folds a prefix → floor = the cutoff id;
  * an episode ends and the whole thread retires → floor = the thread's
    current max id;
  * nothing is ever mutated, so a full read (`include_retired`) still
    returns the complete record for transcripts and audit.

An earlier draft carried both a `floor` watermark *and* an `episode`
column stamped per row. The episode column is unnecessary: "retire
everything through id N" is a floor move, and a fresh episode is just the
rows after it. One cursor, one column fewer, one less suite concept in
platform schema.

**The floor is owned by the thread owner**, like the messages. When
another component needs a thread retired (a channel ending a delegation),
it enqueues a hand-over carrying an explicit `through_id` snapshot, and
the owner applies it on its next turn, before appending anything new
(§3.5). Lazy application is not a compromise: a thread with no next turn
has no context to bound.

### 3.5 Hand-over queue

The one channel by which anything reaches another agent's context. An
item is `(target_agent_id, thread_key, kind, payload, created_at,
consumed_at)` where `kind` and `payload` are **suite-defined and opaque**.

An item is not a message: no role, no content, no position in any
transcript. The owner drains its queue at turn start and decides what, if
anything, to write into its own thread. The enqueuer proposes; only the
owner speaks.

Two properties make this more than a renamed cross-thread write. An item
is inert until materialised, so an un-drained queue can never appear in a
transcript as something an agent said. And materialisation is an ordinary
owner-authored append, so the ownership invariant holds with no exception
anywhere in the system.

### 3.6 State

A per-scope key/value store: `(scope, session_id?, owner_agent_id?, key)
→ (value, version, metadata)`.

  * **Thread state** (`owner_agent_id` set) — owner-writable only. The
    floor lives here (`sys.floor`, the one reserved key the router
    interprets); everything else is opaque.
  * **Session state** (`owner_agent_id` null) — writable by the steward
    or any executor in the session. Routing flags, delegation pointers,
    whatever the suite needs.

Every write takes an optional `expected_version` for compare-and-swap.
Session state has two legitimate concurrent writers (a steward and an
executor), so CAS is not optional sugar — without it the delegation
pointer is a lost update waiting to happen.

## 4. Schema

Four tables, one migration (`bp_router/db/migrations/versions/0010_*`).

**`session_messages`**

| column | type | notes |
| --- | --- | --- |
| `id` | bigserial PK | cursor for reads, floors, and idempotency |
| `user_id` | text, FK `users` | derived |
| `session_id` | text NULL, FK `sessions` ON DELETE CASCADE | null for `user` scope |
| `owner_agent_id` | text | derived from the task's active executor — never a wire field |
| `thread_key` | text NOT NULL DEFAULT `''` | caller's own namespace |
| `role` | text | opaque; no CHECK constraint, by design (§3.3) |
| `content` | text | capped (§9.4) |
| `hidden` | bool | a rendering hint the router stores and ignores |
| `metadata` | jsonb NOT NULL DEFAULT `'{}'` | extension point; never read by the router |
| `task_id` | text NULL, FK `tasks` | provenance |
| `idempotency_key` | text NULL | set only by an idempotent append; unique per task (§9.1) |
| `redacted_at` | timestamptz NULL | tombstone; `content` blanked, id preserved (§5.2) |
| `created_at` | timestamptz | |

There is **no `incumbent` column** (replaced by the floor, §3.4) and **no
`author_agent_id`** (with the owner derived, the author *is* the owner —
recording it separately records nothing).

**`session_threads`** — one row per live thread, carrying the floor and
the counters that make `StatThread` O(1).

| column | type | notes |
| --- | --- | --- |
| `user_id`, `session_id`, `owner_agent_id`, `thread_key` | | PK |
| `floor_id` | bigint DEFAULT 0 | §3.4 |
| `message_count`, `content_bytes` | bigint | counted **above the floor**; adjusted by delta on append and redact, recomputed by one aggregate over the retired range when the floor moves (rare) |
| `last_message_id`, `updated_at` | | |

Denormalising the counters turns "should I summarize?" from a thread scan
into a single-row read (§9.3), and gives quota enforcement (§10.3) a
number to gate on without an aggregate.

**`session_state`** — `(scope, session_id, owner_agent_id, key) →
(value, version, metadata, updated_at)`. `version` is a bigint bumped on
every write: the CAS token of §3.6. NULL `owner_agent_id` (session state)
is handled by a partial unique index, not a sentinel.

**`session_handovers`** — `(id, user_id, session_id, target_agent_id,
thread_key, kind, payload jsonb, created_at, consumed_at,
consumed_by_task_id)`. Indexed `(session_id, target_agent_id, thread_key)
WHERE consumed_at IS NULL`, so a drain is a bounded index scan.

## 5. Protocol

### 5.1 Frames

```python
class SessionOpFrame(_FrameBase):
    """Agent → router. `task_id` is not trusted as proof: the router
    derives `(user_id, session_id)` from the task row after verifying the
    connection's authenticated agent is that task's active executor
    (`attachments.derive_task_file_scope`), and that same derivation
    supplies `owner_agent_id` for every write. No op below carries a
    writable owner field."""
    type: Literal["SessionOp"] = "SessionOp"
    task_id: str
    scope: Literal["session", "user"] = "session"
    ops: list[SessionOp]        # ordered; one transaction; §6
```

```python
class SessionResultFrame(_FrameBase):
    """Router → agent. Correlated reply. `results` is positional — one
    entry per op, same order, each a typed per-op outcome. A set `error`
    means the whole batch was refused and NOTHING was applied."""
    type: Literal["SessionResult"] = "SessionResult"
    ref_correlation_id: str
    error: str | None = None
    results: list[SessionOpResult] = []
```

Every request is a batch, minimum length one, so there is a single
request shape to implement, test, rate-limit, and audit. Convenience
wrappers live in the SDK, not the protocol.

### 5.2 Op inventory

Writes — all confined to the caller's own threads:

| op | shape | notes |
| --- | --- | --- |
| `Append` | `{thread_key, role, content, hidden, metadata, idempotency_key}` | supplying `idempotency_key` makes the append exactly-once within its task: a redelivered task appends nothing and gets the original id back. Omit it and the same task may append many rows of the same role, which is the normal case |
| `SetFloor` | `{thread_key, floor_id}` | monotonic; a lower value is refused `floor_regression` |
| `SetState` | `{thread_key\|null, key, value, expected_version, metadata}` | null value deletes; CAS via `expected_version` (§3.6) |
| `Redact` | `{message_ids}` | blanks `content`, stamps `redacted_at`, **keeps the id** so floors and cursors stay valid |
| `HandOver` | `{target_agent_id, thread_key, kind, payload}` | the only cross-agent write (§3.5) |
| `ConsumeHandovers` | `{kinds, limit}` | drains **my** queue; items come back in the batch results |

Reads — session-scoped, since reading cannot fabricate (§7):

| op | shape | notes |
| --- | --- | --- |
| `Read` | `{owner_agent_id, thread_key, roles, include_retired, since_id, before_id, limit, max_bytes, order}` | the one read. Defaults to `id > floor`, ascending, caller's own thread |
| `GetState` | `{owner_agent_id\|null\|"*", keys}` | `"*"` returns per-thread maps |
| `StatThread` | `{owner_agent_id, thread_key}` | `{message_count, content_bytes, last_message_id, floor_id}` from one row (§9.3) |
| `ListThreads` | `{owner_agent_id\|null}` | thread coordinates plus their stats |

`Read` deliberately subsumes what an earlier draft split into `reload`
and `recall`: "the active user/assistant turns" and "a page of past tool
exchanges" are one query with different `roles` and cursors. One read
path to index, budget, and optimise.

### 5.3 Errors

`denied` (unknown task, not the active executor, or a write outside the
caller's own threads — deliberately non-enumerable, as the file ops'
`denied` is), `session_closed`, `version_conflict` (CAS),
`floor_regression`, `content_too_large`, `quota_exceeded`,
`unsupported_op`, `rate_limited`, `batch_too_large`.

### 5.4 Forward compatibility

  * The op union is discriminated on `kind`; a router that does not know
    a `kind` refuses that batch with `unsupported_op` and applies
    nothing, rather than failing the frame at parse time. New ops are
    additive for old routers and legible to new SDKs.
  * `WelcomeFrame` gains `features: list[str]` — a defaulted field, so
    older SDKs are unaffected — advertising e.g. `session_store.v1`. Note
    the existing `WelcomeFrame.capabilities` is **not** this: it echoes
    the connecting agent's own capability list (`ws_hub.py:630`) and
    cannot carry router features.
  * `metadata` on messages and state values absorbs suite-specific fields
    without schema changes — the pressure valve that keeps
    `history_summary`-shaped columns out of platform tables.

## 6. Batches: mixed reads and writes, one transaction

`ops` is an ordered list applied in a single transaction, all-or-nothing,
returning positional results. Reads observe the writes that precede them
in the same batch.

This is the design's main efficiency lever and its main correctness lever
at once. A turn opens with

```
[ConsumeHandovers(kinds=[…]), Append(input), GetState(keys=[…]), Read(…)]
```

— one round trip that drains the queue, records the turn's input, fetches
the caller's own summary/config state, and returns the bounded context,
atomically. It closes with

```
[Append(assistant), Append(tool_call), Append(tool_result), SetState(…), SetFloor(…)]
```

— one more. **Two round trips per turn**, against five or more if each op
were its own frame; and every multi-write sequence a suite might need
(apply-a-summary, end-an-episode, record-and-retire) is atomic by
construction rather than by a bespoke compound command.

What the router still refuses to offer: a `summarize` op, a `delegate`
op, an `end_episode` op. Primitives and a transaction boundary; the
policy that composes them stays in the suite. Cross that line and the
platform is encoding one conversation model for everyone.

Cap batch length (suggest 32 ops) and total write bytes so a transaction
cannot be held open indefinitely; `batch_too_large` is the refusal.

## 7. Authorization

| op class | permitted actor |
| --- | --- |
| append / redact / set floor / thread state | **the thread's owner only** — and the owner is derived, so there is no field in which to name another agent |
| session state | steward, or any executor in the session (CAS-guarded) |
| hand-over put | steward, or any executor in the session |
| hand-over consume | the target agent only |
| reads | any agent acting in the session |

Two consequences worth stating plainly.

**The rule is structural, not enforced.** `Append` has no owner field;
the router fills it from the task's active executor. There is no check to
misconfigure and no ACL rule to get wrong, because the malformed request
is unrepresentable. This is also why the HTTP surface (§8) has no
message-POST endpoint: a gated one would reintroduce exactly the hole the
wire format closes.

**Reads stay session-wide, deliberately.** A summarizer must read the
thread it summarizes; a delegate reads what a peer left for it. Reading
cannot fabricate, so the file store's "shared-session reach is
intentional" applies unchanged. Narrowing reads per agent is possible
later (§15); nothing needs it today.

Writes to a **closed** session are refused (`session_closed`), matching
the `NewTask` admit rule (`docs/backplaned/router/state.md` §2.2). Reads
of a closed session are allowed — transcripts outlive conversations, and
reopen must not lose them.

## 8. HTTP surface

For gateways: agents that spawn tasks but are never a task's active
executor, and so have no task from which scope can be derived. Precedent
and shape follow `/v1/files/names`.

| endpoint | use |
| --- | --- |
| `GET /v1/sessions/{id}/messages` | transcript rendering; cursor-paginated, `roles` + `include_retired` filters |
| `GET /v1/sessions/{id}/threads` | thread list plus stats |
| `POST /v1/sessions/{id}/handovers` | enqueue for an agent |
| `POST /v1/sessions/{id}/ops` | a batch restricted to session state, hand-over, and reads |
| `GET\|PATCH /v1/sessions/{id}/state` | session-scoped keys only |
| `PATCH /v1/sessions/{id}` | session `metadata` (title, channel, whatever the suite puts there) |

**There is no message-POST endpoint, by design.** A steward holding a
session JWT cannot append to any thread, at any time, for any reason. It
enqueues.

Auth is the caller's session JWT, ownership-checked against
`sessions.user_id` exactly as `POST /v1/files` is. Both surfaces call one
module (`bp_router/session_store.py`, sibling to `file_store.py`) so they
cannot drift.

## 9. Efficiency

### 9.1 Indexes

  * `session_messages (session_id, owner_agent_id, thread_key, id)` — the
    read path: a range scan over a narrow partition, with `roles` and
    `redacted_at` applied as filters on the scanned rows.
  * `session_messages (user_id, owner_agent_id, thread_key, id) WHERE
    session_id IS NULL` — the `user`-scope read path.
  * `UNIQUE (task_id, idempotency_key) WHERE idempotency_key IS NOT
    NULL` — makes an idempotent append a no-op on redelivery without a
    read-then-write race. Keying on the caller's explicit key rather than
    on `(task_id, role)` matters: one task legitimately appends several
    rows of the same role (a tool_call and its tool_result, two tool
    calls in one turn), and a role-keyed constraint would reject them.
  * `session_handovers (session_id, target_agent_id, thread_key) WHERE
    consumed_at IS NULL` — partial, so drains never scan consumed rows.
  * `session_threads` is PK-only; every stat is a single-row fetch.

### 9.2 Read budget and resumption

`max_payload_bytes` is 1 MiB per WS frame (`bp_router/settings.py:269`;
the SDK's receive ceiling is 2 MiB, `bp_sdk/settings.py:102`). A long
thread's active window can approach it, and unlike files there is no
indirection available — the agent needs the text.

So the router enforces the budget rather than hoping. `Read` fills to
`min(max_bytes, budget)` where `budget` defaults to ~60% of the
negotiated payload cap, walks **backwards from newest** so the most
recent turns always survive truncation, and returns `truncated_before_id`
when it stopped early. The SDK pages transparently and presents one list.
The HTTP transcript endpoint is the unbounded path.

Operators can enable WebSocket `permessage-deflate` for a large win on
conversational text; it is a CPU-for-bandwidth trade on a single-replica
router, so it is a lever, not a default.

### 9.3 Cheap decisions

`StatThread` exists so a steward can decide to summarize without
transferring a thread: `content_bytes` above the floor comes from one
`session_threads` row. Token estimation stays caller-side — the router
has no tokenizer and must not pretend otherwise. Bytes are the honest
proxy it can serve in O(1).

### 9.4 Write path cost

Appends ride the router's pool alongside task admit. Three mitigations,
specified rather than left open: batch the turn's writes (§6) so a turn
is two round trips; cap `content` (suggest 256 KiB, `content_too_large`
beyond, with the file stash as the documented alternative); and size
`db_pool_max_size` for the added per-turn writes before enabling the
service. A dedicated pool is available if measurement justifies it, but
one pool with correct sizing is the simpler default.

## 10. Lifecycle, GC, quota

### 10.1 Close vs purge

Unlike the file stash, **history is not GC'd on session close.** Sessions
reopen; transcripts outlive conversations. Close does nothing to the
store. Purge (`DELETE …?purge=true`) and the closed-session retention
sweep take messages, threads, state, and hand-overs through the
`ON DELETE CASCADE` on `session_id` — one-sided, no reconcile loop, no
`filter-existing` probe.

`user`-scoped threads survive both and are reaped by `purge_user` in the
same transaction that scrubs the user row, so `users.purged_at` stops
being a cross-database signal for conversation data.

### 10.2 Consumed hand-overs

Swept on the session cascade, plus a short retention for audit. An
un-consumed item lives until its target drains it or the session dies; a
suite that wants staleness rules puts a timestamp check in its drain
(§15).

### 10.3 Quota

Per-user `content_bytes` summed over `session_threads`, ceiling by user
level, gated on append — the same shape as the file store's storage
quota, reusing its enforcement point. A per-session message ceiling
bounds the pathological case a byte ceiling misses (a million empty
rows).

## 11. SDK surface

`ctx.history`, shaped like `ctx.files`, in `bp_sdk/history.py`. Note what
the signatures cannot express:

```python
class SessionHistory:
    # Writes — no owner parameter exists, at any level. You write yours.
    async def append(self, role: str, content: str, *, thread: str = "",
                     hidden: bool = False, metadata: dict | None = None,
                     idempotency_key: str | None = None) -> int: ...
    async def set_floor(self, floor_id: int, *, thread: str = "") -> None: ...
    async def redact(self, *message_ids: int) -> int: ...

    # Reads — owner IS a parameter; reads are session-scoped (§7).
    async def read(self, *, owner: str | None = None, thread: str = "",
                   roles: list[str] | None = None, since_id: int | None = None,
                   include_retired: bool = False) -> list[Message]: ...  # pages internally
    async def stat(self, *, owner: str | None = None,
                   thread: str = "") -> ThreadStat: ...
    async def state(self, *keys: str,
                    owner: str | None = None) -> dict[str, StateValue]: ...

    # Hand-over — the only way to reach another agent's context.
    async def hand_over(self, target: str, kind: str, payload: dict, *,
                        thread: str = "") -> None: ...

    def batch(self) -> SessionBatch: ...   # one transaction, one round trip
```

```python
async with ctx.history.batch() as b:            # the turn-opening idiom
    items = b.consume_handovers()               # my queue
    b.append("user", payload.prompt, idempotency_key="input")
    state = b.get_state("summary")
    turns = b.read(roles=["user", "assistant"])
# exiting the context manager resolves every handle, positionally
```

Scope selection is a client construction (`ctx.history.user_scope`), not
a parameter on every call, so the common case stays terse.

No LLM tool bundle. Unlike files, there is no case for handing a model
raw write access to a conversation log; a suite that wants a recall tool
builds it over `read`, as the current one does over
`common/tool_history.py`.

## 12. Security

  * **Derived identity, one pattern.** Scope resolution reuses
    `attachments.derive_task_file_scope` (`bp_router/attachments.py:25`)
    — no second derivation to get wrong.
  * **Attribution is structural.** A row's writer is its thread's owner;
    there is nothing to forge and nothing to cross-check.
  * **The steward's authority is bounded.** Session state and enqueueing.
    A malicious steward can spam a queue or flip a routing flag — a
    nuisance, not a forged utterance. Still worth an explicit ACL
    capability (`session.steward`) and audit coverage.
  * **The hand-over queue is the residual trust surface.** A poisoned
    item becomes a real utterance once its owner materialises it. Owners
    must treat payloads as untrusted input, exactly as they treat task
    payloads — this is where prompt injection reaches a thread, and the
    SDK docs should say so at the `consume_handovers` call site.
  * **Audit** on mutating ops (`session.append`, `session.redact`,
    `session.state_set`, `session.handover`), hash-chained as file
    mutations are. **Never** put `content` in an audit payload: ids,
    coordinates, role, and byte counts only. Conversation text in an
    append-only chain is an erasure problem `purge_user` cannot solve.
  * **Redaction is real.** `Redact` blanks stored content rather than
    hiding it behind a flag, so a user-facing "delete this message" is
    honest at the storage layer while ids stay stable.
  * **DoS.** Batch caps (§6), content cap (§9.4), quota (§10.3), and the
    existing per-agent rate limiter (`rate_limited`).

## 13. Reference mapping — a suite on these primitives

Not a migration plan; a completeness check, and a target shape for the
suite rebuild. Every behaviour the current suite gets from
`session_history` + `session_info`, expressed in §5 ops — and note that
none of them writes another agent's thread:

| suite behaviour | today | on these primitives |
| --- | --- | --- |
| record the user's turn | channel writes a `user` row into the target's thread (`core.py:101`) | channel `HandOver(kind="input")`; the target materialises it in its opening batch |
| build context | `reload_incumbent` | `Read(roles=["user","assistant"])` — floor applied server-side |
| assistant turn, tool rows | agent appends its own | unchanged — `Append`, batched at turn close |
| recall past tool results | `recent_tool_exchanges` | `Read(roles=["tool_call","tool_result"], before_id=…)` |
| rolling summary | `session_info.history_summary`, channel-written | thread state `summary`, owner-written |
| apply a summary | set summary + `demote_incumbent_through` | `[SetState(summary), SetFloor(cutoff)]`, one batch |
| decide to summarize | agent measures context, reports it in metadata | steward `StatThread` — no transfer |
| delegation seed | orchestrator writes into the delegate's thread (`:259`) | the delegate composes it from the `LLMData` the hand-off already carries (`prompt`, `agent_instruction`, `context`) and appends to its own thread |
| sticky delegation pointer | `session_info.delegated_to` | session state, CAS-guarded |
| end an episode | `demote_thread` on the delegate's thread | `HandOver(kind="retire", through_id=N)`; the owner sets its floor on next use |
| hand-back recap | channel writes `user`+`assistant` into the orchestrator's thread (`core.py:355`) | `HandOver(kind="recap")`, materialised by the orchestrator |
| cron report | channel writes an `assistant` row (`cron.py:166`) | pass the job's report policy in the task payload; the orchestrator is already that task's executor and appends its own row |
| session title, channel, chat id | `session_info` columns | session `metadata` on the router's session row |
| webapp transcript | direct SQL | `GET /v1/sessions/{id}/messages` |

Two failure modes disappear rather than move. The orphan-seed rollback
(`orchestrator/agent.py:270-277`) exists only because the seed is written
before the reassignment that can fail — composed at the far end, that
window does not exist. And a cancelled turn no longer leaves a dangling
`user` row: the un-consumed hand-over is the durable record, and the
webapp renders it as pending or drops it, its choice.

The suite's remaining Postgres use after this is cron and platform
mappings. `user_config` should follow the same path — a per-user state
surface on the router, with presets typed because the router already owns
the catalog and the tier gate (`bp_router/llm/service.py:205-257`) — and
that, not this, is what finally closes those ten pools. Worth building
first: it is smaller, has no atomicity requirement, and exercises the same
KV and HTTP shapes.

## 14. What not to do

  * **Don't add an owner or thread-owner field to `Append`, or a
    message-POST endpoint.** Gating is not the same as absence (§7, §8).
  * **Don't let the router interpret `role`.** No enum, no CHECK, no
    implicit filter. Reads name the roles they want (§3.3).
  * **Don't reintroduce a mutable `incumbent`-style flag.** One floor
    cursor covers prefix and whole-thread retirement (§3.4).
  * **Don't add `summarize` / `delegate` / `end_episode` ops.**
    Primitives plus a transaction boundary (§6).
  * **Don't GC history on session close** (§10.1) — the file store's
    close-time GC is the wrong precedent here.
  * **Don't put `content` in audit payloads** (§12).
  * **Don't let `content` grow unbounded.** The stash is the answer for
    large payloads; a second blob path is not (§9.4).
  * **Don't skip CAS on session state.** Two writers exist by design
    (§3.6).

## 15. Open questions

  * **Change notification.** The webapp polls or rides progress frames
    today. A `SessionEvent` push (new message in a session you can read)
    would serve live transcripts and multi-viewer cases, but it is a
    fan-out mechanism on a single-replica router and wants the scrutiny
    `ProgressFrame` got. Deferred, not dismissed.
  * **Search.** A `tsvector` on `content` plus a `Search` op is a small
    addition and an obvious want ("what did we decide about X?"). Left
    out of v1 to avoid designing ranking semantics into the platform
    before a caller needs them; the column can be added without a
    protocol change.
  * **Turn-level serialization.** The router can lock per session around
    store ops, which is complete on a single replica. It does not cover
    dispatch → result (a task-tree concept), so a suite needing turn
    ordering still needs its own lock. Whether the platform should offer
    a session-busy primitive at all is a real question — it would make
    the suite's Valkey lock unnecessary.
  * **Per-agent read narrowing** (§7). Nothing wants it yet; the shape
    would be an ACL scope, not a schema change.
  * **Hand-over staleness.** TTL, drop-on-drain, or leave it to the
    consumer? The router can carry a column and enforce nothing, which is
    probably right, but decide before the first suite builds on it.
  * **`user` scope and quota.** Session and user threads share one
    ceiling in v1, as session and `persist/` files do. Split if
    cross-session context turns out to be the abuse vector.

## 16. Sizing

For reference, the file store cost ~2,300 LOC of platform code:
`bp_sdk/files.py` (446), `bp_sdk/file_tools.py` (448),
`bp_router/api/files.py` (635), `bp_router/file_store.py` (146), ~371
lines of dispatch handlers, ~246 lines of frames. This service is
comparable in surface but simpler per op — no blobs, no S3, no dedup, no
signed URLs, no LLM tool bundle:

| piece | estimate |
| --- | --- |
| frames + op union + results (`bp_protocol`) | ~250 |
| `bp_router/session_store.py` — ownership, floors, budget, CAS, quota | ~450 |
| queries + migration | ~350 |
| dispatch handler (one frame, batch executor) | ~200 |
| HTTP endpoints | ~200 |
| `bp_sdk/history.py` (client, batch builder, paging) | ~400 |

~1,850 LOC, self-contained, with no suite dependency and no migration.
What it buys the platform: conversation becomes a first-class managed
resource alongside identity, tasks, and files; `session.history` becomes
structurally enforceable rather than advisory; session purge, retention,
and GDPR erase become one-sided; and any suite built on the platform —
the rebuilt one included — gets a conversation store without a database
credential.
