# Agent Suite — Delegation Lifecycle

> How a session is handed to an l1 specialist and handed back, mapped onto
> Backplaned's `delegate` (task reassignment) and `spawn` primitives. This
> is the highest-risk area for task-termination bugs — the invariants in §5
> are load-bearing.

## 1. Two layers — keep them distinct

- **Router `delegate`** — reassigns a task's `active_agent_id`; the new executor **must terminate that task**. Used **only** for the two transitions (hand-off, hand-back).
- **Session `delegated_to`** (session-info) — the persistent routing flag the channel reads. Steady-state turns during delegation are plain **`spawn`s** (channel → delegate), not router delegations.

So one delegation episode = a delegated **hand-off** task, then N **spawned** steady-state tasks, then a delegated **hand-back** task.

**Delegation is persistent — it is not a one-shot call.** A delegate's
**first** turn (`on_delegation`) is therefore *not* offered `end_delegation`:
it must do substantive work and terminate the hand-off task `T` itself. The
hand-back tool appears only on **subsequent** turns (`delegated_message`).
This is also why the router is right to reject a hand-back on `T` (see §4
F6): `T` originated at the orchestrator, so delegating it back there is a
cycle. For genuinely **one-shot** work the orchestrator must call the
specialist's stateless `subagent` mode (a peer-tool call it awaits inline),
**not** `hand_off`.

## 2. `delegated_to` is channel-maintained by observing the result source

The channel owns `delegated_to` (`session.management`) and derives it from **who produced the result** vs **who it dispatched to** — no orchestrator→channel signaling:

- dispatched `orchestrator(message)` but `ResultFrame.agent_id` is a delegate ⇒ a hand-off happened ⇒ set `delegated_to = <delegate>`.
- dispatched `<delegate>(delegated_message)` but the result is from `orchestrator` ⇒ hand-back ⇒ `delegated_to = null`.
- **spawns don't trip this** — a spawned subagent reports as a *child*; the *parent* still terminates the dispatched task, so `agent_id` is unchanged. Only `delegate` reassigns the terminator.

`on_delegation` / `end_delegation` therefore **never write `delegated_to`**.

## 3. The three phases (who terminates each task)

### Phase 1 — Hand-off (orchestrator → delegate), via `delegate`

1. Channel spawns `T = orchestrator(message)`. The orchestrator's LLM elects to delegate to e.g. `computer_use`.
2. Orchestrator writes the **`delegate_prompt` seed row** into the delegate thread ([sessions.md §6](./sessions.md)), then `ack = peers.delegate(computer_use, LLMData{instruction, context, prompt}, mode=on_delegation)` → router reassigns `T`.
3. **On ack**, the orchestrator returns **without a Result** (SDK suppresses it). **On admit failure** it must produce a fallback instead (F1).
4. `computer_use.on_delegation` runs the first delegated turn on `T`, streams via `ctx.progress`, appends to its thread, and **terminates `T`**.
5. Channel (awaiting `T`) receives computer_use's `AgentOutput`, sends it to the user, and — observing `result.agent_id = computer_use ≠ orchestrator` — sets `delegated_to = computer_use`.

→ **Exactly one terminal Result on `T`, produced by computer_use.**

The first turn **cannot** elect to end (no `end_delegation` tool); it always
produces a Result on `T`. The episode ends later, in Phase 3, on a steady-state
`Tn`.

### Phase 2 — Steady state (per user message), via `spawn`

1. Channel reads `delegated_to = computer_use` → spawns `Tn = computer_use(delegated_message, {prompt})`.
2. computer_use runs, streams, appends to its thread, terminates `Tn`. Channel relays. `delegated_to` unchanged.

→ Each `Tn` is independent; computer_use terminates each.

### Phase 3 — Hand-back (delegate → orchestrator), via `delegate`

1. During some `Tn`, computer_use elects to end (its LLM calls the `end_delegation` local tool, or the user issues a slash command).
2. `peers.delegate(orchestrator, {delegation_summary, exit_reason, user_prompt?}, mode=end_delegation)` → router reassigns `Tn`; computer_use returns **without a Result**.
3. `orchestrator.end_delegation`: appends a `{delegate, summary, reason}` recap to the **main** thread (`user`-role, `(incumbent=T, hidden=T)`); flips the **delegate episode's** rows (including the `delegate_prompt` seed) `incumbent=false`; then **if `user_prompt`** runs the orchestrator loop on it, **else** returns a brief/empty `AgentOutput`. **Terminates `Tn`.**
4. Channel gets the orchestrator's Result and — observing `result.agent_id = orchestrator` — sets `delegated_to = null`. Next message routes to `orchestrator(message)`.

→ Symmetric with hand-off; `Tn` terminated by the orchestrator.

**ACL touchpoints:** Phase 1 `l0/*→l1/*`; Phase 2 `channel/*→l1/*`; Phase 3 `l1/*→l0/agent.orchestration`.

## 4. Failure modes & rules

| # | Failure | Rule |
| --- | --- | --- |
| **F1** | `delegate` admit fails at hand-off (rejected / ack-timeout / disconnected) | `delegate()` raised — **don't** silently finish; the orchestrator produces a fallback Result. The channel sees the result from `orchestrator` ⇒ `delegated_to` stays `null`. |
| **F2** | a delegated task (`on_delegation` / `delegated_message`) **fails** after the session was marked delegated | On a **failed** result from the delegate, the channel reverts `delegated_to` (and may surface an error / route the next turn to the orchestrator). Otherwise the session is stuck routing to a broken delegate. |
| **F3** | concurrent messages / an end-of-delegation routing race | Resolved by per-session serialization ([sessions.md §4](./sessions.md)) — the routing read for turn N+1 happens only after turn N committed its `delegated_to` change. |
| **F4** | a slow first delegated turn hits `T`'s **original** deadline | `delegate` reassigns but does **not** reset the deadline. Use generous deadlines for delegatable agents (or extend at hand-off). Steady-state `Tn` each get a fresh per-turn deadline. |
| **F5** | a delegate wants to switch to a *third* specialist | **Single-level only** — a delegate has `end_delegation`, not `delegate`. It ends back to the orchestrator, which then re-delegates. `delegated_to` stays single-valued; the task chain stays flat. |
| **F6** | a delegate tries to hand back on the **hand-off task `T`** (e.g. it decides it's done on the first turn) | Forbidden by construction: the first turn isn't offered `end_delegation`, so it terminates `T` itself. If it *were* attempted, the router rejects it as a **delegation cycle** — `T` originated at the orchestrator. One-shot needs belong in a `subagent` call, not a delegation. |

## 5. Invariants

1. **Exactly one terminal Result per task.** At each transition the *new* executor terminates; the handing-off agent returns result-less. Zero or two results on a task are both bugs.
2. **Transitions use `delegate`; steady state uses `spawn`.** Never spawn a hand-off (two live tasks) and never `delegate` a steady-state turn (premature termination).
3. **`delegated_to` ⇔ a live delegation**, maintained solely by the channel via result-source observation (incl. the F2 error path).
4. **Single-level delegation.** No chained/nested delegation.

## 6. User- and slash-initiated delegation

A user can request delegation (slash command / UI). Route it **through the orchestrator** — the channel dispatches `orchestrator(message)` with the request, and the orchestrator decides + executes the hand-off as in Phase 1. This keeps the delegation decision and the `delegate_prompt` seed in one place; the channel still learns `delegated_to` by observing the result source.
