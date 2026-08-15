# Deployment: agent host, roster provisioning, one init

> **Status: implemented.** Shipped as `bp_agents/host.py` (supervised
> multi-agent runner), `bp_agents/init.py` (the merged one-shot),
> `invitations.agent_ids` (migration `0012_invitation_roster`) with roster
> support in `bp_agents/bootstrap.py` and `scripts/register-invitations.sh`,
> `scripts/gen_env_reference.py` → `docs/env-reference.md`, and a rewritten
> `docker-compose.prod.yml`: **22 services → 11**, **12 credential vars → 2**,
> **13 state volumes → 4**. Covered by `tests/test_agent_host.py` and
> `tests/test_invitation_roster.py`. Deviations are marked **[shipped]**.
>
> Targets the operator-facing cost of running the suite: 22 compose services,
> 12 mandatory single-use invitation tokens minted on every launch, 13 state
> volumes, and three ordered one-shots — driven by a 710-line compose file and
> an 805-line launcher. None of that is inherent to what the suite *does*.

The thesis in one line: **the deployment unit should be an isolation
boundary, not an agent.** Twelve agents that trust each other equally, share
one image, one tag, and one release do not need twelve containers, twelve
identities provisioned out-of-band, or twelve volumes. The three that *are*
isolation boundaries — the untrusted sandbox, the MCP bridge, the edge —
stay separate, and stay separate for reasons this design does not weaken.

## 1. What it costs today

Measured against the repo as it stands:

| | count | where |
| --- | --- | --- |
| compose services | **22** | 4 infra, 3 one-shots, router, 12 suite agents, 2 optional-profile |
| distinct `${VAR}` references | **83** | `docker-compose.prod.yml` |
| mandatory invitation vars | **12** | all `${X_INVITATION:?}` — a bare `docker compose up` fails without the launcher |
| named state volumes | **13** | one per agent (12) plus the MCP bridge; the shared `lancedb_data` is separate |
| ordered one-shots | **3** | `migrate` → `suite-migrate` → `bootstrap`, each an edge every agent depends on |
| hand-written config | **1,777 lines** | 710 compose + 805 `prod.sh` + 262 env example |
| documented router settings | **24 of 104** | `.env.example` vs. `Settings.model_fields` |

Four causes. Only the last is really about Compose, and only §1.3 is
strictly a documentation problem — but all four land on the same operator
on the same afternoon, which is why they belong in one document.

### 1.1 One process per agent

The deployment unit is the container, so every agent drags an image
reference, an env block, a volume, a `depends_on` set, an invitation, and a
Python interpreter. The interpreter cost is measurable rather than
theoretical:

```
one suite agent (orchestrator) imported          →  40 MB RSS
ELEVEN suite agents imported in ONE process      →  96 MB RSS
```

The heavy dependencies — `lancedb`, `fastapi`, `markitdown`, `asyncpg` — are
loaded once per *process*, not once per *agent*. Eleven containers pay for
eleven copies of them; one host process pays once. That is a ~5× difference
in resident memory for identical behaviour, before per-container overhead.

### 1.2 Per-agent identity, provisioned out-of-band

Each agent needs a single-use invitation. `ensure_credentials`
(`bp_sdk/onboarding.py:125`) only consumes one on *first* boot — afterwards it
loads `credentials.json` from the state volume — but `prod.sh` cannot know
whether a volume survived, so `refresh_invitations` mints twelve fresh tokens
on **every** launch and twelve go unused on every restart. The failure mode is
asymmetric and ugly: a lost volume with no fresh token means that agent can
never start, which is exactly why the launcher over-mints.

Worth noting what those tokens actually are. The `invitations` table has no
`agent_id` column (`0001_initial_schema.py:403`), and `POST /v1/onboard` takes
the name from the agent's own `agent_info.agent_id` — the invitation is
consumed with `used_by` recorded after the fact. So today's twelve tokens are
twelve **unbound bearer credentials** sitting in an env file: any one of them
can onboard as any agent name. Binding them to a roster (§3) is a tightening,
not a loosening.

### 1.3 The env reference documents a quarter of the surface

`Settings` carries **104** fields; `.env.example` documents **24** of them,
and the README describes that file as "every configurable environment
variable (router / agent SDK / suite), grouped with defaults". The
dict-shaped settings — `file_storage_quota_bytes`, `quota_admit_rate_per_s`,
`session_store_quota_bytes` — appear nowhere, so an operator tuning a quota
has to read `bp_router/settings.py` to learn the variable exists.

