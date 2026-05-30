#!/usr/bin/env bash
#
# scripts/prod.sh — production launcher for `docker-compose.prod.yml`.
#
# One entry point for the whole prod lifecycle:
#   1. Optionally (re)build the env file deploy/.env.prod — prompts for the
#      handful of values only you can provide (domain, admin email/password,
#      LLM provider + key, Telegram token, web-search backend) and
#      RANDOM-GENERATES the rest (Postgres password, JWT / session / metrics
#      secrets, object-store keys, one invitation token per agent). Skip this
#      to reuse the existing env file.
#   2. Run a compose action against that env file: start / restart / stop.
#
# The `search` compose profile (the bundled SearXNG) is auto-added to start /
# restart whenever the env file's SUITE_SEARXNG_URL is the bundled
# http://searxng:8080 — no flag to remember.
#
# Usage:
#   scripts/prod.sh                  # interactive: build env? -> action
#   OUT=/path/to/.env scripts/prod.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${OUT:-deploy/.env.prod}"
COMPOSE_FILE="docker-compose.prod.yml"
BUNDLED_SEARXNG_URL="http://searxng:8080"

# URL-/DSN-/JSON-safe secret of $1 chars (alphanumeric only).
gen() { openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | head -c "${1:-44}"; }

ask() {  # ask <var> <prompt> <default>
    local var="$1" prompt="$2" def="${3:-}" reply
    if [[ -n "$def" ]]; then read -rp "$prompt [$def]: " reply || true
    else read -rp "$prompt: " reply || true; fi
    printf -v "$var" '%s' "${reply:-$def}"
}

yesish() { [[ "${1,,}" =~ ^(y|yes)$ ]]; }

