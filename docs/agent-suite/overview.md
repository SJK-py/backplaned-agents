# Agent Suite — Overview

> The first-party agent application that runs **on top of** the Backplaned
> router/SDK. Backplaned provides transport, task lifecycle, delegation,
> the file store, ACL, the LLM service, and per-user sessions; the agent
> suite layers conversation, orchestration, memory, knowledge, scheduling,
> and channels on top.
>
> Companion docs in this directory:
> [`agents.md`](./agents.md) ·
> [`acl.md`](./acl.md) ·
> [`channel.md`](./channel.md) ·
> [`sessions.md`](./sessions.md) ·
> [`delegation.md`](./delegation.md) ·
> [`cron.md`](./cron.md) ·
> [`memory.md`](./memory.md) ·
> [`data-model.md`](./data-model.md)

## 1. What the platform gives us (and what we build)

| Concern | Provided by Backplaned | Built by the suite |
| --- | --- | --- |
| Transport / auth | WS + HTTP, agent JWTs, per-user sessions | — |
| Task lifecycle | admit / ack / result / cancel / deadline | turn orchestration |
| Agent-to-agent | `spawn` (parent/child) + `delegate` (task reassignment) | the **two-layer delegation** model ([delegation.md](./delegation.md)) |
| Files | router-managed named store (`ctx.files`, name-addressed) | knowledge base, sandbox workspace I/O |
| ACL | firewall rules over groups/capabilities | the suite's [rule set](./acl.md) |
| LLM | `ctx.llm.generate` / `embed`, presets, tools, streaming | the agent loops, prompt construction |
| Sessions | a `session_id` + close GC | the suite's **own** conversation/memory model |

The suite keeps its **own** Postgres + per-user LanceDB ([data-model.md](./data-model.md)); the router's `session_id` is the join key but the router never sees conversation history.

### 1.1 Platform prerequisites (confirm before building)

The suite assumes these Backplaned features. Most are baseline; the first three were added specifically to support the suite — a cold-start build should confirm they're present:

- **Root-task injection (B1).** A channel agent can submit a *parentless* `NewTaskFrame` carrying the end-user's `(user_id, session_id)` over its own WS; the router admits it (the channel is the caller; lineage validation is skipped when `parent_task_id is None`). No router change — already supported. See [channel.md §4](./channel.md).
- **Service-principal provisioning at onboarding.** An invitation flagged `provisions_service_user` makes `/v1/onboard` create a co-located `usr_service_{agent_id}` (`level=service`) and return its refresh token, so a channel bootstraps its service identity without a manual `create_user` + env-seeded token. See [`../security.md` §3.2](../security.md).
- **Session-authed named-store endpoints.** `POST /v1/files/names`, `GET /v1/files/names`, `GET /v1/files/names/resolve` let a gateway agent (no task context) store/fetch stash files **by name** under its session JWT. See [`../design/router-managed-file-store.md` §6](../design/router-managed-file-store.md).
- **`serviced_by` auto-grant** at registration approval (§2.1), the **named file store** with LLM-feed-by-reference (§2.4, §6), and per-user **sessions** with close-GC.

## 2. Foundations (read these first)

Almost every design decision downstream traces to one of these.

### 2.1 Per-user identity via `serviced_by`

Everything — files, memory, sessions, knowledge — is **per end-user**, and the router *derives* `user_id` from the task (an asserted `user_id` is only a data-integrity check; authority is established upstream — `security.md §8.3`). So the chatbot must operate as a `service`-level principal (its co-located `usr_service_{agent_id}`, provisioned at onboarding — [channel.md §3](./channel.md), [`../security.md` §3.2](../security.md)), **mint a per-user session credential** for each user (using its `serviced_by` rights), and submit every task under the **end-user's** `(user_id, session_id)` — never under its own service principal (that would commingle all users' data and evaluate the ACL at `service` level).

**The `serviced_by` grant is automatic — not a manual step.** When the chatbot submits a registration *as a service principal*, the router records it as `submitted_by_service_user_id` on the pending row (`api/registrations.py`); on **admin approval** the new user is created with `serviced_by = [chatbot]` and an **initial session is opened** in the same transaction (`api/admin.py::approve_registration`) — that session is the seed for `default_session_id` ([cron.md](./cron.md)). So the chatbot only needs to (a) submit registrations as a service principal and (b) use the resulting `serviced_by` rights to mint per-user credentials. *(A non-service submitter gets no auto-grant; it would need the explicit F8 admin grant endpoint.)*

### 2.2 Two serialization domains

The router serializes per **task**, never per **session** or per **user**. The suite adds two of its own:

- **Per-`session_id` FIFO queue** (in the channel) — serializes message turns + summarization so history reads/writes never race. In-memory for a single channel instance; **Redis** (or session→worker affinity) when the channel is multi-worker. See [sessions.md](./sessions.md).
- **Per-`user_id` lock** (in the memory agent) — serializes `memory.add` + GC, because the fact-graph is per-user and LanceDB is non-transactional. See [memory.md](./memory.md).