This is a documentation gap rather than a deployment one, but it lands on
the same person on the same day, and it is the reason "hassle with env var
passing" reads as a deployment problem: the variables you *must* set are
tangled up with variables you cannot discover.

Two changes, both small:

  * **Generate the full reference.** A `scripts/gen-env-reference.py` that
    walks the three settings models and emits every field with its default
    and docstring, checked by a test asserting the committed output matches.
    Then it cannot drift — which is the property `.env.example` was reaching
    for and does not have.
  * **Say what `.env.example` is.** Keep it as the curated quick-start (the
    ~24 variables a deployment actually sets), and correct the README's claim
    to match, pointing at the generated reference for the rest.

### 1.4 Three ordered one-shots

`migrate` (router schema) → `suite-migrate` (suite schema) → `bootstrap`
(register invitations + apply ACL), each a service, each with a
`service_completed_successfully` edge that all twelve agents then declare.
Three places for a boot to fail, three sets of logs to correlate.

## 2. Change 1 — the agent host

`bp_sdk` already supports this. `Agent.run()` is documented as the blocking
entry point for *external* agents, with `run_async()` existing precisely so a
host process can drive several agents on one event loop
(`bp_sdk/agent.py:728-738`). Each agent keeps its own identity, its own WS
connection, and its own position in the ACL — the host is a process, not a
merged principal.

```
python -m bp_agents.host --group suite-core
```

### 2.1 Grouping by isolation, not by count

| group | agents | why grouped |
| --- | --- | --- |
| `suite-core` | orchestrator, deep_reasoning, research, computer_use, knowledge_base, memory, history_summarizer, md_converter, config | equal trust, same image, same release, no privileged requirements |
| `channels` | chatbot, webapp | user-facing edge; worth restarting without touching the workers, and the only ones on the `edge` network |
| `sandbox` | sandbox | **must stay alone** |
| `mcp-bridge` | (profile) | **must stay alone** |

The sandbox is non-negotiable and the reasoning is already in the compose
file: it runs as `user: "0:0"` with `cap_drop: ALL` plus only
`SETUID`/`SETGID`/`CHOWN`, `no-new-privileges:true`, a root-owned `0700` state
dir, and `networks: [agents]` — no database, no Valkey, no web. Root exists
solely so it can *drop* each user's bash to a distinct uid; putting any other
agent in that process would hand it those capabilities and that network
position for free. The MCP bridge is the same argument in miniature: it runs
as root to drop stdio subprocesses to private uids, with root-owned state.

### 2.2 Supervision is the new code

The honest cost of a host is that one agent's unhandled crash must not take
its eight neighbours down. The host supervises each agent as a task, restarts
it with backoff, and logs the transition — roughly fifty lines. Note this is
*better* than the status quo for the surviving agents in a group: today an
OOM or an unhandled exception restarts a container and drops that agent's
in-flight tasks; under supervision the same fault restarts one agent while the
rest keep their sockets and their work.

Two properties must be preserved deliberately:

  * **Non-zero exit on permanent transport failure.** `Agent.run()` raises
    `SystemExit(1)` when the transport is permanently dead so a supervisor
    sees it. The host must propagate the same signal — if *every* agent's
    transport is dead the host should exit non-zero, not sit restarting
    forever behind a healthy-looking container.
  * **Graceful drain on SIGTERM.** The compose `stop_grace_period: 30s` exists
    because SDK agents drain in-flight tasks. The host forwards SIGTERM to all
    its agents and waits for the same window.

### 2.3 [shipped] What a shared process makes per-agent

Three settings are process-wide environment variables that hosted agents
would otherwise share, and each one is a real failure rather than an
inefficiency:

  * **`AGENT_STATE_DIR`** — credentials live at
    `state_dir/credentials.json`, so nine agents in one directory overwrite
    each other's tokens and load someone else's on restart. The host gives
    each `state_dir/<name>/`.
  * **`AGENT_INVITATION_TOKEN`** — one shared token means the first agent to
    onboard consumes it and the rest 403, and in `channels` it would hand the
    chatbot's `provisions_service_user` credential to whichever went first.
    The host resolves `<NAME>_INVITATION` first, roster second.
  * **`SUITE_DB_POOL_MAX_SIZE`** — pool size is per agent, so nine agents at
    the suite default (max 10) is up to 90 connections from one container,
    against a stock `max_connections` of 100. The compose pins 0/3 for
    `suite-core`. This one shrinks on its own as the session-store rework
    removes the suite database from most agents.

