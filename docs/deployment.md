# Deployment

> How a full Backplaned + agent-suite stack is packaged and wired. The
> **platform** (router + the data services it needs) lives in this repo;
> the **agent suite** (chatbot, sandbox, orchestrator, …) lives in its
> own repo. This doc is the cross-cutting topology and the **wire-up
> contract** between the two. Concrete artifacts in this repo:
> [`Dockerfile`](../Dockerfile), [`docker-compose.prod.yml`](../docker-compose.prod.yml),
> [`deploy/Caddyfile`](../deploy/Caddyfile),
> [`deploy/postgres-init/`](../deploy/postgres-init/).

## 1. Topology

```
                         ┌─────────────┐
              443/wss ──▶│  caddy      │  TLS + WSS upgrade + admin UI / webapp
                         └──────┬──────┘
                       edge net │
                         ┌──────▼──────┐      backend net (private)
                         │   router    │──────┬─────────┬──────────┐
                         │ (this repo) │      │         │          │
                         └──────┬──────┘  ┌───▼───┐ ┌───▼───┐ ┌────▼────┐
                       agents   │         │  PG   │ │ Redis │ │ rustfs  │
                       net      │         │ (×2db)│ │       │ │  (S3)   │
            ┌────────────┬──────┴────┬────────────┐└───────┘ └─────────┘
        ┌───▼───┐   ┌────▼────┐  ┌───▼────┐   ┌────▼────┐         ▲ suite net
        │chatbot│   │sandbox  │  │reasoning│  │ stores  │ helpers │ (PG#2/Redis)
        │(gway) │   │(untrust)│  │ l0+l1   │  │ kb+mem  │  …      │
        └───────┘   └─────────┘  └─────────┘  └─────────┘─────────┘
```

Two boundaries to keep straight:

- **Two Postgres databases, one server** — `bp_router` (router: users/tasks/agents/files/acl/audit) and `bp_suite` (suite: session_info/session_history/user_config/cron/suite_platform_mappings). Different owners, different credentials. ([`agent-suite/data-model.md`](./agent-suite/data-model.md))
- **The router is the only holder of provider/S3/JWT secrets.** Agents call `ctx.llm` and the router's file endpoints; they never reach providers, S3, or the router DB directly.

## 2. Boxes

### Edge — `caddy`
TLS termination, the WebSocket upgrade on `/v1/agent`, and routing the admin UI (`/admin/*`) + webapp (v2). The router does **not** terminate TLS. PG/Redis/rustfs are never proxied.

### docker 1 — router (this repo)
FastAPI + WS + admin UI (`bp-router` → uvicorn `create_app` factory). Config (env `ROUTER_*`):

| Env | Purpose |
| --- | --- |
| `ROUTER_DB_URL` | → `bp_router` DB |
| `ROUTER_REDIS_URL` | jti revocation, rate limits (required in prod) |
| `ROUTER_JWT_SECRET` | ≥32 bytes (`openssl rand -base64 32`) |
| `ROUTER_FILE_STORE=s3` + `ROUTER_FILE_STORE_OPTIONS` | JSON: `bucket`, `endpoint_url`, `region_name`, `access_key_id`, `secret_access_key` → rustfs |
| `ROUTER_PUBLIC_URL` | external URL (behind the proxy) |
| `ROUTER_ADMIN_SESSION_SECRET` | admin-UI cookie signing |
| `ROUTER_METRICS_TOKEN` | bearer-gates `/metrics` (required in prod) |
| `ROUTER_BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | seed the first admin (idempotent) |
| provider key secrets (e.g. `GEMINI_API_KEY`) | resolved by preset `api_key_ref` |

LLM **presets** are rows in the router's `llm_presets` table, admin-managed via `/admin`; a preset's `api_key_ref` (e.g. `env://GEMINI_API_KEY`) resolves to the env secrets above. The table is auto-seeded on first boot from a commentable JSONC catalogue (`bp_router/llm/presets_catalog.jsonc`, or `ROUTER_LLM_PRESET_CATALOG_PATH`) — edit that to set a deployment's initial model list. Build an image from this repo (no Dockerfile existed before — [`Dockerfile`](../Dockerfile) adds one).

### docker 2 — Postgres 16
Hosts both `bp_router` and `bp_suite`. The router DB is migrated by this repo's alembic; the suite DB by the suite repo's migrations. Persistent volume + backups. Reachable by the router (router DB) and the suite session-manager/agents (suite DB) — **never by the sandbox**. The second DB is created on first init by [`deploy/postgres-init/`](../deploy/postgres-init/).

### docker 3 — Redis 7
Shared: router (revocation/rate-limits) + suite (the per-session FIFO queue when the channel is multi-worker; cron coordination). One instance.

