#!/usr/bin/env bash
#
# scripts/register-invitations.sh — register PRE-SUPPLIED agent invitation
# tokens (production bootstrap).
#
# The mint endpoint (POST /v1/admin/invitations) accepts a caller-supplied
# `token`, so production needs no mint -> copy -> paste round-trip: put one
# token per agent in your env file once, register them in a single pass with
# this script, and the SAME env feeds the agent containers via
# AGENT_INVITATION_TOKEN (CHATBOT_INVITATION, ORCHESTRATOR_INVITATION, ...).
#
# Usage:
#   # 1. Generate one token per agent and append them to your env file:
#   scripts/register-invitations.sh --gen >> deploy/.env.prod
#
#   # 2. Register them with the router (idempotent — safe to re-run):
#   ROUTER_URL=https://your.domain scripts/register-invitations.sh deploy/.env.prod
#
# Env (read from the env file or the environment):
#   ROUTER_URL                  router base URL (default http://127.0.0.1:8000)
#   BOOTSTRAP_ADMIN_EMAIL / _PASSWORD     (or ROUTER_BOOTSTRAP_ADMIN_*)
#   <AGENT>_INVITATION          the pre-supplied token for each agent
#
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON:-python}"

# name:ENV_VAR:provisions_service_user — only the chatbot provisions its
# usr_service_* principal (registration submit + per-user minting).
ROSTER=(
    "chatbot:CHATBOT_INVITATION:true"
    "webapp:WEBAPP_INVITATION:false"
    "orchestrator:ORCHESTRATOR_INVITATION:false"
    "history_summarizer:HISTORY_SUMMARIZER_INVITATION:false"
    "memory:MEMORY_INVITATION:false"
    "knowledge_base:KNOWLEDGE_BASE_INVITATION:false"
    "md_converter:MD_CONVERTER_INVITATION:false"
    "config:CONFIG_INVITATION:false"
    "deep_reasoning:DEEP_REASONING_INVITATION:false"
    "research:RESEARCH_INVITATION:false"
    "computer_use:COMPUTER_USE_INVITATION:false"
    "sandbox:SANDBOX_INVITATION:false"
)

# --gen: print `<VAR>=<fresh-token>` lines (44-char URL-safe; > the 32 min).
if [[ "${1:-}" == "--gen" ]]; then
    for entry in "${ROSTER[@]}"; do
        var="$(cut -d: -f2 <<<"$entry")"
        tok="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9_-' | head -c 44)"
        echo "${var}=${tok}"
    done
    exit 0
fi

log() { printf '\033[1;36m[register-invitations]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[register-invitations]\033[0m %s\n' "$*" >&2; exit 1; }

ENV_FILE="${1:-deploy/.env.prod}"
if [[ -f "$ENV_FILE" ]]; then
    log "loading $ENV_FILE"
    set -a; . "$ENV_FILE"; set +a
else
    log "no env file at $ENV_FILE — reading tokens from the environment"
fi

ROUTER_URL="${ROUTER_URL:-http://127.0.0.1:8000}"
ADMIN_EMAIL="${BOOTSTRAP_ADMIN_EMAIL:-${ROUTER_BOOTSTRAP_ADMIN_EMAIL:-}}"
ADMIN_PASSWORD="${BOOTSTRAP_ADMIN_PASSWORD:-${ROUTER_BOOTSTRAP_ADMIN_PASSWORD:-}}"
[[ -n "$ADMIN_EMAIL" && -n "$ADMIN_PASSWORD" ]] || fail "admin creds not set (BOOTSTRAP_ADMIN_EMAIL / _PASSWORD)"

curl -sf "$ROUTER_URL/healthz" >/dev/null || fail "router not reachable at $ROUTER_URL"

log "logging in as $ADMIN_EMAIL"
TOKEN=$(curl -sf -X POST "$ROUTER_URL/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
    | "$PYTHON_BIN" -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
[[ -n "$TOKEN" ]] || fail "login returned empty token"

registered=0
for entry in "${ROSTER[@]}"; do
    name="$(cut -d: -f1 <<<"$entry")"
    var="$(cut -d: -f2 <<<"$entry")"
    prov="$(cut -d: -f3 <<<"$entry")"
    val="${!var:-}"
    [[ -n "$val" ]] || fail "$var is empty — generate tokens with '--gen' first"
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$ROUTER_URL/v1/admin/invitations" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -H "Idempotency-Key: register-$name" \
        -d "{\"level\":\"tier1\",\"token\":\"$val\",\"provisions_service_user\":$prov}")
    case "$code" in
        201) log "$name: registered (provisions_service_user=$prov)"; registered=$((registered+1));;
        409) log "$name: already registered (idempotent)";;
        *)   fail "$name: register failed (HTTP $code)";;
    esac
done

log "done — $registered newly registered. Agents can now onboard with their \$<AGENT>_INVITATION."
