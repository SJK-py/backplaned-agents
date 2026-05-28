# Agent Suite — Channel Runtime & Inbound

> How a channel agent (chatbot v1, webapp v2) actually runs: identity,
> credentials, **task injection (B1)**, progress, slash commands, and file
> I/O. The channel is a long-running **gateway**, not a normal handler-agent.
> Read with [`overview.md`](./overview.md), [`sessions.md`](./sessions.md),
> [`cron.md`](./cron.md), [`delegation.md`](./delegation.md).

## 1. Runtime shape — a gateway, not a handler-agent

The channel is an SDK `Agent` (`groups=[channel, inbound]`, `capabilities=[channel.telegram]`) but on the normal path it serves **no inbound router modes**. It uses `@agent.on_startup` to launch:

- the **Telegram long-poll loop** (`getUpdates`, persisted offset across restarts),
- the **cron daemon** ([cron.md](./cron.md)).

Each inbound message spawns a per-message handler task (tracked for graceful shutdown and `/stop`). The **normal reply is the awaited result** of the task the channel injected — *not* a call into a channel mode. (`message_to_user` / `file_to_user` remain only as optional *proactive*-push modes; the common path never uses them.)

## 2. Identity resolution

```
chat_id ──(suite_platform_mappings)──▶ user_id ──(user_config.default_session_id)──▶ session_id
```

- `suite_platform_mappings(platform, chat_id) → user_id` is populated by the **admin approve-registration** flow ([overview §2.1](./overview.md)).
- The chat's active session is the user's `default_session_id`; `/new` rotates it.
- An **unmapped** chat gets the `/register` prompt. `/register` submits `POST /v1/registrations` as the channel's service principal (`usr_service_{agent_id}`, §3); on admin approval the router creates the user with `serviced_by=[usr_service_{agent_id}]` and opens the initial session (the `default_session_id` seed).

## 3. Credentials — three identities

| Identity | Purpose |
| --- | --- |
| **Agent JWT** (handled by `bp_sdk`) | the channel-as-agent WS identity, for frame routing + task injection |
| **Service principal** `usr_service_{agent_id}` | the channel's own `level=service` user, **provisioned at onboarding** — an invitation flagged `provisions_service_user` makes `/v1/onboard` create the user and return its refresh token on the `OnboardResponse`, which `bp_sdk` persists into `credentials.json` and rotates via `/v1/auth/refresh`. With its `serviced_by` rights it admin-mints per-user refresh tokens (`POST /v1/admin/users/{id}/refresh-tokens`) and password-reset tokens. See [`../security.md` §3.2](../security.md). |
| **Per-user refresh chains** (a token cache) | one per mapped user, lazy, bootstrapped via the service-mint. Used for per-user **HTTP** ops (incl. file I/O, §7) |

**Split of mechanisms:** task **injection** rides the **agent WS** (§4); session **lifecycle** (`/new` open-session, `/stop` cancel, `/password` mint) **and file I/O** (§7) ride the **per-user HTTP token**.

> **No env-seeded service token.** The service identity is delivered by onboarding, so the channel only needs its (admin-issued, `provisions_service_user`-flagged) agent invitation to bootstrap both identities — there is no separate admin `create_user` + env-injected refresh token step.

## 4. Task injection (B1)

The channel injects a user turn as a **root task on behalf of the user**, over its own WS:

```
task_id = await outbound_admit(
    agent, destination_agent_id=dest, payload=ChannelMessage(prompt=…),
    user_id=…, session_id=…, timeout_s=ACK_TIMEOUT_S,
)                                            # builds a parentless NewTaskFrame
                                             # (user's user_id/session_id), sends,
                                             # awaits the admit ack → task_id
result = await outbound_await_result(
    agent, task_id=task_id, timeout_s=…, on_progress=cb,
)                                            # awaits the correlated ResultFrame;
                                             # cb runs per ProgressFrame
```

