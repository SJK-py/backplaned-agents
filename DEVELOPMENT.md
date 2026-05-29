# Development setup

Bring the dev stack up from a fresh clone.

## Quick start

```bash
# 1. Postgres + Redis + .env + migrations.
scripts/dev-up.sh

# 2. Boot the router (separate shell).
set -a && . ./.env && set +a && python -m bp_router

# 3. Smoke-check.
curl http://127.0.0.1:8000/healthz                  # → {"status":"ok"}
curl -I http://127.0.0.1:8000/admin/login           # → HTTP/1.1 200 OK
```

The bootstrap admin's credentials print on `dev-up.sh`'s stdout
(also persisted in `.env`). Default email is
`admin@example.com` so it survives Pydantic's `EmailStr`
validation — see the note in `.env.example` about why `.test`
TLDs don't work here.

## Manual setup (no `dev-up.sh`)

If you'd rather wire it up by hand or your environment doesn't
have Docker:

```bash
# Postgres / Redis (host or container — your choice)
docker compose -f docker-compose.dev.yml up -d

# Project (Python 3.12+).
python -m venv .venv && . .venv/bin/activate
pip install -e ".[router,admin,dev]"

# Schema.
export ROUTER_DB_URL=postgresql://postgres:bp@127.0.0.1:5432/bp_router
alembic upgrade head

# Secrets + first admin.
cp .env.example .env
# edit .env, fill in:
#   ROUTER_JWT_SECRET=$(openssl rand -base64 32)
#   ROUTER_ADMIN_SESSION_SECRET=$(openssl rand -base64 32)
#   ROUTER_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
#   ROUTER_BOOTSTRAP_ADMIN_PASSWORD=...

# Boot.
set -a && . ./.env && set +a && python -m bp_router
```

## What "ready" looks like

The router's startup log emits these events in order. If any are
missing, that's a misconfiguration:

| Event | Means |
|---|---|
| `db_pool_opened` | Postgres reachable + pool sized |
| `redis_opened` | Redis reachable (omitted if `ROUTER_REDIS_URL` unset) |
| `acl_rules_loaded` | Bootstrap ACL rows present (3 from migration 0001) |
| `admin_console_agent_ensured` | Synthetic `admin_console` agent inserted |
| `bootstrap_admin_created` (or `_exists`) | First-admin seeding fired (only when env vars set) |
| `llm_presets_seeded count=34` (first boot) or `llm_presets_loaded count=34` | LLM presets table populated (count = the seeded `presets_catalog.jsonc` size) |
| `router_started` | Lifespan complete |
| `Application startup complete.` | uvicorn ready to serve |

## Smoke tests

There are three categories of admin-side smoke test. (Automated CI is
not yet configured — run these locally; if you change anything in
`bp_admin/`, run at least these three:)

```bash
pytest tests/test_admin_smoke.py            # standalone bp-admin
pytest tests/test_admin_mounted_smoke.py    # mounted under /admin
pytest tests/test_upstream_bugs_*.py        # boot-blocker regressions
```

The split between standalone and mounted matters — almost every
admin BFF bug in the upstream-bug chain (see commit history) was
caused by code that worked in one deployment shape but not the
other. Both shapes ship; both are tested.

## Agent test drive

Three runnable example agents live under `examples/test_drive/`:

| Agent | What it does |
| --- | --- |
| `echo_agent.py` | Receives `LLMData`, returns the prompt uppercased. |
| `caller_agent.py` | Spawns `echo_agent` via `ctx.peers.spawn(...)` and returns the composed result — exercises agent-to-agent dispatch. |
| `gemini_agent.py` | Calls Gemini through the `default` preset — real LLM round-trip. Surfaces `thoughts_tokens` in metadata so the thinking-budget split is visible. |

`scripts/run-test-agents.sh` codifies the full smoke:

```bash
scripts/run-test-agents.sh                       # agent-to-agent + Gemini if configured
scripts/run-test-agents.sh --skip-gemini         # agent-to-agent only
scripts/run-test-agents.sh --run-quota-test      # also drive the admit-rate quota
```

What it does:

1. Logs in as the bootstrap admin (reads creds from `.env`).
2. Mints invitations for any agent whose state dir lacks
   `credentials.json` (default state dirs live under
   `/tmp/bp-test-drive/`); subsequent runs resume via the
   persisted token, no fresh invitations needed.
3. Starts each agent in the background.
4. Drives `caller_agent → peers.spawn(echo_agent)` via
   `POST /v1/admin/tasks/test` and asserts the uppercase result.
5. If `GEMINI_API_KEY` is in `.env` AND `google-genai` is
   installed (the `llm-gemini` extra), drives a real Gemini
   call through `gemini_agent` and asserts on the response.
   Otherwise skips cleanly with a one-line note.
6. Tears the agents down on exit.

### Quota-leg setup (`--run-quota-test`)

Drives the admit-time per-user rate quota end-to-end. Fires N
admit calls back-to-back and asserts at least one returns
HTTP 429 with a `Retry-After` header + non-zero
`router_quota_exceeded_total` metric.

The shipped defaults leave `admin` and `service` levels uncapped
(see `Settings.quota_admit_rate_per_s`), so the leg won't
trigger out of the box. Configure a tight cap before running:

```bash
# In .env (gitignored — operator-local).
ROUTER_QUOTA_ADMIT_RATE_PER_S='{"admin": 2.0, "service": null, "tier0": 100.0, "tier1": 20.0, "tier2": 5.0, "tier3": 1.0}'
ROUTER_QUOTA_ADMIT_BURST='{"admin": 2, "service": null, "tier0": 200, "tier1": 40, "tier2": 10, "tier3": 2}'

# Restart the router so the new env is picked up.
# Then:
scripts/run-test-agents.sh --skip-gemini --run-quota-test
```

