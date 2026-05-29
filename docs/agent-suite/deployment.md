# Agent Suite — Deployment

> How to run the v1 suite (Telegram chatbot + webapp browser channel +
> orchestrator + specialists)
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
  is on `agents` only.

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
- chatbot: `SUITE_TELEGRAM_BOT_TOKEN`; `SUITE_REDIS_URL` makes the
  per-session turn lock distributed — **required when both the chatbot and
  the webapp run**, so the two channels serialize turns for a shared
  session; a chatbot-only deploy needs no Redis.
- webapp: `WEBAPP_SESSION_SECRET` (signs the browser session cookie;
  required). Serves FastAPI on `:8002`, fronted by Caddy on its own host
  (`WEBAPP_DOMAIN`, default `app.<PUBLIC_DOMAIN>`) — it serves from root, so
  it can't share the router's domain where `/admin` lives. HTTP ops use the
  logged-in user's own token (no service principal). Optional
  `WEBAPP_USE_BUILT_CSS=true` swaps the Tailwind CDN for a pre-built
  stylesheet (see `bp_agents/agents/webapp/tailwind.config.js`).
- research: `SUITE_SEARXNG_URL` (the bundled SearXNG or an external
  Brave-API-compatible endpoint).
- LLM presets are router-side (`llm_presets` table); the suite only names
  presets (`SUITE_DEFAULT_PRESET_*` / per-user `user_config`).

## ACL

Apply the suite firewall rule set once after first boot (admin
credentials in env):

```
python -m bp_agents.load_acl        # PUT /v1/admin/acl/rules
```

This replaces the router's ACL with `bp_agents.acl.suite_acl_rules()`.

## Web search (SearXNG)

The `searxng` service is behind the `search` compose profile — enable it
with `docker compose --profile search up`, or leave it off and set
`SUITE_SEARXNG_URL` to an external instance. With neither, `web_search`
returns a "not configured" notice; the rest of research still works.

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
scripts/init-prod-env.sh                                     # generates deploy/.env.prod
docker compose -f docker-compose.prod.yml --env-file deploy/.env.prod up -d
```

`init-prod-env.sh` first asks which **LLM provider** to use (Anthropic /
Gemini / OpenAI / Custom), captures that provider's API key into the matching
env var (`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY`), and wires
the suite's per-tier defaults to that provider's seeded aliases — e.g.
Anthropic → `lite=claude-haiku`, `balanced=claude`, `pro=claude-opus` (Gemini
and OpenAI have analogous `gemini-lite/gemini/gemini-pro` and
`gpt-nano/gpt/gpt-pro` mappings). Anthropic has no embedding model, so
embeddings stay on `default_embedding` (Gemini) — set `GEMINI_API_KEY` too, or
repoint `SUITE_DEFAULT_PRESET_EMBEDDING` to an OpenAI embedding preset.

The **Custom** option asks for no key and wires the generic tier slots
(`lite` / `default` / `pro` — preset names seeded to Gemini placeholders in the
catalogue); set the provider key(s) and repoint those presets to any
provider/model via the admin webUI (`/admin`) afterward.

`compose up` resolves the whole order via `depends_on`: `postgres` →
`migrate` + `suite-migrate` (schemas) → `router` (healthy) → `bootstrap`
(`python -m bp_agents.bootstrap` — registers the pre-supplied invitation
tokens + applies the ACL) → the agents. The migrations stay one-shot init
services (never on agent start), so scaling an agent never races the schema.
Add `--profile search` to bring up the bundled SearXNG. To run the steps
manually instead (e.g. `register-invitations.sh` + `load_acl`), see the
sections above.

Then message the Telegram bot and send `/register` (an admin approves the
registration; the chatbot's approval poller maps the chat to the new user).
