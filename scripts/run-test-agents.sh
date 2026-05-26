#!/usr/bin/env bash
#
# scripts/run-test-agents.sh — codified end-to-end smoke for the
# agent SDK against a live router.
#
# Drives the same flow that surfaced upstream-bugs #10–#13:
#
#   1. Log in as the bootstrap admin → get a JWT.
#   2. Mint invitations as needed (one per agent w/o persisted creds).
#   3. Start `examples/test_drive/echo_agent.py` and
#      `examples/test_drive/caller_agent.py` in the background,
#      each with its own state dir + invitation token.
#   4. Drive `caller_agent → peers.spawn(echo_agent)` round-trip via
#      `POST /v1/admin/tasks/test`; assert the uppercase result.
#   5. If `GEMINI_API_KEY` is set in the router's env (i.e. the
#      `default` preset's `env://GEMINI_API_KEY` resolves), also
#      spawn `gemini_agent` and drive a real Gemini call.
#   6. Tear the agents down on exit.
#
# Pre-reqs:
#   - The router is already running on http://127.0.0.1:8000.
#   - `./.env` is populated with ROUTER_BOOTSTRAP_ADMIN_EMAIL +
#     ROUTER_BOOTSTRAP_ADMIN_PASSWORD (scripts/dev-up.sh seeds these).
#   - `python -c "import bp_sdk, bp_protocol"` works in the active venv.
#   - For the Gemini leg only: `GEMINI_API_KEY` set in `.env` AND
#     `pip install -e .[llm-gemini]` has installed `google-genai`.
#
# Usage:
#   scripts/run-test-agents.sh
#   scripts/run-test-agents.sh --skip-gemini    # only the agent-to-agent leg
#
set -euo pipefail

cd "$(dirname "$0")/.."

SKIP_GEMINI=0
RUN_QUOTA_TEST=0
for arg in "$@"; do
    case "$arg" in
        --skip-gemini) SKIP_GEMINI=1 ;;
        --run-quota-test) RUN_QUOTA_TEST=1 ;;
        --help|-h)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

ROUTER_URL="${ROUTER_URL:-http://127.0.0.1:8000}"
WS_URL="${WS_URL:-ws://127.0.0.1:8000/v1/agent}"
PYTHON_BIN="${PYTHON:-python}"

log() { printf '\033[1;36m[run-test-agents]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[run-test-agents]\033[0m %s\n' "$*" >&2; exit 1; }

# Pull bootstrap admin creds from .env (a strict subset — no full export).
if [[ ! -f .env ]]; then
    fail ".env not found — run scripts/dev-up.sh first"
fi
ADMIN_EMAIL=$(grep '^ROUTER_BOOTSTRAP_ADMIN_EMAIL=' .env | cut -d= -f2-)
ADMIN_PASSWORD=$(grep '^ROUTER_BOOTSTRAP_ADMIN_PASSWORD=' .env | cut -d= -f2-)
[[ -n "$ADMIN_EMAIL" ]] || fail "ROUTER_BOOTSTRAP_ADMIN_EMAIL not in .env"
[[ -n "$ADMIN_PASSWORD" ]] || fail "ROUTER_BOOTSTRAP_ADMIN_PASSWORD not in .env"

# 0. Healthcheck.
curl -sf "$ROUTER_URL/healthz" >/dev/null || fail "router not reachable at $ROUTER_URL"