### 2.4 What is lost

`docker compose restart research` becomes "restart nine agents". Since those
nine already share one image and one tag and are deployed together, the
practical loss is small — but it is real, and worth stating rather than
discovering. If per-agent restart turns out to matter operationally, the host
can grow a control socket; that is not in v1.

## 3. Change 2 — one roster token, not twelve invitations

Add an optional `agent_ids: list[str]` roster to an invitation. One token,
each listed name consumable exactly once, unlisted names refused.

```
invitations
  + agent_ids   text[]  NULL     -- NULL keeps today's unbound behaviour
  + consumed    text[]  NOT NULL DEFAULT '{}'
```

`POST /v1/onboard` gains one check: when `agent_ids` is non-null, the
requested `agent_info.agent_id` must be in it and not already in `consumed`;
consuming appends the name rather than burning the row. `used_at` is stamped
when the roster is exhausted, so the existing GC sweep still reaps it.

This is what it buys:

  * **Twelve mandatory env vars become one.** `refresh_invitations` shrinks
    from twelve `gen` calls plus twelve env lines to one, and a bare
    `docker compose up` stops failing on `${X_INVITATION:?}`.
  * **The token is bound to names.** §1.2's unbound bearer credential becomes
    a credential that can only produce the agents the operator listed — a
    tightening that also makes the blast radius of a leaked env file smaller.
  * **The host onboards its own group.** Each agent still onboards
    individually against the shared state volume, so a partially-provisioned
    group heals on restart: agents with credentials skip, agents without
    consume their slot.

**`bootstrap` keeps the admin credential.** The host must never hold one. The
split stays: an admin-authenticated one-shot mints the roster token and
applies the ACL; the host holds only a token that can produce the agents it
was given.

**[shipped] Two tokens, not one.** The chatbot's invitation is flagged
`provisions_service_user` — it yields a minting-capable principal — and
that flag is per-*invitation*, not per-name. Bundling it into the roster
would hand eleven ordinary agents the same privilege, so it keeps its own
token: `SUITE_ROSTER_TOKEN` + `CHATBOT_INVITATION`. Twelve mandatory vars
become two rather than one, and the higher-privilege credential stays
visibly separate — which is the better outcome, not a compromise.
`--gen-per-agent` still emits the pre-roster shape for a deployment that
provisions individually.

## 4. Change 3 — one `init`

Collapse `migrate` + `suite-migrate` + `bootstrap` into a single `init`
container running the three in order. One service, one ordering edge for every
group to declare, one place to read a boot failure. The three steps stay
independently runnable for debugging (`python -m bp_agents.init --step acl`),
they simply stop being three Compose services with three sets of
`depends_on`.

## 5. Change 4 — a roster manifest

With §2 the per-agent Compose blocks disappear, but their content has to live
somewhere: research's search-backend keys, memory and knowledge_base's LanceDB
mount, chatbot's channel credentials. That goes in `deploy/suite.roster.yaml`
— agent → group, plus its extra env — which the host reads and the init step
uses to build the invitation roster. Declarative, one file, no YAML anchors.

## 6. Projected result

| | today | after |
| --- | --- | --- |
| compose services | 22 | **11** |
| mandatory invitation vars | 12 | **1** |
| state volumes | 13 | **4** (`lancedb_data` unchanged) |
| credential vars | 12 | **2** (roster + the service-user invite) |
| python processes | ~14 | **5** |
| suite RSS (interpreters) | ~500 MB–1 GB | **~150 MB** |
| ordered one-shots | 3 | **1** |

Services after: `caddy`, `postgres`, `valkey`, `seaweedfs`, `init`, `router`,
`suite-core`, `channels`, `sandbox`, plus `searxng` and `mcp-bridge` on their
existing profiles.

## 7. A free reduction — banked, and smaller than it looked

