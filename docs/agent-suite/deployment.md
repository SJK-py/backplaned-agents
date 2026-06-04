# Agent Suite — Deployment

> How to run the v1 suite (Telegram + KakaoTalk chatbot + webapp browser
> channel + orchestrator + specialists)
> on top of a Backplaned router. The router's own deployment (Postgres,
> Redis, file store, edge proxy, secrets) is in
> [`../deployment.md`](../deployment.md); this covers the **suite**
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
  and reaches the in-cluster Redis over `suite` (so enabling Kakao needs no
  new network).

## Databases

The router uses `bp_router`; the suite uses its own `bp_suite`
(`deploy/postgres-init/01-create-suite-db.sql` creates it). Apply both
schemas once, as one-shot jobs (never on container start):

```
alembic upgrade head                          # router  (migrate service)
alembic -c alembic_suite.ini upgrade head     # suite   (suite-migrate service)
```

## Invitations (one per agent)

Each agent onboards with its own admin-issued invitation. There's **no
mint → paste round-trip**: `POST /v1/admin/invitations` accepts a
caller-supplied `token`, so you set one token per agent in the env file and
register those same values. `scripts/register-invitations.sh` does both —
`--gen` prints one `<AGENT>_INVITATION=<token>` line per agent (append to
`deploy/.env.prod`), and a normal run logs in as admin and registers each
(the **chatbot's** with `provisions_service_user: true` automatically — it
bootstraps the `usr_service_chatbot` principal used for registration +
per-user minting). It's idempotent (re-runs are safe). The same env vars
feed the agent containers:

| env var | agent | notes |
| --- | --- | --- |
| `CHATBOT_INVITATION` | chatbot | `provisions_service_user=true` |
| `WEBAPP_INVITATION` | webapp | browser channel (no service principal) |
| `ORCHESTRATOR_INVITATION` | orchestrator | |
| `DEEP_REASONING_INVITATION` · `RESEARCH_INVITATION` · `COMPUTER_USE_INVITATION` | l1 | |
| `KNOWLEDGE_BASE_INVITATION` · `MEMORY_INVITATION` | l3 stores | share `lancedb_data` |
| `HISTORY_SUMMARIZER_INVITATION` · `MD_CONVERTER_INVITATION` · `CONFIG_INVITATION` | l3/l4/l2 | |
| `SANDBOX_INVITATION` | sandbox | hardened container |

The dev launcher `scripts/run-suite.sh` mints + starts the whole roster
automatically for a local router.

## Per-agent configuration

- `AGENT_ROUTER_URL` — `ws://router:8000/v1/agent`
- `AGENT_STATE_DIR` — persists `credentials.json` (+ chatbot's Telegram
  offset); give the chatbot a volume.
- `SUITE_DATABASE_URL` — `postgresql://…@postgres:5432/bp_suite`
- `SUITE_LANCE_ROOT` — per-user LanceDB root (`/lancedb`; shared volume
  for `knowledge_base` + `memory`).
- chatbot / webapp Redis: `SUITE_REDIS_URL` makes the per-session turn lock
  **distributed** so the two channels serialize turns on a shared session
  (the lock key is `session_id`-only, so a Telegram turn and a webapp turn
  for the same session contend on the same key). The reference
  `docker-compose.prod.yml` **defaults it on** (in-cluster `redis` on db 1;
  db 0 is the router's) — because v1 runs both channels — and the lock
  **fails open** if Redis is unreachable. Override only to point at a
  different Redis; a single-channel deploy can unset it for an in-process
  lock.
- chatbot: `SUITE_TELEGRAM_BOT_TOKEN` (Telegram).
- chatbot (KakaoTalk, optional): an egress-only second channel. The agent
  **pulls** turns from a Cloudflare Queue fed by the
  [`deploy/kakao-relay`](../../deploy/kakao-relay/) Worker — it opens no
  inbound port. Gate it with `SUITE_KAKAO_CF_ACCOUNT_ID` /
  `SUITE_KAKAO_CF_QUEUE_ID` / `SUITE_KAKAO_CF_API_TOKEN` (a token scoped to
  Queues pull+ack); it uses the same `SUITE_REDIS_URL` above for its
  deadline / next-touch registry (so keep Redis on when Kakao is enabled).
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
- LLM presets are router-side (`llm_presets` table); the suite only names
  presets (`SUITE_DEFAULT_PRESET_*` / per-user `user_config`).

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
its `/state` volume), not an admin token, and **cannot mint invitations**: an
admin action (create / **Reconnect**) stashes a short-TTL invitation on the
server's row, which the bridge consumes to onboard. To run it elsewhere, point
`BP_MCP_BRIDGE_ROUTER_URL` / `_ROUTER_ADMIN_URL` at the router and supply the
secret. To NOT run it, leave `MCP_BRIDGE_SECRET` empty.

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
`postgres` → `migrate` + `suite-migrate` (schemas) → `router` (healthy) →
`bootstrap` (`python -m bp_agents.bootstrap` — registers the pre-supplied
invitation tokens + applies the ACL) → the agents. The migrations stay
one-shot init services (never on agent start), so scaling an agent never
races the schema. To run the steps manually instead (e.g. drive
`register-invitations.sh` + `load_acl` yourself, or add `--profile search` by
hand), see the sections above.

Then message the Telegram bot and send `/register` (an admin approves the
registration; the chatbot's approval poller maps the chat to the new user).