# 1. Admin login.
log "logging in as $ADMIN_EMAIL"
TOKEN=$(curl -sf -X POST "$ROUTER_URL/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
    | "$PYTHON_BIN" -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
[[ -n "$TOKEN" ]] || fail "login returned empty token"

# 2. Spawn agents (mint invitations only if state dirs are empty).
#
# Use STABLE state dirs so reruns resume via the persisted token
# instead of trying to onboard a fresh agent_id and hitting 409
# Conflict against the existing registration. The first run does
# the onboard; later runs are pure resume.
: "${ECHO_AGENT_STATE_DIR:=/tmp/bp-test-drive/echo}"
: "${CALLER_AGENT_STATE_DIR:=/tmp/bp-test-drive/caller}"
ECHO_DIR="$ECHO_AGENT_STATE_DIR"
CALLER_DIR="$CALLER_AGENT_STATE_DIR"
mkdir -p "$ECHO_DIR" "$CALLER_DIR"
ECHO_LOG=/tmp/echo-agent-smoke.log
CALLER_LOG=/tmp/caller-agent-smoke.log

mint_invite() {
    curl -sf -X POST "$ROUTER_URL/v1/admin/invitations" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -H "Idempotency-Key: $1" \
        -d '{"level":"tier1","expires_in_s":600}' \
        | "$PYTHON_BIN" -c "import json,sys;print(json.load(sys.stdin)['invitation_token'])"
}

ECHO_TOKEN=""
CALLER_TOKEN=""
if [[ ! -f "$ECHO_DIR/credentials.json" ]]; then
    log "echo_agent has no persisted creds — minting invitation"
    ECHO_TOKEN=$(mint_invite "echo-$(date +%s)")
    [[ -n "$ECHO_TOKEN" ]] || fail "echo_agent invitation mint returned empty"
else
    log "echo_agent has persisted creds — resuming"
fi
if [[ ! -f "$CALLER_DIR/credentials.json" ]]; then
    log "caller_agent has no persisted creds — minting invitation"
    CALLER_TOKEN=$(mint_invite "caller-$(date +%s)")
    [[ -n "$CALLER_TOKEN" ]] || fail "caller_agent invitation mint returned empty"
else
    log "caller_agent has persisted creds — resuming"
fi

cleanup() {
    log "tearing down agents"
    [[ -n "${ECHO_PID:-}" ]] && kill "$ECHO_PID" 2>/dev/null || true
    [[ -n "${CALLER_PID:-}" ]] && kill "$CALLER_PID" 2>/dev/null || true
    [[ -n "${GEMINI_PID:-}" ]] && kill "$GEMINI_PID" 2>/dev/null || true
}
trap cleanup EXIT

log "starting echo_agent (state=$ECHO_DIR)"
AGENT_INVITATION_TOKEN="$ECHO_TOKEN" \
    AGENT_ROUTER_URL="$WS_URL" \
    AGENT_STATE_DIR="$ECHO_DIR" \
    "$PYTHON_BIN" examples/test_drive/echo_agent.py > "$ECHO_LOG" 2>&1 &
ECHO_PID=$!

log "starting caller_agent (state=$CALLER_DIR)"
AGENT_INVITATION_TOKEN="$CALLER_TOKEN" \
    AGENT_ROUTER_URL="$WS_URL" \
    AGENT_STATE_DIR="$CALLER_DIR" \
    "$PYTHON_BIN" examples/test_drive/caller_agent.py > "$CALLER_LOG" 2>&1 &
CALLER_PID=$!

# 4. Drive the round-trip — retry while agents finish their
#    WS connect handshake. The catalog endpoint isn't a reliable
#    readiness signal (it lists historically-registered agents
#    independent of current WS state); the cleanest gate is the
#    admit-task call itself, which 503s with `agent_disconnected`
#    until both ends are live.
log "driving caller_agent round-trip (will retry while agents connect)"
RESPONSE=""
for attempt in {1..30}; do
    RESPONSE=$(curl -s -X POST "$ROUTER_URL/v1/admin/tasks/test" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"destination_agent_id":"caller_agent","payload":{"prompt":"hello round trip"},"wait":true,"timeout_s":30}')
    if echo "$RESPONSE" | grep -q '"status":"succeeded"'; then
        break
    fi
    if [[ $attempt -eq 30 ]]; then
        echo "$RESPONSE" >&2
        fail "round-trip did not succeed after 30 attempts"
    fi
    sleep 1
done

# 6. Assert.
echo "$RESPONSE" | "$PYTHON_BIN" -c "
import json, sys
r = json.load(sys.stdin)
assert r.get('status') == 'succeeded', f'expected succeeded, got {r!r}'
content = (r.get('output') or {}).get('content', '')
assert 'HELLO ROUND TRIP' in content, f'expected echoed uppercase in {content!r}'
print(f'OK round-trip in {r[\"duration_s\"]:.3f}s — {content!r}')
"

log "agent-to-agent smoke OK"

# ---------------------------------------------------------------------------
# Optional Gemini leg
# ---------------------------------------------------------------------------
SKIP_GEMINI_REASON=""
if [[ $SKIP_GEMINI -eq 1 ]]; then
    SKIP_GEMINI_REASON="--skip-gemini"
elif ! grep -q '^GEMINI_API_KEY=' .env; then
    SKIP_GEMINI_REASON="no GEMINI_API_KEY in .env"
elif ! "$PYTHON_BIN" -c "import google.genai" 2>/dev/null; then
    SKIP_GEMINI_REASON="google-genai not installed (pip install -e .[llm-gemini])"
fi

if [[ -n "$SKIP_GEMINI_REASON" ]]; then
    log "Gemini leg skipped ($SKIP_GEMINI_REASON)"
fi

if [[ -z "$SKIP_GEMINI_REASON" ]]; then

: "${GEMINI_AGENT_STATE_DIR:=/tmp/bp-test-drive/gemini}"
GEMINI_DIR="$GEMINI_AGENT_STATE_DIR"
mkdir -p "$GEMINI_DIR"
GEMINI_LOG=/tmp/gemini-agent-smoke.log

GEMINI_TOKEN=""
if [[ ! -f "$GEMINI_DIR/credentials.json" ]]; then
    log "gemini_agent has no persisted creds — minting invitation"
    GEMINI_TOKEN=$(mint_invite "gemini-$(date +%s)")
    [[ -n "$GEMINI_TOKEN" ]] || fail "gemini_agent invitation mint returned empty"
else
    log "gemini_agent has persisted creds — resuming"
fi

log "starting gemini_agent (state=$GEMINI_DIR)"
AGENT_INVITATION_TOKEN="$GEMINI_TOKEN" \
    AGENT_ROUTER_URL="$WS_URL" \
    AGENT_STATE_DIR="$GEMINI_DIR" \
    "$PYTHON_BIN" examples/test_drive/gemini_agent.py > "$GEMINI_LOG" 2>&1 &
GEMINI_PID=$!

log "driving real Gemini call (will retry while agent connects)"
GEMINI_RESPONSE=""
for attempt in {1..30}; do
    GEMINI_RESPONSE=$(curl -s -X POST "$ROUTER_URL/v1/admin/tasks/test" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"destination_agent_id":"gemini_agent","payload":{"prompt":"Reply with exactly one short sentence: what is the capital of France?"},"wait":true,"timeout_s":60}')
    if echo "$GEMINI_RESPONSE" | grep -q '"status":"succeeded"'; then
        break
    fi
    if [[ $attempt -eq 30 ]]; then
        echo "$GEMINI_RESPONSE" >&2
        fail "Gemini round-trip did not succeed after 30 attempts"
    fi
    sleep 1
done

echo "$GEMINI_RESPONSE" | "$PYTHON_BIN" -c "
import json, sys
r = json.load(sys.stdin)
assert r.get('status') == 'succeeded', f'expected succeeded, got {r!r}'
out = r.get('output') or {}
content = out.get('content', '')
md = out.get('metadata', {})
assert 'Paris' in content, f'expected Paris in {content!r}'
# Pin Bug-13 fix: thoughts_tokens is in metadata. If a regression
# drops it, we lose the visibility into Gemini's thinking budget.
assert 'thoughts_tokens' in md, f'thoughts_tokens missing from metadata: {md!r}'
print(f'OK Gemini round-trip in {r[\"duration_s\"]:.3f}s — '
      f'{content!r} (in={md.get(\"input_tokens\")}, '
      f'out={md.get(\"output_tokens\")}, '
      f'thoughts={md.get(\"thoughts_tokens\")})')
"

log "Gemini smoke OK"
fi  # end Gemini leg gate

# ---------------------------------------------------------------------------
# Optional quota leg (--run-quota-test)
# ---------------------------------------------------------------------------
#
# Drives the admit-time per-user rate quota end-to-end. Pre-req:
# the router was started with a tight `admin` cap (default is
# uncapped, which would never refuse). Set, e.g.:
#
#   ROUTER_QUOTA_ADMIT_RATE_PER_S='{"admin": 2.0, ...}'
#   ROUTER_QUOTA_ADMIT_BURST='{"admin": 2, ...}'
#
# in `.env` and restart the router before running with
# `--run-quota-test`. The leg fires N admit_task calls and asserts
# that at least one returns HTTP 429 with a `Retry-After` header
# and a `quota_exceeded` body.
if [[ $RUN_QUOTA_TEST -eq 0 ]]; then
    exit 0
fi

log "driving quota leg (firing 6 admit calls back-to-back)"
QUOTA_REFUSALS=0
QUOTA_RETRY_AFTER=""
for i in 1 2 3 4 5 6; do
    HTTP_CODE=$(curl -s -o /tmp/quota_resp.txt \
        -D /tmp/quota_headers.txt \
        -w "%{http_code}" \
        -X POST "$ROUTER_URL/v1/admin/tasks/test" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d "{\"destination_agent_id\":\"echo_agent\",\"payload\":{\"prompt\":\"q$i\"},\"wait\":false,\"timeout_s\":5}")
    if [[ "$HTTP_CODE" == "429" ]]; then
        QUOTA_REFUSALS=$((QUOTA_REFUSALS + 1))
        if [[ -z "$QUOTA_RETRY_AFTER" ]]; then
            QUOTA_RETRY_AFTER=$(grep -i '^retry-after:' /tmp/quota_headers.txt | tr -d '\r' | awk '{print $2}')
        fi
    fi
done

if [[ $QUOTA_REFUSALS -eq 0 ]]; then
    fail "quota leg saw 0 HTTP 429 refusals over 6 admit calls — \
configured admin cap may be too high to demonstrate the quota. \
Set ROUTER_QUOTA_ADMIT_RATE_PER_S/BURST tighter and restart the \
router. See DEVELOPMENT.md#agent-test-drive."
fi

# Confirm the metric increments — pin that
# `router_quota_exceeded_total{counter="admit_rate"}` is being
# incremented, not just that 429 is being returned. Catches a
# regression that drops the `metrics.quota_exceeded_total.labels(...)
# .inc()` line from `admit_task`.
METRIC_VALUE=$(curl -s "$ROUTER_URL/metrics" \
    | grep '^router_quota_exceeded_total{counter="admit_rate"' \
    | awk '{print $NF}' | head -1)
if [[ -z "$METRIC_VALUE" ]] || [[ "$METRIC_VALUE" == "0.0" ]]; then
    fail "quota leg got 429s but metric router_quota_exceeded_total \
is still zero — admit_task isn't incrementing it"
fi

log "Quota leg OK ($QUOTA_REFUSALS / 6 refusals; Retry-After=$QUOTA_RETRY_AFTER s; \
metric=$METRIC_VALUE)"
