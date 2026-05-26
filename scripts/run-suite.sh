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

: "${ORCH_STATE_DIR:=/tmp/bp-suite/orchestrator}"
: "${CHATBOT_STATE_DIR:=/tmp/bp-suite/chatbot}"
mkdir -p "$ORCH_STATE_DIR" "$CHATBOT_STATE_DIR"

ORCH_TOKEN=""
CHATBOT_TOKEN=""
if [[ ! -f "$ORCH_STATE_DIR/credentials.json" ]]; then
    log "orchestrator: minting invitation"
    ORCH_TOKEN=$(mint_invite "orch-$(date +%s)" false)
    [[ -n "$ORCH_TOKEN" ]] || fail "orchestrator invitation mint returned empty"
else
    log "orchestrator: resuming from persisted creds"
fi
if [[ ! -f "$CHATBOT_STATE_DIR/credentials.json" ]]; then
    log "chatbot: minting service-provisioning invitation"
    CHATBOT_TOKEN=$(mint_invite "chatbot-$(date +%s)" true)
    [[ -n "$CHATBOT_TOKEN" ]] || fail "chatbot invitation mint returned empty"
else
    log "chatbot: resuming from persisted creds"
fi

cleanup() {
    log "tearing down suite agents"
    [[ -n "${ORCH_PID:-}" ]] && kill "$ORCH_PID" 2>/dev/null || true
    [[ -n "${CHATBOT_PID:-}" ]] && kill "$CHATBOT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "starting orchestrator (state=$ORCH_STATE_DIR)"
AGENT_INVITATION_TOKEN="$ORCH_TOKEN" \
    AGENT_ROUTER_URL="$WS_URL" \
    AGENT_STATE_DIR="$ORCH_STATE_DIR" \
    "$PYTHON_BIN" -m bp_agents.agents.orchestrator &
ORCH_PID=$!

log "starting chatbot (state=$CHATBOT_STATE_DIR)"
AGENT_INVITATION_TOKEN="$CHATBOT_TOKEN" \
    AGENT_ROUTER_URL="$WS_URL" \
    AGENT_STATE_DIR="$CHATBOT_STATE_DIR" \
    "$PYTHON_BIN" -m bp_agents.agents.chatbot &
CHATBOT_PID=$!

log "suite running (orchestrator=$ORCH_PID chatbot=$CHATBOT_PID). Ctrl-C to stop."
log "message the bot on Telegram and send /register to begin (admin approves the registration)."
wait