`[shipped]` The session-store rework plus the `user_config` move
([`router-managed-session-store.md` §13.5`](./router-managed-session-store.md))
took the suite pool count from **ten agents to four**: `chatbot` and `webapp`
(cron + chat mappings), `config` (cron), and `memory` (its GC sweep).
`SUITE_VALKEY_URL` likewise narrowed to the KakaoTalk parked-turn registry.

The container-level saving is smaller than the agent-level one, and that is
worth being straight about: grouping means `suite-core` co-hosts `config` and
`memory` with seven agents that need nothing, so the group still needs the
credential and the `suite` network. What actually landed is that
`SUITE_DATABASE_URL` moved off the shared `&suite-env` anchor onto the two
groups that use it — so `sandbox`, the container that runs untrusted code, no
longer carries a Postgres credential — and that `suite-core`'s connection
ceiling is now bounded by two agents rather than nine.

The grouping and the credential narrowing pull against each other here. If
that matters more than the process count, the lever is the group boundary,
not the env block.

## 8. The single-node profile

The router already supports in-process agents
(`bp_router.embedded.attach_embedded_agent`), which would collapse router *and*
suite into one process: one container plus Postgres, for the "run it on my
NAS" case. Offer it as an explicit, clearly-labelled `all-in-one` image —
**not** as the default path — because two properties do not survive it: a
suite bug becomes a router outage, and the sandbox can never be embedded
(§2.1), so the all-in-one either ships without code execution or ships with a
second container anyway. Naming it honestly is the whole design: an operator
choosing it should know they are trading isolation for one `docker run`.

## 9. What not to do

  * **Don't reach for Kubernetes or Nomad.** The problem is process count and
    a provisioning dance, not scheduling. Either would replace 710 lines of
    Compose with more YAML, not less, and neither removes a single invitation
    token.
  * **Don't put anything in the sandbox's process.** Its capabilities and
    network position are the isolation (§2.1).
  * **Don't give the host an admin credential.** The roster token is scoped to
    names it may create; an admin token is scoped to everything (§3).
  * **Don't merge groups to chase the service count.** `channels` is separate
    because it is the only group on the `edge` network and the only one whose
    restart is user-visible; folding it into `suite-core` saves one service and
    costs a restart-blast-radius property.
  * **Don't make the all-in-one profile the default** (§8).

## 10. Implementation sequence

1. `bp_agents/host.py` — supervised multi-agent runner, signal forwarding,
   non-zero exit on total transport failure.
2. `deploy/suite.roster.yaml` + loader shared by the host and init.
3. Router: `invitations.agent_ids` / `consumed` + the `POST /v1/onboard`
   roster check. Additive — a NULL roster behaves exactly as today.
4. `bp_agents/init.py` — the merged one-shot, with `--step` for debugging.
5. Compose rewrite against the four groups; `prod.sh` loses
   `refresh_invitations`' twelve-token loop.
6. `scripts/gen-env-reference.py` + the README correction (§1.3) —
   independent of everything above, and the cheapest item here.
7. `all-in-one` profile (§8), last and optional.

Steps 1–2 are testable without touching the router. Step 3 is backward
compatible on its own, so it can land and soak before the Compose rewrite
depends on it.

## 11. Open questions

  * **Does `channels` want to be two groups?** chatbot (Telegram/Kakao egress,
    the cron scheduler) and webapp (HTTP, SSE) have different restart profiles
    and different failure blast radii. Two services costs one more container
    and buys independent restarts of the browser channel.
  * **Per-agent restart control.** §2.3 defers it. If it proves necessary, the
    cleanest shape is a host control socket rather than splitting groups back
    apart.
  * **[shipped] `md_converter`'s memory bound now covers its group.** It had
    `mem_limit: 2g` specifically so the cgroup OOM-killer would target a
    ballooning conversion *child* and leave the parent agent up to report the
    failure. In `suite-core` that cap bounds nine agents, so it is raised to
    4g (`SUITE_CORE_MEM_LIMIT`). The mechanism still holds — conversion runs
    in a forked child, which remains the largest process and the killer's
    target — but the margin is now shared. If a pathological document starts
    taking the host down instead of the child, `md_converter` wants its own
    group, and that is the first thing to try.
  * **Roster token TTL.** Today's invitation TTL is 600 s because tokens are
    minted per launch and consumed seconds later. A roster token that survives
    a partial rollout may want longer — but a long-lived multi-name credential
    is exactly the thing worth keeping short. Pin at implementation.
  * **Does `init` belong in the suite package at all?** It applies the *router*
    schema, which is platform, and the *suite* schema plus the suite ACL, which
    is not. Splitting it back into two one-shots along that seam would be more
    honest about ownership and cost one service — the opposite of this
    document's direction, which is why it is a question rather than a decision.