The dict-shape values are JSON, parsed by pydantic-settings; do
NOT shell-source `.env` (`set -a; . ./.env`) for this — bash
interprets the JSON as commands. `python -m bp_router` reads
`.env` directly. See the matching footgun row in the table at
the end of this file.

### Gemini-specific setup

```bash
pip install -e ".[llm-gemini]"        # google-genai >= 1.14
echo "GEMINI_API_KEY=<your-key>" >> .env
# Restart the router so the new env var is in scope when the
# secrets resolver looks up `env://GEMINI_API_KEY`.
```

The default preset (`name="default"`, provider `gemini`,
model `gemini-2.5-flash`) reads its key via
`api_key_ref="env://GEMINI_API_KEY"` — so the key has to live in
the **router process's** environment, not the agent's. (Agents talk
to the router; the router talks to Gemini.)

> **Thinking-token budget on Gemini 2.5+**: `max_tokens` is the
> total budget the model splits between hidden thoughts and visible
> output. A small cap (e.g. 256) gets eaten almost entirely by
> thoughts on creative prompts and the visible answer truncates
> with `finish_reason="length"`. Leave `max_tokens=None` and let
> the provider's default apply, OR raise the cap considerably.
> See `bp_sdk.llm.LlmServiceClient.generate`'s docstring and Bug 13
> in `tests/test_upstream_bugs_10_to_13.py` for the full audit.

## Tearing down

```bash
docker compose -f docker-compose.dev.yml down -v   # drops the volume too
rm .env                                             # secrets are local
```

## Why these specific defaults

- **`admin@example.com`** — RFC 2606 reserves `example.com` for
  documentation. `email-validator` (used by Pydantic's `EmailStr`)
  rejects `.test`, `.example`, `.localhost`, `.invalid` as
  special-use; using one of those in `ROUTER_BOOTSTRAP_ADMIN_EMAIL`
  silently creates an unloggable user (the bootstrap accepts the
  string but the auth login validates with `EmailStr` and rejects
  it). `dev-up.sh` defaults to `example.com` for that reason.
- **Postgres password `bp`** — matches `docker-compose.dev.yml`.
  Production should use a managed Postgres / a separate compose
  with proper secret management.
- **`deployment_env=dev`** — turns off the `Secure` flag on the
  admin session cookie so it works over plain HTTP localhost.
  Production deployments MUST flip this to `prod` before going
  public (the cookie carries the upstream JWTs in clear today;
  see `docs/design/admin-session-cookie-encryption.md` for the
  deferred work).

## Known dev-mode footguns

| Symptom | Cause | Fix |
|---|---|---|
| `/admin/login` 500s on first request | `bp_admin` failed to import | Install `[admin]` extra: `pip install -e ".[admin]"` |
| `csrf_validation_failed` 403 on POST | CSRF middleware path mismatch | Open an issue — should not happen post-PR #91 |
| `.test` admin can't log in | `EmailStr` rejects special-use TLDs | Use `example.com` |
| `bash: ROUTER_FILE_STORE_OPTIONS=...` parse error | Sourcing `.env` from shell | Don't `set -a; . ./.env`; let pydantic-settings load it |
| Gemini `finish_reason="length"` with only a handful of visible output tokens | `max_tokens` is the TOTAL budget on Gemini 2.5+; thinking ate it | Drop `max_tokens` (provider default applies) or raise it well above the thinking estimate |
| Router refuses to start with `ROUTER_REDIS_URL is required when deployment_env=...` | `staging` / `prod` mode without Redis configured | Set `ROUTER_REDIS_URL`, OR switch `ROUTER_DEPLOYMENT_ENV=dev` (single-worker only — multi-worker without Redis silently disables JWT revocation across workers) |
| Router refuses to start with `ROUTER_METRICS_TOKEN is required when deployment_env=...` | `staging` / `prod` mode without `/metrics` bearer configured | Generate one via `openssl rand -base64 32` and set `ROUTER_METRICS_TOKEN=<token>`; configure Prometheus scrapes with `Authorization: Bearer <token>` |
| `bash: 2.0,: command not found` when sourcing `.env` | JSON-shaped values like `ROUTER_QUOTA_ADMIT_RATE_PER_S='{"admin": 2.0}'` | Don't `set -a; . ./.env`; pydantic-settings parses these correctly when `python -m bp_router` reads `.env` directly |
| WS clients see 4029 / `reason="rate_limited"` close codes after burst reconnects | Per-IP handshake rate limit on `/v1/agent` (default 5/s burst 20) | Loosen via `ROUTER_WS_HANDSHAKE_RATE_LIMIT_PER_IP_PER_S` / `_BURST`; set rate=0 to disable |
| Agent connects fail with `payload_too_large` / close code 1009 on Hello | Hello frame exceeded `ROUTER_MAX_PAYLOAD_BYTES` (default 1 MiB) | The cap is shared with per-frame limits; raise for genuinely large `AgentInfo` payloads |
| Soft-deleted user's still-valid JWT returns 401 on every authenticated endpoint | Expected: `_principal_from_request` consults `users.deleted_at` per-request (cached via `LlmService._user_level_cache`). `delete_user` invalidates the cache synchronously | Restore the user (set `deleted_at = NULL`) — soft-delete is reversible at the SQL level even though there's no `POST /v1/admin/users/{id}/restore` endpoint yet |
