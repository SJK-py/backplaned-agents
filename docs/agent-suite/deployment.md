# Agent Suite — Deployment

> How to run the v1 suite (Telegram + KakaoTalk chatbot + webapp browser
> channel + orchestrator + specialists)
> on top of a Backplaned router. The router's own deployment (Postgres,
> Valkey, file store, edge proxy, secrets) is in
> [`../backplaned/deployment.md`](../backplaned/deployment.md); this covers the **suite**
> layer. `docker-compose.prod.yml` ships a complete reference topology.

## Topology

- **One image** (`Dockerfile.suite`) serves every agent; each container
  picks its agent via `SUITE_AGENT` and runs `python -m
  bp_agents.agents.$SUITE_AGENT`. v1 runs **one container per agent**.
- Agents connect to the router over WebSocket (`AGENT_ROUTER_URL`) and
  reach the suite Postgres + per-user LanceDB directly. They hold **no**
  provider / S3 / JWT secrets — LLM calls go through the router's
  `ctx.llm`, and files through the router's file endpoints.
- Networks: every agent is on `agents` (router WS); suite agents that
  touch the suite DB are also on `suite`; the chatbot (Telegram egress) and
  the webapp (fronted by Caddy) are additionally on `edge`; the **sandbox**
  is on `agents` only. The optional KakaoTalk channel adds no inbound
  surface — the chatbot pulls its turns outbound from a Cloudflare Queue,
  and reaches the in-cluster Valkey over `suite` (so enabling Kakao needs no
  new network).

## Databases

The router uses `bp_router`; the suite uses its own `bp_suite`
(`deploy/postgres-init/01-create-suite-db.sql` creates it). Apply both
schemas once, as one-shot jobs (never on container start):

```
alembic upgrade head                          # router  (migrate service)
alembic -c alembic_suite.ini upgrade head     # suite   (the `init` service)
```

Each database is **one migration**: `0001_initial_schema` for the router,
`0001_suite_initial` for the suite. On an empty database those commands do
the whole job; the compose `migrate` / `init` one-shots run them for you.

> ### ⚠️ Upgrading from a build before 2026-08-18 requires a data wipe
>
> Both Alembic chains were consolidated back down to a single baseline, so
> every revision after `0001` was deleted. A database created by the older
> chain has an `alembic_version` naming a revision that no longer exists,
> and `alembic upgrade head` against it **fails**:
>
> ```
> ERROR [alembic.util.messaging] Can't locate revision identified by
> '0013_code_agents'
> ```
>
> That failure is the intended behaviour, not a bug to work around — the
> tooling is telling you the truth, that there is no upgrade path. **The
> deployment stops there and nothing starts against a stale schema**:
> alembic exits 255, `bp_agents.init` stops at the first failing step
> rather than continuing to the ACL bootstrap, and every agent group
> declares `init: {condition: service_completed_successfully}`. You get a
> failed one-shot, not a half-migrated system.
>
> Do **not** try to `alembic stamp` your way past it: stamping asserts the
> schema already matches the baseline, and on a pre-existing database that
> is a claim nobody has checked. The supported move is to start the
> databases empty:
>
> ```bash
> scripts/prod.sh          # choose the "reset" action — down -v
> ```
>
> (`reset` asks you to type `reset` to confirm before it removes anything.)
>
> **`reset` deletes every data volume**, not just Postgres: Valkey,
> SeaweedFS (the file store), LanceDB (memory + knowledge base), and the
> agents' `credentials.json`. Users, sessions, conversations, files, and
> memories do not survive it. If any of that matters to you, take a
> `pg_dump` and copy the volumes **before** resetting — this repo ships no
> migration path back in, so a dump is a record, not a restore route.
>
> After the reset the normal first-boot flow applies with **no env-file
> changes**: `init` re-registers the same `SUITE_ROSTER_TOKEN` /
> `CHATBOT_INVITATION` values already in `deploy/.env.prod` (registration is
> idempotent and the tokens are caller-supplied, not minted), the agents
> re-onboard against a wiped `state_dir`, and the router re-seeds the first
> admin from `BOOTSTRAP_ADMIN_*`. Registered end users must register again.
>
> A **fresh** install is unaffected — this is only about databases that
> already carry an older `alembic_version`.

