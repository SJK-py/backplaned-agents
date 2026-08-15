# Agent Suite — Session Model, History & Summarization

> How an agent's LLM context is built, kept bounded, and serialized.
> **The conversation is the router's**, not the suite's: it lives in the
> router-managed session store
> ([`../design/router-managed-session-store.md`](../design/router-managed-session-store.md)),
> and this document describes how the suite uses it. Delegation
> interactions are in [`delegation.md`](./delegation.md); what the suite
> still keeps in its own Postgres is in
> [`data-model.md`](./data-model.md).

## 1. Where session state lives

Nothing here is a suite table any more. Four namespaces, all the router's:

| What | Where | Who writes it |
| --- | --- | --- |
| conversation turns | `session_messages`, one **thread** per owning agent | that agent, and only that agent |
| a thread's rolling summary | that thread's **state** (`summary`) | that thread's owner |
| `delegated_to` | **session state** (session-scoped) | the channel |
| channel, chat id, title | the router session's **`metadata`** (`kind` / `external_id` / `title`) | the channel |

The single rule the shape follows: **an `Append` carries no owner field.**
The router stamps the task's active executor, so writing another agent's
thread is not rejected — it is unrepresentable. Everything that used to
cross that line now goes through the hand-over queue instead (§4).

The channel's view is a **steward** one (`bp_agents/channel/store.py`): it
may read any thread in the session, drive session state, enqueue
hand-overs, and take the turn lease. It may not append. There is no
message-POST endpoint to append with.

## 2. The conversation log

Each message carries `owner_agent_id` (whose thread), `role`, `content`,
`hidden`, and a session-wide monotonic `id`. Roles the suite uses:

| Role | Written by | In a reloaded context? |
| --- | --- | --- |
| `user` | the executing agent, from its task payload | yes |
| `assistant` | the executing agent | yes |
| `tool_call` / `tool_result` | the executing agent, `hidden` | **no** — recall only |

**The user's words ride the task payload.** The channel used to write the
`user` row before dispatch; it can't now, and doesn't need to — the payload
already carries the text, and the agent appending it under its own
authorship is the property the store exists to guarantee. `common.thread.
open_turn` does it as the turn's opening act. A file the user attached is
saved to the stash and **named in the prompt**, so the same append records
it.

### 2.1 Reload (building an agent's context at task start)

```
context = system_prompt(general + user-config note + this thread's summary)
        + Read(roles=["user", "assistant"])      # above the thread's floor
```

The **floor** is what bounds it: a monotonic per-thread cursor below which
messages leave the active window. It replaces the old `incumbent` flag and
covers both prefix folding (§3) and whole-episode retirement (§4).
Retired messages are not deleted — a transcript read passes
`include_retired=True` and sees the whole conversation.

`tool_call` / `tool_result` rows are never reloaded: the live loop holds
the full sequence in memory, and `CONTEXT_ROLES` is what keeps them out of
the next turn. **Therefore the terminal `AgentOutput.content` must be
self-contained** (or surface the outcome as a named file).

> **One opt-in exception — `recall_tool_history`.** Every stateful turn
> persists its tool exchanges as two hidden rows each, in the same batch as
> the assistant row. The orchestrator and delegated l1 turns carry a
> `recall_tool_history(count, skip)` local tool the model calls to re-read
> its OWN thread's earlier tool results on demand, capped per-result and in
> total so recall can't re-bloat context. It reads with
> `include_retired=True` on purpose: once a fold has compressed the prose
> away, the tool detail behind it is exactly what recall is for. See
> [`../design/agent-tool-history-recall.md`](../design/agent-tool-history-recall.md).

## 3. Rolling summarization

A thread's summary lives in **its own thread state** and is **rendered into
the system prompt** — not placed as a message row (no chronology problem)
and not duplicated as history (the full log is the durable record).

**The owner folds its own thread, at the start of its own turn.** It has to
be the owner: only the owner may move its own floor. Doing it at the start
rather than the end is what makes it worth doing — the fold shrinks *this*
turn's context, which is the reason to fold at all.