# ---------------------------------------------------------------------------
# Step 1 — build the env file (first deploy, or to change vars)
# ---------------------------------------------------------------------------
build_env() {
    echo "Generating $OUT — answer a few prompts; the rest is auto-generated."
    ask PUBLIC_DOMAIN "Public domain (TLS via Caddy; 'localhost' for a local trial)" "localhost"
    ask ADMIN_EMAIL "Bootstrap admin email" "admin@example.com"
    read -rsp "Bootstrap admin password (blank = generate one): " ADMIN_PW || true; echo

    # --- LLM provider -> API key -> per-tier preset aliases -----------------
    # Pick the provider for the default chat tiers, capture its key, and wire
    # the suite's lite/balanced/pro (+ embedding) defaults to that provider's
    # seeded preset aliases (see bp_router/llm/presets_catalog.jsonc).
    echo
    echo "LLM provider for the default chat tiers:"
    echo "  1) Anthropic (Claude)"
    echo "  2) Gemini (Google)"
    echo "  3) OpenAI (GPT)"
    echo "  4) Custom — wire the generic lite/default/pro slots; set models + keys"
    echo "             later via the admin webUI"
    ask PROVIDER_CHOICE "Choose 1-4" "2"
    case "${PROVIDER_CHOICE,,}" in
        1|anthropic) PROVIDER=anthropic ;;
        2|gemini)    PROVIDER=gemini ;;
        3|openai)    PROVIDER=openai ;;
        4|custom)    PROVIDER=custom ;;
        *) echo "invalid provider choice: $PROVIDER_CHOICE" >&2; exit 1 ;;
    esac

    # provider -> (key env var, lite, balanced, pro, embedding) preset aliases.
    # `custom` wires the generic tier slots (lite / default / pro) the operator
    # repoints via the admin webUI, and asks for no key (configure it there).
    case "$PROVIDER" in
        anthropic)
            KEY_VAR=ANTHROPIC_API_KEY
            PRESET_LITE=claude-haiku; PRESET_BALANCED=claude; PRESET_PRO=claude-opus
            PRESET_EMBEDDING=default_embedding ;;   # Anthropic has no embeddings
        gemini)
            KEY_VAR=GEMINI_API_KEY
            PRESET_LITE=gemini-lite; PRESET_BALANCED=gemini; PRESET_PRO=gemini-pro
            PRESET_EMBEDDING=default_embedding ;;
        openai)
            KEY_VAR=OPENAI_API_KEY
            PRESET_LITE=gpt-nano; PRESET_BALANCED=gpt; PRESET_PRO=gpt-pro
            PRESET_EMBEDDING=text-embedding-3-small ;;
        custom)
            KEY_VAR=""
            PRESET_LITE=lite; PRESET_BALANCED=default; PRESET_PRO=pro
            PRESET_EMBEDDING=default_embedding ;;
    esac

    if [[ -n "$KEY_VAR" ]]; then
        ask PROVIDER_KEY "$PROVIDER API key (resolves env://$KEY_VAR for the presets)" ""
    else
        PROVIDER_KEY=""
    fi
    ask TELEGRAM "Telegram bot token (from @BotFather)" ""

    # --- Web search backend (research agent) --------------------------------
    # 1) bundled SearXNG (URL -> http://searxng:8080; start/restart auto-adds
    #    the compose `search` profile), 2) external SearXNG (ask for the URL),
    #    3) none — research runs without web search (SUITE_SEARXNG_URL empty).
    echo
    echo "Web search backend for the research agent:"
    echo "  1) Deploy SearXNG with compose (bundled 'search' profile)"
    echo "  2) Use an external SearXNG (you provide the URL)"
    echo "  3) Don't configure now (research runs without web search)"
    ask SEARXNG_CHOICE "Choose 1-3" "1"
    case "${SEARXNG_CHOICE,,}" in
        1|compose|bundled)
            SEARXNG_URL="$BUNDLED_SEARXNG_URL" ;;
        2|external)
            ask SEARXNG_URL "External SearXNG base URL (e.g. https://searx.example.com)" ""
            [[ -z "$SEARXNG_URL" ]] && echo "  WARNING: empty URL — research will have no web search." ;;
        3|none|"")
            SEARXNG_URL="" ;;
        *) echo "invalid search choice: $SEARXNG_CHOICE" >&2; exit 1 ;;
    esac

    local GENERATED_PW=0
    if [[ -z "$ADMIN_PW" ]]; then ADMIN_PW="$(gen 20)"; GENERATED_PW=1; fi

    {
        echo "# Generated by scripts/prod.sh on $(date -u +%FT%TZ)."
        echo "# Compose interpolation vars for docker-compose.prod.yml. KEEP SECRET."
        echo
        echo "PUBLIC_DOMAIN=$PUBLIC_DOMAIN"
        echo
        echo "# --- Postgres (router + suite DBs share this server; suite connects as postgres) ---"
        echo "PG_USER=postgres"
        echo "PG_PASSWORD=$(gen 32)"
        echo "SUITE_DB_PASSWORD=$(gen 32)"
        echo
        echo "# --- Router secrets ---"
        echo "ROUTER_JWT_SECRET=$(gen 48)"
        echo "ROUTER_ADMIN_SESSION_SECRET=$(gen 48)"
        echo "ROUTER_METRICS_TOKEN=$(gen 32)"
        echo
        echo "# --- Webapp (browser channel) secrets ---"
        echo "WEBAPP_SESSION_SECRET=$(gen 48)"
        echo
        echo "# --- First-boot admin (idempotent seed) ---"
        echo "BOOTSTRAP_ADMIN_EMAIL=$ADMIN_EMAIL"
        echo "BOOTSTRAP_ADMIN_PASSWORD=$ADMIN_PW"
        echo
        echo "# --- Object store (rustfs / S3) ---"
        echo "S3_BUCKET=bp-files"
        echo "S3_ACCESS_KEY=$(gen 20)"
        echo "S3_SECRET_KEY=$(gen 40)"
        echo
        echo "# --- LLM provider ($PROVIDER) + per-tier preset defaults ---"
        if [[ -n "$KEY_VAR" ]]; then
            echo "$KEY_VAR=$PROVIDER_KEY"
        else
            echo "# custom: set provider key(s) + repoint the lite/default/pro"
            echo "# presets via the admin webUI (/admin)."
        fi
        echo "SUITE_DEFAULT_PRESET_LITE=$PRESET_LITE"
        echo "SUITE_DEFAULT_PRESET_BALANCED=$PRESET_BALANCED"
        echo "SUITE_DEFAULT_PRESET_PRO=$PRESET_PRO"
        echo "SUITE_DEFAULT_PRESET_EMBEDDING=$PRESET_EMBEDDING"
        echo
        echo "# --- Channel ---"
        echo "SUITE_TELEGRAM_BOT_TOKEN=$TELEGRAM"
        echo
        echo "# --- Web search (research agent) ---"
        if [[ "$SEARXNG_URL" == "$BUNDLED_SEARXNG_URL" ]]; then
            echo "# bundled SearXNG — prod.sh auto-adds '--profile search' on start/restart"
        fi
        echo "SUITE_SEARXNG_URL=$SEARXNG_URL"
        echo
        echo "# --- Agent invitation tokens (registered by the compose 'bootstrap' service) ---"
        scripts/register-invitations.sh --gen
    } > "$OUT"
    chmod 600 "$OUT"

    echo
    echo "Wrote $OUT (chmod 600)."
    echo "  provider: $PROVIDER   tiers: lite=$PRESET_LITE balanced=$PRESET_BALANCED pro=$PRESET_PRO"
    [[ $GENERATED_PW -eq 1 ]] && echo "  generated admin password: $ADMIN_PW   (save it!)"
    if [[ "$PROVIDER" == "custom" ]]; then
        echo "  NOTE: custom — the lite/default/pro presets are seeded to Gemini"
        echo "        placeholders. Set provider keys and repoint these presets in"
        echo "        the admin webUI (/admin) before they'll work."
    elif [[ -z "$PROVIDER_KEY" ]]; then
        echo "  WARNING: $KEY_VAR is empty — set it before deploying."
    fi
    if [[ "$PROVIDER" == "anthropic" ]]; then
        echo "  NOTE: Anthropic has no embeddings; memory/knowledge-base use"
        echo "        $PRESET_EMBEDDING (Gemini). Set GEMINI_API_KEY too, or change"
        echo "        SUITE_DEFAULT_PRESET_EMBEDDING to an OpenAI embedding preset + key."
    fi
    [[ -z "$TELEGRAM" ]] && echo "  WARNING: SUITE_TELEGRAM_BOT_TOKEN is empty — the chatbot won't poll Telegram."
    [[ "$SEARXNG_URL" == "$BUNDLED_SEARXNG_URL" ]] && echo "  search: bundled SearXNG (compose 'search' profile)"
    return 0  # don't let a false [[ ]] above become build_env's (set -e) exit
}