## Invitations — two tokens, not twelve

Agents onboard with admin-issued invitations, and there's **no mint → paste
round-trip**: `POST /v1/admin/invitations` accepts a caller-supplied `token`,
so you set the values and register those same values.

**Two env vars, both required** (compose refuses to start without them):

| env var | covers | notes |
| --- | --- | --- |
| `SUITE_ROSTER_TOKEN` | the eleven agents that do **not** provision a service user — `webapp`, `orchestrator`, `history_summarizer`, `memory`, `knowledge_base`, `md_converter`, `config`, `deep_reasoning`, `research`, `computer_use`, `sandbox` | one token with an **agent-name roster**; each name consumable once |
| `CHATBOT_INVITATION` | `chatbot` only | registered `provisions_service_user=true` — it bootstraps the `usr_service_chatbot` principal used for registration + per-user minting |

The chatbot's stays separate deliberately: its invitation yields a
minting-capable principal, and the other eleven agents must not inherit that
flag. And the roster is **tighter** than the per-agent tokens it replaced,
not merely fewer — an invitation with no roster is an *unbound bearer
credential*, because `POST /v1/onboard` takes the agent name from the agent's
own `agent_info`, so any such token can onboard as any name. A roster token
is bound to its list, stays live until the list is exhausted, and therefore
lets a partially-provisioned group heal on restart.

```bash
# 1. generate both tokens
scripts/register-invitations.sh --gen >> deploy/.env.prod

# 2. register them (compose's `init` one-shot already does this;
#    run it by hand only for a non-compose deploy, once the router is up)
ROUTER_URL=https://your.domain scripts/register-invitations.sh deploy/.env.prod
```

Both steps are idempotent — re-running registers nothing new and exits 0.
Step 2 is `bp_agents/bootstrap.py`'s shell equivalent and registers the same
two things it does; the compose path runs the Python one via `init`.

**Per-agent tokens are still supported** for a deployment that registers
agents individually: `--gen-per-agent` emits one `<AGENT>_INVITATION` line
each, and the register step picks up whichever are set. Leave
`SUITE_ROSTER_TOKEN` empty if you go that route — and note that the agent
*containers* only receive the vars `docker-compose.prod.yml` passes them, so
per-agent tokens beyond the chatbot's need a compose edit as well.

The dev launcher `scripts/run-suite.sh` mints + starts the whole roster
automatically for a local router.

## Per-agent configuration

- `AGENT_ROUTER_URL` — `ws://router:8000/v1/agent`
- `AGENT_STATE_DIR` — persists `credentials.json` (+ chatbot's Telegram
  offset); give the chatbot a volume.
- `SUITE_DATABASE_URL` — `postgresql://…@postgres:5432/bp_suite`. Needed by
  **four** agents only: `chatbot` and `webapp` (cron + chat mappings),
  `config` (cron), and `memory` (its GC sweep). The rest read the
  conversation from the router's session store and the user's settings from
  its user scope, so the reference compose sets this per service rather than
  on the shared env anchor — `sandbox`, which runs untrusted code, carries no
  database credential at all.
- `SUITE_LANCE_ROOT` — per-user LanceDB root (`/lancedb`; shared volume
  for `knowledge_base` + `memory`).
