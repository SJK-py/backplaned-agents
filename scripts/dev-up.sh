#!/usr/bin/env bash
#
# scripts/dev-up.sh — bring up the dev stack from a fresh clone.
#
# Steps:
#   1. Start Postgres + Redis via docker-compose.dev.yml.
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
    log "starting Postgres + Redis via docker-compose.dev.yml"
    docker compose -f docker-compose.dev.yml up -d
else
    log "BP_SKIP_COMPOSE=1 — assuming Postgres+Redis are already running"
fi

# 2. Wait for Postgres
log "waiting for Postgres on localhost:5432 …"
for i in {1..30}; do
    if PGPASSWORD=bp psql -U postgres -h 127.0.0.1 -d bp_router -c "SELECT 1" >/dev/null 2>&1; then
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
    cat > .env <<EOF
ROUTER_DB_URL=postgresql://postgres:bp@127.0.0.1:5432/bp_router
ROUTER_PUBLIC_URL=http://localhost:8000
ROUTER_JWT_SECRET=$JWT
ROUTER_ADMIN_SESSION_SECRET=$SESS
ROUTER_REDIS_URL=redis://localhost:6379/0
ROUTER_DEPLOYMENT_ENV=dev
ROUTER_LOG_LEVEL=INFO
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