# ---------------------------------------------------------------------------
# Search-profile detection — read SUITE_SEARXNG_URL from the env FILE (not the
# prompt answer), so the `search` profile is added correctly even when the
# env-build step is skipped and an earlier-built file is reused.
# ---------------------------------------------------------------------------
env_searxng_url() {  # echo SUITE_SEARXNG_URL from $OUT (empty if unset/missing)
    [[ -f "$OUT" ]] || return 0
    sed -n 's/^SUITE_SEARXNG_URL=//p' "$OUT" | tail -n1
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "Build the env file ($OUT)? Needed for a first deploy or to change vars."
echo "  (n reuses the existing file — just run a compose action.)"
ask BUILD_CHOICE "Build env? [y/N]" "n"
case "${BUILD_CHOICE,,}" in
    y|yes)
        if [[ -f "$OUT" ]]; then
            ask OVERWRITE "  $OUT exists — overwrite? [y/N]" "n"
            if yesish "$OVERWRITE"; then build_env; else echo "  keeping existing $OUT."; fi
        else
            build_env
        fi ;;
    n|no|"") : ;;
    *) echo "invalid choice: $BUILD_CHOICE" >&2; exit 1 ;;
esac

# Any compose action needs the env file to exist.
if [[ ! -f "$OUT" ]]; then
    echo "no env file at $OUT — re-run and choose to build it (first deploy)." >&2
    exit 1
fi

# Resolve compose args once (base + auto search profile from the env file).
CARGS=(-f "$COMPOSE_FILE" --env-file "$OUT")
if [[ "$(env_searxng_url)" == "$BUNDLED_SEARXNG_URL" ]]; then
    CARGS+=(--profile search)
    echo "  (search profile auto-enabled: SUITE_SEARXNG_URL=$BUNDLED_SEARXNG_URL)"
fi

echo
echo "Action:"
echo "  1) start     (docker compose up -d)"
echo "  2) restart   (recreate containers)"
echo "  3) stop      (docker compose down)"
echo "  4) exit      (do nothing)"
ask ACTION "Choose 1-4" "1"

# start / restart can optionally rebuild images from this source first.
maybe_build_flag() {
    ask REBUILD "  Rebuild images from this source first? [y/N]" "n"
    yesish "$REBUILD" && printf '%s' "--build"
    return 0
}

case "${ACTION,,}" in
    1|start)
        BUILD_FLAG="$(maybe_build_flag)"
        echo; echo "+ docker compose ${CARGS[*]} up -d ${BUILD_FLAG}"
        exec docker compose "${CARGS[@]}" up -d ${BUILD_FLAG:+$BUILD_FLAG} ;;
    2|restart)
        BUILD_FLAG="$(maybe_build_flag)"
        # `up -d --force-recreate` recreates ALL containers (a true restart);
        # --build first when rebuilding images from this source.
        echo; echo "+ docker compose ${CARGS[*]} up -d --force-recreate ${BUILD_FLAG}"
        exec docker compose "${CARGS[@]}" up -d --force-recreate ${BUILD_FLAG:+$BUILD_FLAG} ;;
    3|stop)
        echo; echo "+ docker compose ${CARGS[*]} down"
        exec docker compose "${CARGS[@]}" down ;;
    4|exit|"")
        echo "nothing to do." ;;
    *) echo "invalid choice: $ACTION" >&2; exit 1 ;;
esac