```
open_turn        →  read this thread's active window + its summary
maybe_fold       →  over the soft limit and enough turns to compress?
                    ├─ spawn history_summarizer(agent_id, up_to=cutoff)
                    └─ [SetState(summary), SetFloor(cutoff)]   ONE batch
run the loop     →  on the folded context
close_turn       →  tool rows + the assistant row, ONE batch
```

The apply is one batch because the two halves are only correct together: a
crash between them either re-folds those turns into the summary next pass
(double-counting them) or retires content the summary never got. The
cutoff is the oldest ~70% of the active window, and only when there are at
least six turns to compress.

The summarizer is **read-only** and structurally cannot apply what it
produces — it isn't the thread's owner. It reads the target thread (and
that thread's current summary) with `owner_agent_id`, which exists for
reads and deliberately does not for writes.

A fold that fails — too few turns, a summarizer error, an empty summary —
leaves the turn untouched and it runs on the context it has. A failed fold
must never cost the user their answer.

## 4. Reaching another agent: the hand-over queue

The only route into another agent's context. An item is not a message: no
role, no content, no place in a transcript, until the target materialises
it under its own authorship (`common.thread.open_turn`). The suite's
vocabulary:

| kind | from → to | materialised as |
| --- | --- | --- |
| `seed` | channel → delegate | a hidden `user` row (the `/delegate` summary) |
| `recap` | channel or orchestrator ← delegate's work | a hidden `user` row — external input, so the model can't narrate it as its own |
| `retire` | channel/orchestrator → delegate | a `SetFloor`, nothing rendered |

An orchestrator hand-off needs no queue item at all: it carries `LLMData`,
and the delegate composes its own opening row from it
(`l1_common._seed_text`). That also removed the orphan-seed rollback the
old shape needed — a seed written before a reassignment that could fail.

> **Ordering caveat.** The router flips a task's active executor to the
> delegate *before* delivering it, so an agent stops being able to append
> to its own thread the moment `ctx.peers.delegate(...)` is called.
> Everything it wants recorded must be written first.

## 5. Per-session serialization

One turn at a time per session, using the router's **FIFO turn lease**
(§6.4 of the store design) — `ChannelCore.turn(user_id, session_id)`.

- **Why:** the router serializes per *task*, never per *session*. Without
  ordering, two turns build context from the same snapshot and the second
  answers a question the first already moved past.
- **Fairness is the router's:** waiters queue by ticket, so the steward
  polling a 409 (each carrying the holder's remaining TTL as `Retry-After`)
  costs nothing. The ticket, not the retry timing, decides who runs next.
- **Multi-instance for free.** This replaced `session_lock.py` — an
  in-process `asyncio.Lock` plus an optional Valkey lock with a renewal
  watchdog. A webapp and a Telegram bot now serialize against each other
  through the router with nothing shared, so **`SUITE_VALKEY_URL` is no
  longer the prerequisite for a second channel instance** (the KakaoTalk
  channel still needs it for its parked-turn registry).
- **Out of the lease:** `memory.add` and session titling, both
  fire-and-forget after the turn. Cron doesn't take it at all — the
  orchestrator records its own run, so the scheduler only delivers.

Safety does not depend on the lease. Every write is guarded independently:
`AssertThread` on the id a turn opened at, CAS on state, monotonic floors,
idempotency keys. The lease buys **coherence**; the assertions buy
**correctness**.

## 6. System-prompt composition

- **Orchestrator (`message`):** general instruction + user-config note
  (name, timezone, custom note) + the orchestrator thread's summary.
- **Delegation (`on_delegation` / `delegated_message`):** general
  delegation instruction + the agent's own instruction + user-config note +
  that delegate thread's summary. The delegation seed is a `user` row in
  the thread, not a prompt section.

## 7. What the suite still keeps

Per-user config (`user_config`), cron, and chat platform mappings. Full
schema in [`data-model.md`](./data-model.md).