- chatbot Valkey: `SUITE_VALKEY_URL` (db 1; db 0 is the router's) backs the
  **KakaoTalk** channel's parked-turn registry, and nothing else. It is no
  longer what makes two channels serialize — turn ordering is the router's
  per-session FIFO lease ([sessions.md §5](./sessions.md)), so a webapp and a
  Telegram bot order their turns through the router with nothing shared
  between them. The reference `docker-compose.prod.yml` defaults it on;
  a deployment without KakaoTalk can unset it.
- chatbot: `SUITE_TELEGRAM_BOT_TOKEN` (Telegram).
- chatbot (KakaoTalk, optional): an egress-only second channel. The agent
  **pulls** turns from a Cloudflare Queue fed by the
  [`deploy/kakao-relay`](../../deploy/kakao-relay/) Worker — it opens no
  inbound port. Gate it with `SUITE_KAKAO_CF_ACCOUNT_ID` /
  `SUITE_KAKAO_CF_QUEUE_ID` / `SUITE_KAKAO_CF_API_TOKEN` (a token scoped to
  Queues pull+ack); it uses the same `SUITE_VALKEY_URL` above for its
  deadline / next-touch registry (so keep Valkey on when Kakao is enabled).
  Outbound images additionally need the `SUITE_KAKAO_R2_*` vars (a
  presigned-URL bucket); inbound images reuse the router file store. Design
  + the relay/queue setup: [`../design/kakao-channel.md`](../design/kakao-channel.md)
  and [`deploy/kakao-relay/README.md`](../../deploy/kakao-relay/README.md).
  Approved Kakao registrations reconcile to `platform=kakao` via a second
  approval poller, mirroring Telegram.
- webapp: `WEBAPP_SESSION_SECRET` (signs the browser session cookie;
  required). Serves FastAPI on `:8002`, fronted by Caddy on its own host
  (`WEBAPP_DOMAIN`, default `app.<PUBLIC_DOMAIN>`) — it serves from root, so
  it can't share the router's domain where `/admin` lives. HTTP ops use the
  logged-in user's own token (no service principal). Optional
  `WEBAPP_USE_BUILT_CSS=true` swaps the Tailwind CDN for a pre-built
  stylesheet (see `bp_agents/agents/webapp/tailwind.config.js`).
- research web search: `SUITE_WEB_SEARCH_BACKEND` picks the backend —
  `searxng` (default; `SUITE_SEARXNG_URL` → bundled or external endpoint),
  `brave` (`SUITE_BRAVE_API_KEY` → Brave's LLM-Context API), or `kagi`
  (`SUITE_KAGI_API_KEY` → Kagi FastGPT for search + Extract for `html_fetch`).
  See [Web search](#web-search) below.
- LLM presets are router-side (`llm_presets` table). The suite names a
  **slot** (`pro` / `balanced` / `lite`) and the router resolves it from the
  user's own preference and their tier gate, defaulting to
  `ROUTER_LLM_DEFAULT_PRESETS`. The one preset the suite still names outright
  is `SUITE_DEFAULT_PRESET_EMBEDDING` — not a slot, because changing an
  embedding model invalidates every vector already stored.

## ACL

Apply the suite firewall rule set once after first boot (admin
credentials in env):

```
python -m bp_agents.load_acl        # PUT /v1/admin/acl/rules
```

This replaces the router's ACL with `bp_agents.acl.suite_acl_rules()`.

## Web search

`SUITE_WEB_SEARCH_BACKEND` selects how the research agent's `web_search`
(and, for Kagi, `html_fetch`) works:

| Backend | Key / config | Behaviour |
|---|---|---|
| `searxng` (default) | `SUITE_SEARXNG_URL` | Classic metasearch — returns a list of result links (title/url/snippet). |
| `brave` | `SUITE_BRAVE_API_KEY` | Brave's [LLM-Context API](https://brave.com/search/api/) — returns AI-grounded context. `web_search` exposes `country` / `search_language` / `count` / `freshness` / `local_city` (the last is sent as the `X-Loc-City` header for location-aware results). |
| `kagi` | `SUITE_KAGI_API_KEY` | Kagi [FastGPT](https://help.kagi.com/kagi/api/fastgpt.html) — returns an AI answer with cited sources; `html_fetch` routes URLs through Kagi's [Extract](https://help.kagi.com/kagi/api/) API (batch, Markdown). |

The chosen backend's key must be set — if it's missing the agent **falls back
to SearXNG** and logs a warning, so `web_search` only goes fully dark when
neither a key nor a SearXNG URL is configured. `prod.sh` prompts for the
backend and its key/URL.

### SearXNG

The `searxng` service is behind the `search` compose profile — enable it
with `docker compose --profile search up`, or leave it off and set
`SUITE_SEARXNG_URL` to an external instance. With neither, `web_search`
returns a "not configured" notice; the rest of research still works.

The bundled instance mounts `deploy/searxng/settings.yml`, which enables the
**JSON output format** and the **GET method** that `web_search` relies on
(`GET /search?format=json`). The stock SearXNG image defaults to
`formats: [html]` and `method: POST`, so without this both the format and the
method are refused and SearXNG answers **403 Forbidden**. `prod.sh` also writes
a `SEARXNG_SECRET` (the instance `secret_key`).

**Using an external SearXNG?** Apply the same two settings on it, or
`web_search` will 403:

```yaml
search:
  formats: [html, json]
server:
  method: GET
```

### MCP bridge

The `mcp_bridge` service (suite image, `python -m bp_mcp_bridge`) connects the
MCP servers configured in the **admin UI** (`/admin/mcp-servers`) and onboards
one backplane agent per server (`mcp_<server>`, one mode per tool, exposed to
the LLM as `call_mcp_<server>_<tool>`). It's behind the `mcp` compose profile;
`prod.sh` generates `MCP_BRIDGE_SECRET` and **auto-adds `--profile mcp`**, so the
bridge runs by default. With no MCP servers configured it simply idles.

Auth: the bridge authenticates as a fixed `service_mcp` principal — a
`level=service` user the **router** seeds + re-arms each boot from
`ROUTER_MCP_BRIDGE_SECRET` (the same value the service presents as
`BP_MCP_BRIDGE_SERVICE_SECRET`). It holds a refresh token (rotated + persisted to
its root-owned `/mcp-state` volume), not an admin token, and **cannot mint
invitations**: an admin action (create / **Reconnect**) stashes a short-TTL
invitation on the server's row, which the bridge consumes to onboard. To run it
elsewhere, point `BP_MCP_BRIDGE_ROUTER_URL` / `_ROUTER_ADMIN_URL` at the router
and supply the secret. To NOT run it, leave `MCP_BRIDGE_SECRET` empty.

**Transports.** Each server's `transport` is one of:

- `sse` / `streamable_http` — the bridge connects to a `url` (SSRF-guarded;
  loopback/private allowed, metadata/link-local blocked). Auth via `auth_kind`
  + an `env://` / `secret://` `auth_value_ref`.
- `stdio` — the bridge spawns a local subprocess (`command` + `args`, e.g.
  `uvx some-mcp`) and speaks MCP over its stdin/stdout. Per-server secrets ride
  `env_refs` (`{ENV_NAME: value}`): an `env://VAR` / `secret://…` value is
  resolved from the bridge's env, any other value is an inline literal stored
  as-is (like a preset's inline `api_key` vs `api_key_ref`).

**stdio hardening.** A stdio `command` is third-party code run inside the bridge
container, so it's locked down (mirrors the sandbox agent): the `command` must
be in `BP_MCP_BRIDGE_ALLOWED_LAUNCHERS` (default `uvx`; the router enforces
`ROUTER_MCP_ALLOWED_LAUNCHERS` too); each subprocess is **dropped to a per-server
uid** (`BP_MCP_BRIDGE_UID_BASE.._MAX`) with **no-new-privileges** and rlimits,
and sees **only** a scoped env (`PATH`/`HOME`/`LANG` + its own `env_refs` — never
the bridge's secrets). This needs the container to run as root with
`SETUID`/`SETGID`/`CHOWN` (the prod compose sets this). **Note:** `uvx` fetches
the server package from PyPI on first run, so the `mcp_bridge` container needs
egress to PyPI (the `agents` net is otherwise router-only) — add an egress path
or pre-bake a uv cache if you use stdio servers. SSE/HTTP servers need none of
this.

## Data retention & user erasure

Closed sessions and permanently-deleted users are reaped by background
reconcile loops, not at the click. A **permanent user purge** (admin UI
"Permanently erase user…", or `DELETE /v1/admin/users/{id}?purge=true`)
hard-deletes the router store + scrubs PII synchronously, then the chatbot's
reconcile loop erases the suite store and — by spawning a `purge_user_data`
task on the **memory** agent — the per-user LanceDB (memory + KB share the
volume). So **the memory agent must be running for vector-store erasure to
complete**: while it's down, a purged user's LanceDB stays pending and the
chatbot retries on its next sweep (default daily, `SUITE_SESSION_GC_INTERVAL_S`).
Nothing is half-erased — the suite rows are dropped only after the LanceDB
erase succeeds.

## Sandbox isolation (v1 caveat)

v1 uses the **shared-container / per-uid** model: the sandbox runs bash
in `<sandbox_root>/<user_id>`, dropping to the user's `sandbox_uid` when
configured + running as root. The compose service sets
`no-new-privileges`; for real multi-tenant isolation, run it under a
sandboxed runtime (gVisor / Kata), add resource caps (`cpus`,
`mem_limit`, `pids_limit`), and restrict egress. A Docker-per-user
backend behind the same agent interface is future work
([`deferred-work.md`](./deferred-work.md)).

> **Dev caveat.** `scripts/run-suite.sh` runs every agent — including the
> sandbox — as a **host process**, so its bash executes **uncontained on
> your machine** as the dev user (no container, no uid drop without root,
> no egress limit). `run-suite.sh` defaults `SUITE_SANDBOX_ROOT` to a
> writable `/tmp/bp-suite-sandbox` so it works, and warns on start. Treat
> dev `computer_use` as trusted-only; for untrusted prompts use the
> hardened container above (or a throwaway VM).

## Bring-up order

```
scripts/prod.sh                                              # the prod launcher
```

`scripts/prod.sh` is the single entry point for the prod lifecycle. It runs in
two stages:

**1. Build the env file? (`deploy/.env.prod`)** — answer **y** for a first
deploy or to change vars (it confirms before overwriting an existing file);
**n** reuses the existing file and skips straight to the action. The build
prompts for:

- **LLM provider** (Anthropic / Gemini / OpenAI / Custom) — captures that
  provider's API key into the matching env var (`ANTHROPIC_API_KEY` /
  `GEMINI_API_KEY` / `OPENAI_API_KEY`) and wires the suite's per-tier defaults
  to that provider's seeded aliases — e.g. Anthropic → `lite=claude-haiku`,
  `balanced=claude`, `pro=claude-opus` (Gemini/OpenAI have analogous
  `gemini-lite/gemini/gemini-pro` and `gpt-nano/gpt/gpt-pro` mappings).
  Anthropic has no embedding model, so embeddings stay on `default_embedding`
  (Gemini) — set `GEMINI_API_KEY` too, or repoint
  `SUITE_DEFAULT_PRESET_EMBEDDING` to an OpenAI embedding preset. **Custom**
  asks for no key and wires generic `lite`/`default`/`pro` slots (Gemini
  placeholders) to repoint later via the admin webUI (`/admin`).
- **Web-search backend** — bundled SearXNG (sets
  `SUITE_SEARXNG_URL=http://searxng:8080`), an **external** SearXNG URL, or
  **skip** (empty — research runs without web search).

Everything else (Postgres password, JWT / session / metrics secrets,
object-store keys, one invitation token per agent) is random-generated.

**2. Action** — **start** (`up -d`), **restart** (`up -d --force-recreate`),
**stop** (`down`), or **exit**. start/restart can **rebuild images from this
source** first (asks). The launcher **auto-adds `--profile search`** (the
bundled SearXNG service) to start/restart/stop whenever the env file's
`SUITE_SEARXNG_URL` is the bundled `http://searxng:8080` — read from the file,
so it's correct even when you skip the build step and reuse an earlier env.

Under the hood `compose up` resolves the whole order via `depends_on`:
`postgres` → `router` (healthy) → `init` (`python -m bp_agents.init` — both
schemas, then the invitations + ACL) → the agent groups. `init` replaced the
former `migrate` + `suite-migrate` + `bootstrap` trio; its steps stay
individually runnable for debugging (`docker compose run --rm init python -m
bp_agents.init --step acl`). Migrations stay one-shot (never on agent start),
so restarting a group never races the schema.

The agents run in **groups**, one process each — `suite-core` (nine workers)
and `channels` (chatbot + webapp) — with `sandbox` and the MCP bridge in
their own containers, because their capabilities and network position are
the isolation. See
[`../design/deployment-agent-host.md`](../design/deployment-agent-host.md). To run the steps manually instead (e.g. drive
`register-invitations.sh` + `load_acl` yourself, or add `--profile search` by
hand), see the sections above.

Then message the Telegram bot and send `/register` (an admin approves the
registration; the chatbot's approval poller maps the chat to the new user).
