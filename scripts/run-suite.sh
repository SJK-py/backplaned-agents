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
# The sandbox writes its per-user workspace under SUITE_SANDBOX_ROOT/<user_id>.
# Default to a writable /tmp dir — the production default (/home) isn't
# writable by a non-root dev user, so `mkdir` would fail on the first bash.
: "${SUITE_SANDBOX_ROOT:=/tmp/bp-suite-sandbox}"
export SUITE_SANDBOX_ROOT
# Per-agent state (credentials.json) MUST be durable: agents onboard ONCE
# (invitation tokens are single-use) and resume from persisted creds forever
# after. /tmp is wiped on reboot — losing creds while the router keeps the
# agent registered leaves it un-onboardable (409) with no recovery short of a
# router DB reset. So default to a stable XDG state dir, overridable.
: "${BP_SUITE_STATE_ROOT:=${XDG_STATE_HOME:-$HOME/.local/state}/bp-suite}"
# The webapp agent serves a browser UI (FastAPI) and needs a cookie-signing
# secret + (over http://localhost) a non-secure cookie. Default insecure dev
# values so `run-suite.sh` works out of the box; override for anything real.
: "${WEBAPP_SESSION_SECRET:=dev-insecure-change-me-000000000000000000000000}"
export WEBAPP_SESSION_SECRET
: "${WEBAPP_SESSION_COOKIE_SECURE:=false}"
export WEBAPP_SESSION_COOKIE_SECURE

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

# Can this agent resume from its persisted creds? True iff
# <state>/credentials.json holds a non-empty auth_token that is either
# non-expiring or not yet expired — mirroring the SDK's onboard_or_resume
# load check. Stale/expired/corrupt creds (e.g. a wiped router, a lapsed
# onboard token) return false so the caller re-mints instead of starting an
# agent that can't onboard (the bug behind "no auth_token and no
# invitation_token" on every agent).
creds_resumable() {
    local f="$1/credentials.json"
    [[ -f "$f" ]] || return 1
    "$PYTHON_BIN" - "$f" <<'PY'
import json, sys
from datetime import datetime, timezone
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if not d.get("auth_token"):
    sys.exit(1)
exp = d.get("expires_at")
if exp:
    try:
        if datetime.fromisoformat(exp.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            sys.exit(1)
    except ValueError:
        pass  # unparseable expiry → let the SDK decide
sys.exit(0)
PY
}

# Router-side registration status for $1, or empty if not registered (404).
# Used to pre-empt the cryptic onboard 409 when we're about to mint for an
# agent the router already knows but we have no local creds for.
agent_status() {
    curl -sf -H "Authorization: Bearer $TOKEN" \
        "$ROUTER_URL/v1/admin/agents/$1" 2>/dev/null \
        | "$PYTHON_BIN" -c "import json,sys;print(json.load(sys.stdin).get('status',''))" \
        2>/dev/null || true
}

# Agent roster: name:provisions_service_user. Only the chatbot owns the
# service identity (registration submit + per-user mint); the rest are
# plain agents.
AGENTS=(
    "orchestrator:false"
    "chatbot:true"
    "webapp:false"
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
    state="$BP_SUITE_STATE_ROOT/$name"
    mkdir -p "$state"
    token=""
    if creds_resumable "$state"; then
        log "$name: resuming from persisted creds"
    else
        # No usable local creds. If the router already has this agent
        # registered, a fresh onboard would 409 with no recovery — surface
        # that as actionable guidance instead of a cryptic per-agent crash.
        st=$(agent_status "$name")
        if [[ -n "$st" && "$st" != "pending" ]]; then
            fail "$name is registered on the router (status=$st) but has no usable creds at
  $state — a fresh onboard will be rejected (409). Recover by either:
    • restoring that agent's credentials.json, or
    • resetting the dev router DB and re-onboarding all agents:
        docker compose -f docker-compose.dev.yml down -v && scripts/dev-up.sh
        # then re-migrate (router + bp_suite) + re-bootstrap admin, and rerun.
  (Creds now persist under \$BP_SUITE_STATE_ROOT=$BP_SUITE_STATE_ROOT, not /tmp,
   so this won't recur across reboots.)"
        fi
        # Fresh, or stale/expired/corrupt creds → drop them and re-mint so
        # onboarding has a usable invitation (self-heals a prior failed run).
        if [[ -e "$state/credentials.json" ]]; then
            log "$name: persisted creds unusable — re-minting"
            rm -f "$state/credentials.json"
        fi
        log "$name: minting invitation (provisions_service_user=$prov)"
        token=$(mint_invite "$name-$(date +%s)" "$prov")
        [[ -n "$token" ]] || fail "$name invitation mint returned empty"
    fi
    if [[ "$name" == "sandbox" ]]; then
        log "WARNING: 'sandbox' runs UNCONTAINED on this host — LLM/user bash"
        log "         executes as $(whoami) with NO isolation (workspace:"
        log "         $SUITE_SANDBOX_ROOT). Dev/trusted use only; for real isolation"
        log "         run the hardened sandbox container (docker-compose.prod.yml)."
    fi
    log "starting $name (state=$state)"
    AGENT_INVITATION_TOKEN="$token" \
        AGENT_ROUTER_URL="$WS_URL" \
        AGENT_STATE_DIR="$state" \
        "$PYTHON_BIN" -m "bp_agents.agents.$name" &
    PIDS+=("$!")
done

log "suite running (${#PIDS[@]} agents)."

# Apply the suite ACL automatically (idempotent; skip with SKIP_ACL=1). Reuses
# the same admin creds + router; replaces the old "remember to run load_acl"
# manual step.
if [[ "${SKIP_ACL:-0}" != "1" ]]; then
    log "applying suite ACL (python -m bp_agents.load_acl)"
    ROUTER_URL="$ROUTER_URL" "$PYTHON_BIN" -m bp_agents.load_acl | sed 's/^/  /' || \
        log "WARNING: load_acl failed — apply it manually with python -m bp_agents.load_acl"
fi

log "web UI: http://127.0.0.1:${WEBAPP_BIND_PORT:-8002}  (log in with your email + a web password — send /password to the bot to set one)"
log "Ctrl-C to stop. Message the bot on Telegram and send /register (an admin approves it)."
wait
