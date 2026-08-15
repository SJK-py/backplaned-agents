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
#
# TWO tokens, not twelve (`docs/design/deployment-agent-host.md` §3):
#
#   SUITE_ROSTER_TOKEN   one token bound to every agent that does NOT
#                        provision a service user, each name consumable once.
#                        Bound to names, so it is strictly tighter than the
#                        twelve it replaces: an invitation with no roster can
#                        onboard as ANY name, because `POST /v1/onboard` takes
#                        the name from the agent's own `agent_info`.
#   CHATBOT_INVITATION   kept separate on purpose — it is flagged
#                        `provisions_service_user`, a higher-privilege
#                        credential yielding a minting-capable principal, and
#                        eleven ordinary agents must not inherit that.
gen_token() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9_-' | head -c 44; }

if [[ "${1:-}" == "--gen" ]]; then
    echo "SUITE_ROSTER_TOKEN=$(gen_token)"
    for entry in "${ROSTER[@]}"; do
        prov="$(cut -d: -f3 <<<"$entry")"
        [[ "$prov" == "true" ]] || continue
        var="$(cut -d: -f2 <<<"$entry")"
        echo "${var}=$(gen_token)"
    done
    exit 0
fi

# --gen-per-agent: the pre-roster shape, one token per agent. Kept for a
# deployment that registers agents individually (or is mid-migration).
if [[ "${1:-}" == "--gen-per-agent" ]]; then
    for entry in "${ROSTER[@]}"; do
        var="$(cut -d: -f2 <<<"$entry")"
        echo "${var}=$(gen_token)"
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

# NO per-name Idempotency-Key anywhere below. The token itself is the natural
# dedup key — re-registering the SAME token collides on the token-hash PK and
# returns 409, which is exactly right. A per-name key (`register-<name>`) was
# actively WRONG with fresh-token-per-launch (prod.sh regenerates them): the
# router's idempotency contract returns the EXISTING row for a repeated key
# and IGNORES the new token, so the relaunch's token was never registered and
# the agent then presented an unregistered one → 403. This mirrors
# `bp_agents/bootstrap.py`, which is what the compose `init` one-shot runs;
# keep the two in step.

# ROSTER PATH. One token bound to every agent that does NOT provision a
# service user. `--gen` emits exactly this plus the chatbot's, so this is the
# normal case — without it, the eleven per-agent vars below are all unset and
# nothing would be registered at all.
if [[ -n "${SUITE_ROSTER_TOKEN:-}" ]]; then
    names=$(for entry in "${ROSTER[@]}"; do
        [[ "$(cut -d: -f3 <<<"$entry")" == "false" ]] || continue
        printf '"%s",' "$(cut -d: -f1 <<<"$entry")"
    done)
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$ROUTER_URL/v1/admin/invitations" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d "{\"level\":\"tier1\",\"token\":\"$SUITE_ROSTER_TOKEN\",\"agent_ids\":[${names%,}],\"provisions_service_user\":false}")
    case "$code" in
        201) log "roster: registered"; registered=$((registered+1));;
        409) log "roster: already registered (idempotent)";;
        *)   fail "roster: register failed (HTTP $code)";;
    esac
fi

# PER-AGENT PATH. Still the ONLY way to register a `provisions_service_user`
# invitation (the chatbot's), and still the whole story for a deployment that
# used `--gen-per-agent`. An unset var is skipped rather than fatal: with a
# roster token set, every name but the chatbot is covered by it, and failing
# here told the operator to run `--gen` — the very command that had just
# deliberately not emitted these.
for entry in "${ROSTER[@]}"; do
    name="$(cut -d: -f1 <<<"$entry")"
    var="$(cut -d: -f2 <<<"$entry")"
    prov="$(cut -d: -f3 <<<"$entry")"
    val="${!var:-}"
    if [[ -z "$val" ]]; then
        [[ -n "${SUITE_ROSTER_TOKEN:-}" ]] || log "skip $name: $var unset and no SUITE_ROSTER_TOKEN"
        continue
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$ROUTER_URL/v1/admin/invitations" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d "{\"level\":\"tier1\",\"token\":\"$val\",\"provisions_service_user\":$prov}")
    case "$code" in
        201) log "$name: registered (provisions_service_user=$prov)"; registered=$((registered+1));;
        409) log "$name: already registered (idempotent)";;
        *)   fail "$name: register failed (HTTP $code)";;
    esac
done

[[ "$registered" -gt 0 || -n "${SUITE_ROSTER_TOKEN:-}" ]] \
    || fail "nothing registered — set SUITE_ROSTER_TOKEN + CHATBOT_INVITATION ('--gen')"
log "done — $registered newly registered. Agents can now onboard."