`memory.add` is **not** in the session queue (it's per-user, and a multi-LLM-call extraction shouldn't block the next message).

### 2.3 The channel is the session manager

The message-dispatching agents (chatbot, webapp) own the session queue **and** all session-info writes — they hold `session.management`. Consequences:

- They write the rolling summaries and observe delegation transitions to maintain `delegated_to` (see [delegation.md](./delegation.md)).
- Worker agents (orchestrator, l1, …) hold only `session.history` (read context, append their own turn rows). The orchestrator does **not** hold `session.management`.

### 2.4 File model — named store, with one workspace exception

Files use the **router-managed named store** (`ctx.files`, name-addressed, per-user, S3-backed, fed to the LLM *by reference*) everywhere **except the sandbox**, which keeps a container filesystem workspace bridged via `stash_to_workspace` / `workspace_to_stash`. Two boundary cases:

- **Channel inbound/outbound:** the channel is a gateway with no `ctx.files` (that API is handler-bound to a task's active executor, which the channel — a spawner — never is). It uses the **session-authed named-store HTTP endpoints** (`POST /v1/files/names` to bind, `GET /v1/files/names[/resolve]` to list/resolve — [`../design/router-managed-file-store.md` §6](../design/router-managed-file-store.md)) under its per-user session JWT, with the *same* dedup / scope / quota as the agent frames. See [channel.md §7](./channel.md).
- **Sandbox:** code execution needs a real filesystem, so the sandbox owns a per-user container workspace and bridges files in/out by name.

*(The prior Gemini suite used a shared filesystem workspace for everything; the named store — newer — buys per-user isolation, no shared volume, and LLM-feed-by-reference, at the cost of these two bridges.)*

### 2.5 Channel runtime & task injection (B1)

The channel is a long-running **gateway**, not a handler-agent: `on_startup` launches the inbound poll loop + the cron daemon, and each turn is injected as a **root task on behalf of the user** over the channel's WS (`outbound_admit` / `outbound_await_result`), awaiting the result + progress by correlation. Runtime, credentials, slash commands, and the injection primitive are in [`channel.md`](./channel.md).

## 3. Layer model

Groups are Backplaned `groups`; the ACL routes on them.

| Group | Definition | Members |
| --- | --- | --- |
| **l0** | Orchestration / routing / session entry | `orchestrator` |
| **l1** | **Delegatable specialists** — callable as a subagent *or* handed the session as the active delegate | `computer_use`, `research`, `deep_reasoning` |
| **l2** | **Non-delegatable LLM helpers** — invoked as a tool/subagent, never become the session delegate | `config` |
| **l3** | LLM-backed tools (structured input) | `knowledge_base`, `memory`, `history_summarizer` |
| **l4** | Non-LLM tools | `md_converter` |
| **channel** | User-facing dispatchers + session managers | `chatbot`, `webapp` |
| **infra** | Containerized execution environment | `sandbox` |

> The l1/l2 split is about **delegation**, not "uses an LLM" — every l1/l2 agent is LLM-based. l1 agents can take the wheel of a session; l2 cannot.

## 4. Agent roster

| Agent | Group | One-liner |
| --- | --- | --- |
| `orchestrator` | l0 | Personal assistant; runs the main agent loop, delegates, executes subagent/cron tasks |
| `computer_use` | l1 | Coding / computer tasks via the sandbox |
| `research` | l1 | Web + RAG + document research; owns the knowledge base |
| `deep_reasoning` | l1 | Planning / multi-step reasoning with a plan-execute loop |
| `config` | l2 | Conversational user-config management |
| `knowledge_base` | l3 | Per-user document store (LanceDB) + retrieval |
| `memory` | l3 | Per-user fact graph (LanceDB) + retrieval |
| `history_summarizer` | l3 | Rolling conversation summarization |
| `md_converter` | l4 | File / webpage → Markdown (MarkItDown) |
| `sandbox` | infra | Containerized Debian workspace per user |
| `chatbot` | channel | Telegram channel + session manager + cron scheduler (v1) |
| `webapp` | channel | Web (browser) channel + session manager |

## 5. Scope

- **v1 (shipped):** the full roster above, including both channels
  (`chatbot` Telegram + `webapp` browser). The cron scheduler + routing
  live in the chatbot.
- The cron scheduler is **channel-agnostic** (fire → persist → route to the
  user's reachable channel): a cron whose target session isn't live-reachable
  persists its result and nudges the user via Telegram ([cron.md §6](./cron.md)).

## 6. Output convention (applies everywhere)

Every mode returns a Backplaned **`AgentOutput`**: `content` (text, directly LLM-feedable) + `files` (router-store **names**, fed to the LLM by reference via the router's type routing — image/pdf → multimodal, text → inlined, other → a "not multimodal" note). Callers thread results with `Message.tool_response_from_result` (LLM path) or read `.content`/`.files` directly (programmatic path) — **no per-agent result parsing**. The only control flag rides `metadata` (`cron`'s `report`; the `context_tokens` summarization hint). No agent defines a bespoke `produces_schema`.
