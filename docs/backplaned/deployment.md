# Deployment

> How a full Backplaned + agent-suite stack is packaged and wired. The
> **platform** (router + the data services it needs) lives in this repo;
> the **agent suite** (chatbot, sandbox, orchestrator, …) lives in its
> own repo. This doc is the cross-cutting topology and the **wire-up
> contract** between the two. Concrete artifacts in this repo:
> [`Dockerfile`](../../Dockerfile), [`docker-compose.prod.yml`](../../docker-compose.prod.yml),
> [`deploy/Caddyfile`](../../deploy/Caddyfile),
> [`deploy/postgres-init/`](../../deploy/postgres-init).

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
                       agents   │         │  PG   │ │ Redis │ │seaweedfs│
                       net      │         │ (×2db)│ │       │ │  (S3)   │
            ┌────────────┬──────┴────┬────────────┐└───────┘ └─────────┘
        ┌───▼───┐   ┌────▼────┐  ┌───▼────┐   ┌────▼────┐         ▲ suite net
        │chatbot│   │sandbox  │  │reasoning│  │ stores  │ helpers │ (PG#2/Redis)
        │(gway) │   │(untrust)│  │ l0+l1   │  │ kb+mem  │  …      │
        └───────┘   └─────────┘  └─────────┘  └─────────┘─────────┘
```

Two boundaries to keep straight:

- **Two Postgres databases, one server** — `bp_router` (router: users/tasks/agents/files/acl/audit) and `bp_suite` (suite: session_info/session_history/user_config/cron/suite_platform_mappings). Different owners, different credentials. ([`agent-suite/data-model.md`](../agent-suite/data-model.md))
- **The router is the only holder of provider/S3/JWT secrets.** Agents call `ctx.llm` and the router's file endpoints; they never reach providers, S3, or the router DB directly.

## 2. Boxes

### Edge — `caddy`
TLS termination, the WebSocket upgrade on `/v1/agent`, and routing the admin UI (`/admin/*`) + webapp. The router does **not** terminate TLS. PG/Redis/SeaweedFS are never proxied. Two public hostnames, both served by the one Caddy container (see [`deploy/Caddyfile`](../../deploy/Caddyfile)) — **full setup in [§9 Edge / reverse proxy](#9-edge--reverse-proxy-caddy)**:

| Env var | Serves | Default |
| --- | --- | --- |
| `EDGE_MODE` | how the stack is reached: `domain` \| `ip` \| `both` — `prod.sh` **generates the Caddyfile** for it (see [§9](#9-edge--reverse-proxy-caddy)) | `domain` |
| `PUBLIC_DOMAIN` | (domain/both) router HTTP API (`/v1/*`), health, metrics, OpenAPI, **admin UI** (`/admin/*`); bare `/` → `/admin/login` | `localhost` |
| `WEBAPP_DOMAIN` | (domain/both) the **browser channel** (webapp) — login at `/`, `/chat/*`, `/files/*` | `app.${PUBLIC_DOMAIN}` |
| `EDGE_SCHEME` | (domain/both) scheme Caddy serves; set `http` when **TLS is terminated upstream** (Cloudflare Tunnel / LB) — see [§9](#9-edge--reverse-proxy-caddy) | `https` |
| `PUBLIC_IP` | (ip/both) bare-IP identity — router + admin on `https://<ip>`, **always** Caddy internal-CA TLS | — |
| `WEBAPP_HTTPS_PORT` | (ip/both) the webapp's **own** published https port on the IP (no DNS for `app.<ip>`) | `8443` |
| `ROUTER_PUBLIC_URL` | canonical public origin (always https); the domain when present, else the IP | `https://${PUBLIC_DOMAIN}` |
| `CADDYFILE_HOST_PATH` | bind-mount source for Caddy's config — `prod.sh` sets it to the generated file | `./deploy/Caddyfile` |

The webapp gets its **own** identity (hostname, or a port for a bare IP) because it serves from `/`, which would collide with the router's `/admin` on the same host.

### docker 1 — router (this repo)
FastAPI + WS + admin UI (`bp-router` → uvicorn `create_app` factory). Config (env `ROUTER_*`):

| Env | Purpose |
| --- | --- |
| `ROUTER_DB_URL` | → `bp_router` DB |
| `ROUTER_REDIS_URL` | jti revocation, rate limits (required in prod) |
| `ROUTER_JWT_SECRET` | ≥32 bytes (`openssl rand -base64 32`) |
| `ROUTER_FILE_STORE=s3` + `ROUTER_FILE_STORE_OPTIONS` | JSON: `bucket`, `endpoint_url`, `region_name`, `access_key_id`, `secret_access_key` → SeaweedFS |
| `ROUTER_PUBLIC_URL` | external URL (behind the proxy) |
| `ROUTER_ADMIN_SESSION_SECRET` | admin-UI cookie signing |
| `ROUTER_METRICS_TOKEN` | bearer-gates `/metrics` (required in prod) |
| `ROUTER_BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | seed the first admin (idempotent) |
| provider key secrets (e.g. `GEMINI_API_KEY`) | resolved by preset `api_key_ref` |

LLM **presets** are rows in the router's `llm_presets` table, admin-managed via `/admin`; a preset's `api_key_ref` (e.g. `env://GEMINI_API_KEY`) resolves to the env secrets above. The table is auto-seeded on first boot from a commentable JSONC catalogue (`bp_router/llm/presets_catalog.jsonc`, or `ROUTER_LLM_PRESET_CATALOG_PATH` to replace it wholesale). To **add or override** a few presets without copying the whole built-in list, drop a `deploy/presets.custom.jsonc` (copy the `.example`; gitignored, mounted into the router): it's **merged over** the bundled catalogue at seed time and a custom entry **wins on a name collision** (so e.g. repointing `default` to a local Ollama model is one entry). All of this is **seed-time only** — once the table is populated, edit presets in `/admin`; the files don't retroactively change a seeded deployment. Build an image from this repo ([`Dockerfile`](../../Dockerfile)).

**Preset credentials — `api_key_ref` vs `api_key`.** A preset authenticates one of two ways, in the catalog/overlay or the admin form:

- **`api_key_ref`** (preferred) — an indirect reference resolved at call time. The only built-in scheme is `env://VAR`, which reads the *router container's* environment. Vars that already reach the container (just set the value in `deploy/.env.prod`): `env://GEMINI_API_KEY`, `env://OPENAI_API_KEY`, `env://ANTHROPIC_API_KEY` (filled by `scripts/prod.sh`), plus three generic slots for custom/self-hosted providers — `env://CUSTOM_LLM_API_KEY_1`, `_2`, `_3`. Referencing **some other** var name takes two edits — add it to `deploy/.env.prod` **and** to the `router` service's `environment:` block in `docker-compose.prod.yml`, because compose's `--env-file` only feeds `${...}` interpolation, not the container — so prefer a `CUSTOM_LLM_API_KEY_*` slot to skip the compose edit. A missing ref resolves empty (calls 401). Only the *reference* lives in the DB/file.
- **`api_key`** (inline) — a literal secret on the preset; wins over `api_key_ref` when both are set. No env/compose edits, so it's the path of least resistance for a provider whose key isn't one of the three above, or a keyless local endpoint (pass any dummy string). **Caveat:** it's stored **plaintext** in `llm_presets.api_key` (the admin API masks it to `has_api_key: true` on read-back, but it is *not* encrypted at rest), and a key placed in `deploy/presets.custom.jsonc` also sits plaintext in that file. Prefer `api_key_ref` for shared/production secrets; reserve inline `api_key` for non-standard providers or throwaway/local keys, and lock the overlay file down (`chmod 600`).


### docker 2 — Postgres 16
Hosts both `bp_router` and `bp_suite`. The router DB is migrated by this repo's alembic; the suite DB by the suite repo's migrations. Persistent volume + backups. Reachable by the router (router DB) and the suite session-manager/agents (suite DB) — **never by the sandbox**. The second DB is created on first init by [`deploy/postgres-init/`](../../deploy/postgres-init).

### docker 3 — Redis 7
Shared: router (revocation/rate-limits) + suite (the per-session FIFO queue when the channel is multi-worker; cron coordination). One instance.

### docker 4 — SeaweedFS (S3)
The router's blob backend (`file_store=s3`, `endpoint_url=http://seaweedfs:8333`). Persistent volume, **internal-only**. Downloads default to **router-proxy** mode (`ROUTER_FILE_DOWNLOAD_PRESIGNED=false`), which streams bytes through the router and keeps SeaweedFS off the public net. This is required for the bundled topology: every download consumer (agents, chatbot, webapp backend) is in-cluster and can't resolve the store host, so a presigned redirect to `seaweedfs:8333` would fail. Only switch to **302-presigned** (`ROUTER_FILE_DOWNLOAD_PRESIGNED=true`) if you front the store on a hostname download clients can actually reach and want to offload bytes from the router. Only the router holds the S3 keys. *(Replaced rustfs beta, whose multipart path was broken — `create_multipart_upload` succeeded but the immediate `upload_part` returned NoSuchUpload, failing any upload bigger than one PUT. SeaweedFS has reliable S3 multipart. Pin the tag + confirm flags against upstream; MinIO is a drop-in.)*

### docker 5 — chatbot (channel; webapp in v2)
The gateway ([`agent-suite/channel.md`](../agent-suite/channel.md)). Bootstraps **two identities at onboarding** from a single invitation flagged `provisions_service_user`: its agent JWT *and* the `usr_service_{agent_id}` service refresh token ([`security.md` §3.2](./security.md)). Holds the Telegram bot token + suite-DB creds. Stateful (`state_dir` volume: credentials.json + Telegram offset); runs the cron scheduler (v1). Needs router WS + router HTTP (token mint, `/v1/files/names`) + suite DB + Redis + Telegram egress.

### docker 6 — sandbox (untrusted code)
The one box running untrusted user code — isolate hard: a sandboxed runtime (**gVisor/Kata**), `no-new-privileges`, seccomp, read-only rootfs + a writable per-user workspace, CPU/mem/pids limits, **no DB/Redis/S3/provider access**, egress off or tightly allowlisted. It reaches **only the router WS**. Files move via the router (`stash_to_workspace`/`workspace_to_stash`), never direct S3. Prefer **per-user/per-task ephemeral sub-containers** over one shared container (a single container shares a kernel across users).

**Per-user uid isolation (and why this box runs as root).** Inside the shared container the sandbox drops each user's `bash` to a distinct OS uid (`setgroups([])`/`setgid`/`setuid` in the child pre-exec; the `user_id → uid` map is owned locally in a JSON file on the sandbox's `/state` volume — it has no DB). The drop needs `CAP_SETUID`/`CAP_SETGID`, which only root holds, so the prod compose runs **this one service as `user: "0:0"`** (the agent process does nothing privileged — it immediately drops per command). Two implications for hardening:

- **Keep `no-new-privileges:true`.** It blocks the untrusted code from *regaining* privilege via setuid binaries (`sudo`/`su`/`ping`) while still permitting our root pre-exec to *drop* — the kernel's `no_new_privs` governs `execve` privilege-*gain*, not `setuid()` down. Root only to drop away; dropped code can't climb back.
- **Do NOT `cap_drop: ALL`** here — that strips `CAP_SETUID`/`CAP_SETGID` and the drop silently `EPERM`s, collapsing every user onto one uid. Drop everything *except* `SETUID`/`SETGID` (`cap_drop: ALL` + `cap_add: [SETUID, SETGID]`).
- Without root (e.g. you pin a non-root user), the pre-exec skips the drop and **all users share one uid — no per-user isolation**. The uid range is `sandbox_uid_base`..`sandbox_uid_max` (default `100000`–`165535`); under Docker **userns-remap**, keep that range inside the remapped sub-uid window.

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
- `backend` — router ↔ PG / Redis / SeaweedFS. **Private**; agents never join it.
- `agents` — router ↔ every agent (the agent WS).
- `suite` — chatbot + suite agents ↔ `bp_suite` Postgres / Redis.

The sandbox joins `agents` **only** (router WS), nothing else.

## 5. First-boot order

1. `docker compose -f docker-compose.prod.yml up -d postgres redis seaweedfs` — data services.
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

## 7. Graceful shutdown (rollouts)

On `SIGTERM` (every restart/rollout) the **router** lifespan drains in order — close live WS sockets with code 1001, cancel in-flight router-side LLM tasks (stop burning provider tokens), drain background loops, stop the PendingAcks reaper, close the pool/Redis — and the **suite agents** drain in-flight tasks via the SDK's graceful shutdown. The compose gives router + agents `stop_grace_period: 30s` so Docker doesn't `SIGKILL` mid-drain (its default is only 10s). The router caps its own wait at `ROUTER_SHUTDOWN_GRACE_S` (default 25s, the uvicorn `timeout_graceful_shutdown`) — keep it **below** `stop_grace_period`. Under `gunicorn -k uvicorn.workers.UvicornWorker`, set the worker's `--graceful-timeout` to match.

## 8. Scaling notes

See **[`scaling.md`](./scaling.md)** for the full picture: the current
scaling posture (the router runs as a **single worker** today; which
subsystems already have a Redis path), the per-service scaling rules, and
the ranked backlog of work to lift each ceiling. In short:

- **router** — **single worker** today (process-local socket registry /
  correlation maps); scale **vertically** until the multi-worker work
  ([`scaling.md §1.1`](./scaling.md), [`router/storage.md §6.1`](./router/storage.md#61-multi-worker--planned))
  lands. The one-shot `migrate` is the only process that runs `upgrade`.
- **chatbot** — stateful (session queue, Telegram offset). v1: single
  instance. To scale: `SUITE_REDIS_URL` (distributed session lock) +
  session→worker affinity.
- **stores** — co-located KB+memory; >1 replica requires the memory
  per-user lock in Redis (`SUITE_REDIS_URL`).
- **sandbox** — scale by workspace/runtime capacity; keep the isolation
  invariants regardless of replica count.

## 9. Edge / reverse proxy (Caddy)

The `caddy` service (image `caddy:2`) is the only thing on `:80`/`:443` (plus
the optional webapp port below). It terminates TLS, proxies the public
hostnames, and transparently upgrades the agent WebSocket on `/v1/agent`. The
router never terminates TLS; Postgres / Redis / SeaweedFS are never proxied.

**The Caddyfile is generated.** `scripts/prod.sh` asks how the stack is reached
(**`EDGE_MODE`**) and runs [`scripts/render-caddyfile.sh`](../../scripts/render-caddyfile.sh)
to write `deploy/Caddyfile.generated`, pointing `CADDYFILE_HOST_PATH` (the
`caddy` bind mount) at it. The committed
[`deploy/Caddyfile`](../../deploy/Caddyfile) is the **localhost default** used
only for a bare `docker compose up` without `prod.sh`. Generating (rather than
env-templating one static file) is what lets a single deploy serve **two
identities at once** — a domain *and* a bare IP — each with the right TLS.

`EDGE_MODE`:

- **`domain`** (default) — a hostname identity:
  - **`PUBLIC_DOMAIN`** — router + admin UI (`/v1/*`, `/admin/*`, `/healthz`,
    `/readyz`, `/metrics`, `/docs`; bare `/` redirects to `/admin/login`).
  - **`WEBAPP_DOMAIN`** — the browser channel (defaults to `app.${PUBLIC_DOMAIN}`).
    Its **own identity** because it serves from `/`, which would collide with the
    router's `/admin` — Caddy routes the two by `Host` header on the shared `:443`.
  - **`EDGE_SCHEME`** — `https` (Caddy terminates TLS) or `http` (TLS upstream).
- **`ip`** — a bare-IP identity (LAN, no DNS): router + admin on `https://<ip>`,
  webapp on its own port `https://<ip>:${WEBAPP_HTTPS_PORT}` (default `8443`).
  **Always https** via Caddy's internal CA (see the bare-IP row below).
- **`both`** — serve the domain **and** the bare IP simultaneously (e.g. a public
  domain via a Cloudflare Tunnel *and* direct LAN access by IP). All of the above
  vars apply; `ROUTER_PUBLIC_URL` (the canonical public origin) is the **domain**.

`ROUTER_PUBLIC_URL` is always `https://…` (the domain when present, else the IP)
even when `EDGE_SCHEME=http`, since the *public* scheme stays HTTPS.

> **Two identities, one `:443`.** Caddy virtual-hosts every site on `:443` and
> routes by SNI / `Host` header — so each needs a *distinct* name. With a
> **hostname** that's free (`bp.example.com` + `app.bp.example.com`). With a
> **bare IP** there's no `app.<ip>` to resolve, so the webapp takes a **port
> identity** on the same IP (`https://<ip>:8443`) and the `caddy` service
> publishes `WEBAPP_HTTPS_PORT` (default `8443`). In **`both`** mode the domain
> router and the IP router *share* `:443` — Caddy serves the Let's Encrypt /
> domain cert by SNI and the IP's internal-CA cert when the client sends no SNI
> (the global `default_sni <ip>`). The webapp can't be path-mounted under the
> router (no mount-prefix support), so a second name or a second port is the
> only option.

### Choosing the hostnames — access modes

Caddy decides how to provision TLS **from the site address itself**:

| You want | `EDGE_MODE` + identity | TLS Caddy uses | Reachable from |
| --- | --- | --- | --- |
| **Local only** (single box, a trial) | `domain`; `localhost` / `app.localhost` (the defaults) | local auto-self-signed (trusted by the host's browsers via Caddy's local CA) | this machine only — `https://localhost`, `https://app.localhost` |
| **LAN — by hostname** (preferred) | `domain`; a LAN-resolvable name **and** its `app.` sub, e.g. `bp.lan` / `app.bp.lan` | Caddy's **internal CA** (not public names → no ACME) | any LAN device — **browsers warn until you install Caddy's root CA** (below) |
| **LAN — by bare IP** | `ip`; `PUBLIC_IP=192.168.1.50`, `WEBAPP_HTTPS_PORT=8443` | Caddy's **internal CA** | any LAN device — router at `https://<ip>`, **webapp at `https://<ip>:8443`** (open that port on any firewall) |
| **Public** (internet) | `domain`; a real domain whose DNS points here, e.g. `bp.example.com` / `app.example.com` | **automatic Let's Encrypt** (ACME HTTP/TLS challenge) | anywhere — publicly-trusted cert, no warnings |
| **Public domain + LAN IP** | `both`; the domain pair **and** `PUBLIC_IP` (often with `EDGE_SCHEME=http` for a tunnel) | per-identity (domain: ACME/upstream; IP: internal CA) | both at once — see [Serving HTTP at the origin](#serving-http-at-the-origin-tls-terminated-upstream) |

Notes:

- **`localhost` is loopback only.** It is *not* reachable from other devices;
  use a LAN mode for that. `*.localhost` resolves to `127.0.0.1` on most
  systems, so `app.localhost` works out of the box on the same machine.
- **LAN by hostname (preferred over bare IP):** you need *both* names to
  resolve on every client — `bp.lan` **and** `app.bp.lan`. Use your LAN DNS /
  router, an mDNS `*.local` name, or a per-client `hosts`-file entry (e.g.
  `192.168.1.50  bp.lan app.bp.lan`). Then both sites share `:443` (no extra
  port) and you only deal with the one internal-CA warning.
- **LAN by bare IP:** when you can't add DNS, choose `EDGE_MODE=ip` and set
  `PUBLIC_IP=<ip>`; the webapp gets `https://<ip>:${WEBAPP_HTTPS_PORT}` (default
  `8443`). The `caddy` service publishes that port; **open it on the host
  firewall** so LAN clients can reach it. The router stays on `https://<ip>`.
  A bare IP is **always https** via the internal CA — the generated Caddyfile
  pins `tls internal` (Let's Encrypt won't issue for an IP literal) and adds a
  global `default_sni <ip>` (an IP client sends no SNI, and behind Docker NAT
  Caddy can't otherwise match a cert → handshake `tls: internal error`).
- **Public mode prerequisites:** the domain's DNS A/AAAA record must point at
  this host, and inbound **`:80` and `:443` must be reachable** (Caddy needs
  `:80` for the ACME challenge and the HTTP→HTTPS redirect). Behind NAT,
  forward both ports.
- **LAN / localhost trust:** Caddy signs with a per-instance internal CA, so
  LAN clients see a certificate warning. To remove it, export Caddy's root
  certificate (it lives under the `caddy_data` volume — see Caddy's
  [Local HTTPS / `caddy trust` docs](https://caddyserver.com/docs/automatic-https#local-https)
  for the current path and the `caddy trust` helper) and import it into each
  client's trust store. For an internal tool it's also fine to just accept
  the warning.
- **Custom split:** the two hostnames are independent — e.g. a public
  `WEBAPP_DOMAIN` for users plus a LAN-only `PUBLIC_DOMAIN` to keep `/admin`
  off the internet. Edit `deploy/.env.prod` and re-run `scripts/prod.sh`
  (→ restart) to apply.
- **Editing routing** (extra paths, headers, a third host): the served file is
  `deploy/Caddyfile.generated` (gitignored). Re-run `scripts/prod.sh` to
  regenerate it after changing `EDGE_MODE`/identities, or hand-edit it (and add
  the directive to `scripts/render-caddyfile.sh` so it survives the next
  regenerate). It's bind-mounted read-only, so a `restart` reloads it.

### Serving HTTP at the origin (TLS terminated upstream)

When something **in front of** Caddy already terminates TLS — a **Cloudflare
Tunnel**, an external load balancer / ingress, `ngrok`, a corporate proxy —
you don't want Caddy provisioning its own certificate. Set **`EDGE_SCHEME=http`**
(`prod.sh` asks *"TLS terminated upstream?"*; pick **y**). Caddy then serves
**plain HTTP** at the origin and **disables automatic HTTPS** (no cert, no
`:80→:443` redirect). The upstream connects to the container over HTTP
(e.g. a Cloudflare Tunnel `service: http://caddy:80`, or the bare-IP/port
forms above on `http://`).

This is **not** an insecure deployment: clients still reach the stack over
**HTTPS** (the tunnel/LB provides it), so the *public* scheme is unchanged —
`ROUTER_PUBLIC_URL` stays `https://…` and the webapp's **Secure** session
cookie keeps working. Only the single origin hop (upstream ⇄ Caddy, normally
on a private network) is HTTP.

| | `EDGE_SCHEME=https` (default) | `EDGE_SCHEME=http` |
| --- | --- | --- |
| Caddy provisions TLS | yes (Let's Encrypt / internal CA) | **no** — upstream does |
| Origin listener | `:443` (+ `:80` redirect) | `:80` (the `:443` mapping is simply unused) |
| Use when | Caddy is your edge | behind Cloudflare Tunnel / LB / ngrok |
| Public scheme seen by clients | https | https (via the upstream) |

> **`EDGE_SCHEME=http` assumes the public endpoint is still HTTPS** (provided
> by the upstream). A *fully* plain-HTTP public deployment — no TLS anywhere —
> is intentionally **not** a supported prod configuration: the webapp's prod
> validator requires `WEBAPP_SESSION_COOKIE_SECURE=true` (a Secure cookie that
> a browser won't send over plain HTTP), so the browser channel can't log in
> over public HTTP. If you truly need that (a throwaway trial on a trusted
> network) you'd have to drop the webapp out of `prod`
> (`WEBAPP_DEPLOYMENT_ENV=staging`) to relax the guard and set
> `ROUTER_PUBLIC_URL=http://…` — losing the prod hardening. Use the
> upstream-TLS path instead; it keeps everything https-correct.

#### Example: public domain via Cloudflare Tunnel **and** LAN by IP (`EDGE_MODE=both`)

A common shape: reach the stack publicly through a Cloudflare Tunnel (TLS
terminated at Cloudflare, so the origin is plain HTTP) **and** directly on the
LAN by IP (https, internal CA). Pick `EDGE_MODE=both` in `prod.sh`, answer the
domain prompts with **TLS terminated upstream = y** (`EDGE_SCHEME=http`), and
give the bare IP. The resulting `deploy/.env.prod`:

```ini
EDGE_MODE=both
PUBLIC_DOMAIN=bp.example.com
WEBAPP_DOMAIN=app.example.com
EDGE_SCHEME=http                 # origin is plain HTTP; the tunnel does TLS
PUBLIC_IP=192.168.1.50
WEBAPP_HTTPS_PORT=8443
ROUTER_PUBLIC_URL=https://bp.example.com
```

The generated Caddyfile serves the two domains on **`http://`** (the tunnel
connects to `caddy:80`) and the IP on **`https://`** with the internal CA. Point
`cloudflared` at Caddy's HTTP port — the router and webapp need **separate
hostnames** (distinct `Host` headers), so give each its own ingress rule:

```yaml
# ~/.cloudflared/config.yml (tunnel side)
ingress:
  - hostname: bp.example.com        # router + admin UI
    service: http://caddy:80
  - hostname: app.example.com       # webapp (browser channel)
    service: http://caddy:80
  - service: http_status:404
```

LAN clients reach `https://192.168.1.50` (router/admin) and
`https://192.168.1.50:8443` (webapp) directly — trust Caddy's root CA to clear
the warning. Public clients use `https://bp.example.com` / `https://app.example.com`
with Cloudflare's cert. The Secure session cookie stays valid on every path
(all are HTTPS to the browser).
