# backplaned

Multi-user agent router with a WebSocket transport, a Postgres backend,
and a typed Python SDK.

A central router multiplexes typed frames between agents over a single
WebSocket per agent, persists task state in Postgres with an enforced
state machine, gates inter-agent calls through a firewall-style ACL,
and exposes provider-tailored LLM bridges as a first-class service.

The full design lives in [`docs/`](./docs):

- [`docs/overview.md`](./docs/overview.md) — principles, architecture, departures from the legacy stack.
- [`docs/router/`](./docs/router) — wire protocol, task state machine, schema, HTTP API, sequencing.
- [`docs/sdk/`](./docs/sdk) — agent surface, transports, services, worked Gemini agent example.
- [`docs/acl.md`](./docs/acl.md) — firewall-style ACL: rule grammar, pattern slots, user-level matching.
- [`docs/observability.md`](./docs/observability.md) — span/log/metric conventions.
- [`docs/security.md`](./docs/security.md) — threat model, tokens, secrets.

## Packages

```
bp_protocol/   # Shared frames + types (consumed by both router and SDK)
bp_router/     # The router — FastAPI + asyncpg + Redis + WebSockets
bp_sdk/        # The agent SDK — Agent, TaskContext, services
```

## Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[router,llm-gemini,admin,dev]"
```

Optional extras:

| Extra            | Adds                                                              |
| ---------------- | ----------------------------------------------------------------- |
| `router`         | Router runtime (Postgres, Redis, etc.)                            |
| `admin`          | `bp_admin` web UI (Jinja2; static assets via CDN, no JS build)    |
| `llm-gemini`     | `google-genai` for Gemini provider                                |
| `storage-s3`     | `aioboto3` for S3/R2/MinIO                                        |
| `dev`            | `pytest`, `ruff`, `mypy`                                          |

The agent SDK only needs the core dependencies (no optional extras
required) to run external agents.

## Run the router (local)

The fastest path is `scripts/dev-up.sh` — see [DEVELOPMENT.md](./DEVELOPMENT.md)
for the codified bring-up. The manual flow:

```bash
# 1. Start Postgres.
docker compose -f docker-compose.dev.yml up -d

# 2. Apply the schema.
export ROUTER_DB_URL=postgresql://postgres:bp@localhost:5432/bp_router
alembic upgrade head

# 3. Configure and run the router.
export ROUTER_PUBLIC_URL=http://localhost:8000
export ROUTER_JWT_SECRET=$(openssl rand -base64 32)
export ROUTER_ADMIN_SESSION_SECRET=$(openssl rand -base64 32)   # required when admin UI is mounted

# 3a. (First boot only.) Seed the initial admin user so you can
# log into /admin. `POST /v1/admin/users` requires an existing
# admin (chicken-and-egg), so this is the supported way to
# create the very first one. Idempotent — safe to leave set
# across restarts; subsequent boots no-op if a user with this
# email already exists.
export ROUTER_BOOTSTRAP_ADMIN_EMAIL=admin@local.test
export ROUTER_BOOTSTRAP_ADMIN_PASSWORD=$(openssl rand -base64 16 | tee /dev/tty)

bp-router
```

The router listens on port 8000. Health endpoints are at `/healthz`
and `/readyz`; Prometheus exposition at `/metrics` (bearer-gated via
`ROUTER_METRICS_TOKEN`; required in staging/prod, optional in dev);
OpenAPI docs at `/docs`; admin web UI at `/admin/login` (mounted by
default — disable with `ROUTER_SERVE_ADMIN_UI=false` to deploy the
admin separately via `bp-admin`).

## Write an agent

```python
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

agent = Agent(info=AgentInfo(
    agent_id="echo",
    description="Echoes the prompt back, in uppercase.",
    capabilities=["text.transform.uppercase"],
))

@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    return AgentOutput(content=payload.prompt.upper())

if __name__ == "__main__":
    agent.run()
```

To run, the agent needs an invitation token from a router admin:

```bash
export AGENT_ROUTER_URL=ws://localhost:8000/v1/agent
export AGENT_INVITATION_TOKEN=...   # one-shot from POST /v1/admin/invitations
python my_agent.py
```

The SDK persists credentials under `state_dir/credentials.json` after
the first onboarding so subsequent runs reconnect with the cached
auth token.

Three runnable examples live under [`examples/test_drive/`](./examples/test_drive):

| File | What it does |
| --- | --- |
| `echo_agent.py` | Echoes the prompt back, uppercased. |
| `caller_agent.py` | Spawns `echo_agent` via `ctx.peers.spawn(...)` — agent-to-agent round-trip. |
| `gemini_agent.py` | Calls Gemini through the `default` preset — real LLM round-trip. |

`scripts/run-test-agents.sh` codifies the bring-up + smoke flow for
all three (the Gemini leg skips cleanly when `GEMINI_API_KEY` isn't
configured). See [DEVELOPMENT.md](./DEVELOPMENT.md#agent-test-drive)
for the prereqs and the per-leg expectations.

## Tests

```bash
# End-to-end smoke test against a real router + Postgres
export TEST_DB_URL=postgresql://postgres:bp@localhost:5432/bp_router
alembic upgrade head
pytest tests/test_smoke_e2e.py -xvs
```

## Status

Early-development. The wire protocol, frame schema, DB schema, ACL
grammar, JWT lifecycle, SDK surface, and the per-user admit-rate
quota are stable. Provider adapters ship for Gemini, Anthropic,
OpenAI, and any OpenAI-compatible endpoint (vLLM, LM Studio,
llama.cpp-server). Items still on the roadmap include HashiCorp
Vault / AWS Secrets Manager / GCP Secret Manager backends for
`secret_ref://`, the concurrent-tasks-per-user cap and per-agent
inbound frame rate cap (see
[`docs/design/quota-enforcement.md`](./docs/design/quota-enforcement.md)
§7 phase 3 + §11), and finer-grained OTel span instrumentation
across the dispatch path.

## License

See [LICENSE](./LICENSE).
