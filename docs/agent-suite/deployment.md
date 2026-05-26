# Agent Suite — Deployment

> How to run the v1 suite (Telegram chatbot + orchestrator + specialists)
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
  touch the suite DB are also on `suite`; the chatbot is additionally on
  `edge` (Telegram egress); the **sandbox** is on `agents` only.

## Databases

The router uses `bp_router`; the suite uses its own `bp_suite`
(`deploy/postgres-init/01-create-suite-db.sql` creates it). Apply both
schemas once, as one-shot jobs (never on container start):

```
alembic upgrade head                          # router  (migrate service)
alembic -c alembic_suite.ini upgrade head     # suite   (suite-migrate service)
```

## Invitations (one per agent)

Each agent onboards with its own admin-issued invitation. Mint them after
the router is up (admin `POST /v1/admin/invitations`); the **chatbot's**
must be flagged `provisions_service_user: true` (it bootstraps the
`usr_service_chatbot` principal used for registration + per-user minting).
Supply each via the matching compose env var:

| env var | agent | notes |
| --- | --- | --- |
| `CHATBOT_INVITATION` | chatbot | `provisions_service_user=true` |
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
- chatbot: `SUITE_TELEGRAM_BOT_TOKEN`.
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

## Bring-up order

```
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d postgres redis rustfs
docker compose -f docker-compose.prod.yml up migrate suite-migrate   # one-shots
docker compose -f docker-compose.prod.yml up -d router caddy
# mint invitations (admin), put them in the env, then:
docker compose -f docker-compose.prod.yml --profile search up -d
python -m bp_agents.load_acl
```

Then message the Telegram bot and send `/register` (an admin approves the
registration; the chatbot's approval poller maps the chat to the new user).
