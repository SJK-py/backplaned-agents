#!/usr/bin/env bash
#
# scripts/dev-up.sh — bring up the dev stack from a fresh clone.
#
# Steps:
#   1. Start Postgres via docker-compose.dev.yml (Redis/SearXNG/SeaweedFS opt-in).
#   2. Wait for Postgres healthcheck.
#   3. Generate ./.env from .env.example with fresh secrets if it
#      doesn't exist yet (idempotent — won't overwrite an existing one).
#   4. Run alembic upgrade head.
#   5. Print the next command (`python -m bp_router`) so the operator
#      can boot the router themselves and watch the logs.
#
# This script encodes the smoke-test flow that PRs #88-#91 chased
# down: every step the upstream-bug reports walked through. If
# you hit a failure here, that's a regression worth filing.
#
# Usage:
#   scripts/dev-up.sh
#   scripts/dev-up.sh --no-bootstrap-admin    # skip seeding the first admin
#
# Requirements:
#   - docker (compose v2 plugin) OR a host-managed Postgres+Redis already
#     listening on 5432 / 6379. Set BP_SKIP_COMPOSE=1 to skip step 1.
#   - python3.12+ (project's pyproject.toml minimum).
#   - openssl for secret generation.
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

BOOTSTRAP_ADMIN=1
for arg in "$@"; do
    case "$arg" in
        --no-bootstrap-admin) BOOTSTRAP_ADMIN=0 ;;
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

log() { printf '\033[1;36m[dev-up]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev-up]\033[0m %s\n' "$*" >&2; }

# 1. Containers
if [[ "${BP_SKIP_COMPOSE:-0}" != "1" ]]; then
    if ! command -v docker >/dev/null; then
        warn "docker not found; set BP_SKIP_COMPOSE=1 to use a host-managed Postgres+Redis"
        exit 1
    fi
    log "starting Postgres via docker-compose.dev.yml (Redis/SearXNG/SeaweedFS are opt-in profiles)"
    docker compose -f docker-compose.dev.yml up -d
else
    log "BP_SKIP_COMPOSE=1 — assuming Postgres+Redis are already running"
fi

# 2. Wait for Postgres — probe INSIDE the container (pg_isready) so this
# doesn't depend on a host-side `psql` client (its absence previously read
# as a false "didn't come up"). The router on the host still connects over
# the published port; if THAT fails, it's a host->container reachability
# issue (rootless/remote Docker), surfaced by the router itself.
log "waiting for Postgres (container pg_isready) …"
for i in {1..30}; do
    if docker compose -f docker-compose.dev.yml exec -T postgres \
        pg_isready -U postgres -d bp_router >/dev/null 2>&1; then
        log "  Postgres ready"
        break
    fi
    sleep 1
    if [[ $i -eq 30 ]]; then
        warn "Postgres didn't come up in 30s"
        exit 1
    fi
done

# 3. .env (idempotent)
if [[ ! -f .env ]]; then
    log "generating ./.env with fresh secrets"
    JWT=$(openssl rand -base64 48 | tr -d '\n')
    SESS=$(openssl rand -base64 48 | tr -d '\n')
    ADMIN_PW=$(openssl rand -base64 12 | tr -d '\n=' | head -c 16)
    MCP_SECRET=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40)
    cat > .env <<EOF
ROUTER_DB_URL=postgresql://postgres:bp@127.0.0.1:5432/bp_router
ROUTER_PUBLIC_URL=http://localhost:8000
ROUTER_JWT_SECRET=$JWT
ROUTER_ADMIN_SESSION_SECRET=$SESS
ROUTER_DEPLOYMENT_ENV=dev
ROUTER_LOG_LEVEL=INFO
# MCP bridge (service_mcp). The router seeds + re-arms this as the bridge's
# refresh-token credential; run-suite.sh launches the bridge with the same
# value. Comment out to not run the MCP bridge in dev.
ROUTER_MCP_BRIDGE_SECRET=$MCP_SECRET
# Redis is opt-in for dev. Without it the router uses a per-process fallback
# and the suite uses an in-process session lock — fine for single-channel use.
# Enable it (one container backs both) for cross-process correctness, e.g.
# driving the SAME session from both the Telegram bot and the webapp:
#   docker compose -f docker-compose.dev.yml --profile redis up -d
# then uncomment both:
# ROUTER_REDIS_URL=redis://localhost:6379/0    # router: JWT revocation + rate-limit
# SUITE_REDIS_URL=redis://localhost:6379/1     # suite: distributed session lock
# LLM provider key for the router's seeded presets (the suite's `default`
# preset resolves env://GEMINI_API_KEY). Set this before booting the router.
GEMINI_API_KEY=
EOF
    if [[ $BOOTSTRAP_ADMIN -eq 1 ]]; then
        # RFC-2606 example.com — Pydantic EmailStr rejects .test / .invalid
        # as "special-use" TLDs, so the bootstrap row would be unloggable.
        cat >> .env <<EOF
ROUTER_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
ROUTER_BOOTSTRAP_ADMIN_PASSWORD=$ADMIN_PW
EOF
        log ""
        log "first-admin bootstrap configured:"
        log "  email:    admin@example.com"
        log "  password: $ADMIN_PW"
        log ""
        log "  (also written to ./.env — back it up if you want it persisted"
        log "   beyond this dev scaffold)"
    else
        log "skipping first-admin bootstrap (--no-bootstrap-admin)"
    fi
else
    log "./.env already exists; leaving it alone"
fi

# 4. Migrations
log "running alembic upgrade head"
set -a
. ./.env
set +a
alembic upgrade head | sed 's/^/  /'

# 5. Hand off
log ""
log "dev stack ready. Boot the router with:"
log ""
log "    set -a && . ./.env && set +a && python -m bp_router"
log ""
log "Then in another shell:"
log "    curl http://127.0.0.1:8000/healthz"
log "    open http://127.0.0.1:8000/admin/login"
log ""
log "To run the agent suite (migrates bp_suite + launches all agents):"
log "    SUITE_TELEGRAM_BOT_TOKEN=... scripts/run-suite.sh"
log "    python -m bp_agents.load_acl     # first run only (suite ACL)"