### docker 4 — rustfs (S3)
The router's blob backend (`file_store=s3`, `endpoint_url=http://rustfs:9000`). Persistent volume, **internal-only**. Downloads default to **router-proxy** mode, which keeps rustfs off the public net; only switch to **302-presigned** (rustfs reachable by clients) if you need to offload bytes from the router. Only the router holds the rustfs keys. *(Pin the rustfs tag + confirm its env against upstream; MinIO is a drop-in.)*

### docker 5 — chatbot (channel; webapp in v2)
The gateway ([`agent-suite/channel.md`](./agent-suite/channel.md)). Bootstraps **two identities at onboarding** from a single invitation flagged `provisions_service_user`: its agent JWT *and* the `usr_service_{agent_id}` service refresh token ([`security.md` §3.2](./security.md)). Holds the Telegram bot token + suite-DB creds. Stateful (`state_dir` volume: credentials.json + Telegram offset); runs the cron scheduler (v1). Needs router WS + router HTTP (token mint, `/v1/files/names`) + suite DB + Redis + Telegram egress.

### docker 6 — sandbox (untrusted code)
The one box running untrusted user code — isolate hard: a sandboxed runtime (**gVisor/Kata**), `cap_drop: ALL`, `no-new-privileges`, seccomp, read-only rootfs + a writable per-user workspace, CPU/mem/pids limits, **no DB/Redis/S3/provider access**, egress off or tightly allowlisted. It reaches **only the router WS**. Files move via the router (`storage_to_workspace`/`workspace_to_storage`), never direct S3. Prefer **per-user/per-task ephemeral sub-containers** over one shared container (a single container shares a kernel across users).

### docker 7 + 8 — the LLM agents
One **process per agent** (own `agent_id` + invitation + `state_dir`); pack processes into containers with a supervisor. Recommended grouping (refines the proposed 7/8 split):

- **reasoning** — `orchestrator` (l0) + `computer_use`, `research`, `deep_reasoning` (l1). Split `orchestrator` out if you want to scale the hot path alone.
- **stores** — `knowledge_base` + `memory`, **co-located** with the per-user **LanceDB volume**. They back onto the same per-user LanceDB (non-transactional; memory holds a per-user lock), so they must not be split across containers. The lock is in-process here → move it to Redis if this box scales >1.
- **helpers** — `config` (suite DB) + `history_summarizer` (stateless) + `md_converter` (non-LLM).

All need only: router WS + their invitation + (for history/config) suite DB. **No provider keys.**

### docker 9 (optional) — searxng
Web-search backend for `research`. Internal-only; the one agent-side box with web egress. `research` queries it.

## 3. Secret scoping (least privilege)

| Box | Secrets it holds |
| --- | --- |
| router | `JWT_SECRET`, S3 keys, provider keys, router-DB creds, admin-session secret, metrics token |
| chatbot | Telegram token, suite-DB creds, its agent invitation |
| every other agent | **its agent invitation only** |
| sandbox | its invitation only — nothing else |

## 4. Networks

- `edge` — proxy ↔ router (+ webapp in v2).
- `backend` — router ↔ PG / Redis / rustfs. **Private**; agents never join it.
- `agents` — router ↔ every agent (the agent WS).
- `suite` — chatbot + suite agents ↔ `bp_suite` Postgres / Redis.

The sandbox joins `agents` **only** (router WS), nothing else.

## 5. First-boot order

1. `docker compose -f docker-compose.prod.yml up -d postgres redis rustfs` — data services.
2. `migrate` one-shot runs `alembic upgrade head` against `bp_router`.
3. `router` starts; the bootstrap-admin env seeds the first admin.
4. Admin logs into `/admin`, configures LLM presets, and **issues agent invitations** — the chatbot's flagged `provisions_service_user`.
5. The suite repo runs its **own** migrations against `bp_suite`, then its agents start: each onboards with its invitation (persisting creds to its `state_dir`) and connects.

## 6. Build & run (this repo)

```bash
docker build -t backplaned-router:latest .
cp deploy/.env.prod.example deploy/.env.prod   # fill PG_PASSWORD, ROUTER_JWT_SECRET, S3 keys, …
docker compose -f docker-compose.prod.yml --env-file deploy/.env.prod up -d
```

The agent-suite services are commented in the compose as the contract; bring them in from the suite repo (they share one image, agent selected by entrypoint env).

## 7. Scaling notes

- **router** — stateless; scale horizontally behind the proxy. Only the one-shot `migrate` runs `upgrade`.
- **chatbot** — stateful (session queue, Telegram offset). v1: single instance. To scale: Redis-backed session queue + session→worker affinity ([`agent-suite/overview.md` §2.2](./agent-suite/overview.md)).
- **stores** — co-located KB+memory; >1 replica requires the memory per-user lock in Redis.
- **sandbox** — scale by workspace/runtime capacity; keep the isolation invariants regardless of replica count.
