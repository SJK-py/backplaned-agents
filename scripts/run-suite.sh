#!/usr/bin/env bash
#
# scripts/run-suite.sh — launch the v1 agent suite against a live router.
#
# Mirrors scripts/run-test-agents.sh, but for the suite agents:
#   1. Log in as the bootstrap admin → JWT.
#   2. Mint invitations as needed (stable state dirs → reruns resume):
#        - orchestrator: a plain agent invitation.
#        - chatbot:      an invitation flagged `provisions_service_user`
#                        so onboarding provisions its `usr_service_*`
#                        principal (registration submit + serviced-session
#                        discovery + per-user mint ride that identity).
#   3. Start `python -m bp_agents.agents.orchestrator` and
#      `python -m bp_agents.agents.chatbot` in the background, each with
#      its own state dir + invitation token + the suite DB URL.
#   4. Tail until Ctrl-C; tear the agents down on exit.
#
# Pre-reqs:
#   - The router is running on http://127.0.0.1:8000 (scripts/dev-up.sh).
#   - ./.env has ROUTER_BOOTSTRAP_ADMIN_EMAIL / _PASSWORD.
#   - The suite Postgres exists and is migrated:
#       export SUITE_DATABASE_URL=postgresql://postgres:bp@127.0.0.1:5432/bp_suite
#       alembic -c alembic_suite.ini upgrade head
#   - SUITE_TELEGRAM_BOT_TOKEN is set (a real Telegram bot token).
#   - The router's env has a working LLM key (e.g. GEMINI_API_KEY) for the
#     `default` preset the orchestrator uses.
#
# Usage:
#   SUITE_TELEGRAM_BOT_TOKEN=... scripts/run-suite.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

ROUTER_URL="${ROUTER_URL:-http://127.0.0.1:8000}"
WS_URL="${WS_URL:-ws://127.0.0.1:8000/v1/agent}"
PYTHON_BIN="${PYTHON:-python}"
: "${SUITE_DATABASE_URL:=postgresql://postgres:bp@127.0.0.1:5432/bp_suite}"
export SUITE_DATABASE_URL

log() { printf '\033[1;36m[run-suite]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[run-suite]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || fail ".env not found — run scripts/dev-up.sh first"
ADMIN_EMAIL=$(grep '^ROUTER_BOOTSTRAP_ADMIN_EMAIL=' .env | cut -d= -f2-)
ADMIN_PASSWORD=$(grep '^ROUTER_BOOTSTRAP_ADMIN_PASSWORD=' .env | cut -d= -f2-)
[[ -n "$ADMIN_EMAIL" && -n "$ADMIN_PASSWORD" ]] || fail "bootstrap admin creds not in .env"
[[ -n "${SUITE_TELEGRAM_BOT_TOKEN:-}" ]] || \
    log "WARNING: SUITE_TELEGRAM_BOT_TOKEN unset — the chatbot will connect but won't poll Telegram"

# Apply the suite schema (idempotent; skip with SKIP_SUITE_MIGRATE=1). The
# `bp_suite` database itself is created by docker-compose.dev.yml on first
# init — if this fails with "database bp_suite does not exist", recreate the
# dev Postgres volume (`down -v`) or `createdb ... bp_suite`.
if [[ "${SKIP_SUITE_MIGRATE:-0}" != "1" ]]; then
    log "applying suite schema (alembic -c alembic_suite.ini upgrade head)"
    "${ALEMBIC:-alembic}" -c alembic_suite.ini upgrade head | sed 's/^/  /' || \
        fail "suite migration failed (is bp_suite created + reachable at SUITE_DATABASE_URL?)"
fi

curl -sf "$ROUTER_URL/healthz" >/dev/null || fail "router not reachable at $ROUTER_URL"

log "logging in as $ADMIN_EMAIL"
TOKEN=$(curl -sf -X POST "$ROUTER_URL/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
    | "$PYTHON_BIN" -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
[[ -n "$TOKEN" ]] || fail "login returned empty token"

# Mint an invitation. $1 = idempotency key, $2 = provisions_service_user.
mint_invite() {
    curl -sf -X POST "$ROUTER_URL/v1/admin/invitations" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -H "Idempotency-Key: $1" \
        -d "{\"level\":\"tier1\",\"expires_in_s\":600,\"provisions_service_user\":$2}" \
        | "$PYTHON_BIN" -c "import json,sys;print(json.load(sys.stdin)['invitation_token'])"
}

# Agent roster: name:provisions_service_user. Only the chatbot owns the
# service identity (registration submit + per-user mint); the rest are
# plain agents.
AGENTS=(
    "orchestrator:false"
    "chatbot:true"
    "history_summarizer:false"
    "memory:false"
    "knowledge_base:false"
    "md_converter:false"
    "config:false"
    "deep_reasoning:false"
    "research:false"
    "computer_use:false"
    "sandbox:false"
)

PIDS=()
cleanup() {
    log "tearing down suite agents"
    for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

for entry in "${AGENTS[@]}"; do
    name="${entry%%:*}"
    prov="${entry##*:}"
    state="/tmp/bp-suite/$name"
    mkdir -p "$state"
    token=""
    if [[ ! -f "$state/credentials.json" ]]; then
        log "$name: minting invitation (provisions_service_user=$prov)"
        token=$(mint_invite "$name-$(date +%s)" "$prov")
        [[ -n "$token" ]] || fail "$name invitation mint returned empty"
    else
        log "$name: resuming from persisted creds"
    fi
    log "starting $name (state=$state)"
    AGENT_INVITATION_TOKEN="$token" \
        AGENT_ROUTER_URL="$WS_URL" \
        AGENT_STATE_DIR="$state" \
        "$PYTHON_BIN" -m "bp_agents.agents.$name" &
    PIDS+=("$!")
done

log "suite running (${#PIDS[@]} agents). Ctrl-C to stop."
log "First run only: apply the suite ACL with  python -m bp_agents.load_acl"
log "Then message the bot on Telegram and send /register (an admin approves it)."
wait