- **`parent_task_id = None`** and `(user_id, session_id)` set to the *serviced* user — not inherited from a `ctx` (there is none). This is the one thing `peers.spawn`/`delegate` can't do (they're handler-bound and inherit `ctx`).
- **Admit/await split**: `outbound_admit` returns the `task_id` *before* the (possibly minutes-long) result, so the channel records `current_task_id` per chat → `/stop` can cancel it.
- **Routing**: `dest = delegated_to` when a delegation is active ([delegation.md](./delegation.md)), else the orchestrator.
- **Why the router accepts it (settled).** The router already admits an *agent-submitted, parentless* task: the admit path treats the channel agent as the caller (a real agent) and **skips parent-lineage validation when `parent_task_id is None`** (`bp_router/tasks.py` — the root-task path). The asserted `user_id` is a data-integrity check, not authorization (`../security.md §8.3`); admit re-validates `(user_id, session_id)` is a real owned session, which it is because the channel minted it. **No router change is needed for B1** — the channel is a normal WS agent that simply spawns without a parent.
- **Implementation**: a suite helper over the agent's dispatcher (`transport.send` + the correlation map). Strong candidate to **promote to a supported SDK API** (e.g. `agent.spawn_root_for_user(...)`) instead of reaching into dispatcher internals.

## 5. Progress / verbose

Agents emit a structured **`LoopProgress`** in `ProgressFrame.metadata` ([data-model.md](./data-model.md)). In **verbose** mode the channel passes an `on_progress` callback to `outbound_await_result` and renders **one Telegram message per frame**; non-verbose suppresses interim output. Effective verbose = `/v` one-shot prefix > `user_config.verbose_default` > false.

Every verbose line leads with a **`💭` marker** so it's visually distinct from the final answer (which has none). When the session is **delegated**, both the specialist's verbose lines and its final reply are prefixed with a **`[<Specialist> Agent]`** tag (derived per-frame from the emitting/producing `agent_id`, prettified) — the orchestrator's own lines stay untagged, so the user can see exactly when a specialist holds the session. The delegation **transition** tools (`hand_off` / `end_delegation`) render as `Delegating to a specialist…` / `Handing back to the assistant…`.

## 6. Slash commands (intercepted; never reach an agent)

| Command | Effect |
| --- | --- |
| `/new` | open a new session (rotate `default_session_id`; archive + move the pointer off the old session) |
| `/stop` | cancel the in-flight `current_task_id` (per-user HTTP cancel) |
| `/register [email]` | submit a pending registration (email optional) |
| `/password` | mint a one-time password-setup token |
| `/config [text]` | bare → show config; `<text>` → NL update via the config agent, **bypassing the orchestrator**; never written to history |
| `/v <text>` | one-shot verbose override |
| `/help` | command reference |

Delegation-control commands (e.g. `/unlink`) follow [`delegation.md`](./delegation.md). Slash handling fires **after** the `/v` strip, so `/v /register` still routes to registration.

## 7. Files

The channel is a **gateway, not a task handler**, so it has no `ctx.files` (that API binds to a running task's active executor, which the channel — a spawner — never is). It uses the **session-authed named-store HTTP endpoints** instead, authenticated with the per-user session JWT (§3); they share the agent frames' dedup / scope / quota, so HTTP and frame stores behave identically ([`../design/router-managed-file-store.md` §6](../design/router-managed-file-store.md)). At the channel boundary:

- **Inbound:** download the Telegram upload → `POST /v1/files?session_id=…` (stores the blob, scoped to the user's session) → `POST /v1/files/names` to bind it to a name in the **session scope** → append the `(incumbent=T, hidden=T)` "user-attached file saved as `{name}`" history row *before* dispatching the message ([sessions.md](./sessions.md)). The message payload stays `{prompt}`; the orchestrator discovers the files from that row / `ctx.files.list()`.
- **Outbound:** files an agent produced arrive as **names** on `result.output.files`; the channel resolves each via `GET /v1/files/names/resolve` (→ `file_id` + a fetch key) and pulls the bytes from `GET /v1/files/{file_id}`, then sends them (`send_named_file`, shared by the message path and the cron scheduler). A user-facing agent populates `output.files` by calling its **`send_file(name)`** tool — the orchestrator (`message` / `cron_message`), an l1 in a delegated turn, and the F1 fallback all carry it. Files are sent only when explicitly named this way; scratch files an agent writes to the stash are not auto-delivered.
- Only the **sandbox** uses a filesystem workspace, bridged to the named store via `storage_to_workspace` / `workspace_to_storage` ([agents.md](./agents.md)).

## 8. History ownership

The channel is the **sole writer of user turns** (a single `append_user`); each agent appends **only its assistant turn**. User turns are stored **verbatim — no timestamp prefix** (it pollutes prompt-cache hit rates and litters the rendered transcript). Agents that need the wall clock call the **`current_time` tool** registered on every l0/l1 agent ([agents.md](./agents.md)).
