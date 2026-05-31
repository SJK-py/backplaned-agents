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
DEFAULT_WEBAPP_HTTPS_PORT="8443"   # webapp's port identity for bare-IP LAN

# Set to 1 once invitation tokens have been minted in THIS invocation (by
# build_env). start/restart then skip their own refresh so tokens are
# generated exactly once per launch — never twice (see refresh_invitations).
TOKENS_MINTED=0

# URL-/DSN-/JSON-safe secret of $1 chars (alphanumeric only).
gen() { openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | head -c "${1:-44}"; }

# Read a single VAR's value from an existing env file (empty if absent/missing).
# Used to CARRY FORWARD secrets that get baked into persistent volumes on first
# use (PG_PASSWORD → pg_data, S3 keys → seaweedfs_data): Postgres/SeaweedFS only honor
# them when the volume is EMPTY (first init), so regenerating them on an env
# rebuild while the volume survives a non-reset relaunch would break auth
# (migrate: password authentication failed → exit 1). Caller falls back to
# `gen` only when this returns empty (first build, or after `reset`/down -v).
env_val() {  # env_val <VAR> [file]
    local f="${2:-$OUT}"
    [[ -f "$f" ]] || return 0
    sed -n "s/^$1=//p" "$f" | tail -n1
}

# Keep a volume-baked secret: reuse the existing file's value if present, else
# mint a fresh one. Echoes the value (for capture).
keep_or_gen() {  # keep_or_gen <VAR> <gen-length>
    local cur; cur="$(env_val "$1")"
    if [[ -n "$cur" ]]; then printf '%s' "$cur"; else gen "$2"; fi
}

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
    # Edge / reverse proxy (Caddy). PUBLIC_DOMAIN is the router + admin host;
    # WEBAPP_DOMAIN is the browser-channel host (separate IDENTITY because the
    # webapp serves from / and would collide with the router's /admin).
    #   - 'localhost'  → local box only, Caddy serves a local self-signed cert.
    #   - a real domain that resolves to this host → Caddy auto-provisions a
    #     public Let's Encrypt cert (ports 80/443 must be reachable).
    #   - a LAN name (e.g. bp.lan, with `app.bp.lan` resolvable too) → LAN
    #     access; Caddy internal-CA TLS (browsers warn until you trust it).
    #   - a bare IP (e.g. 192.168.1.50) → no DNS for `app.<ip>`, so the webapp
    #     gets a PORT identity instead: WEBAPP_DOMAIN=<ip>:$WEBAPP_HTTPS_PORT.
    # See docs/deployment.md "Edge / reverse proxy (Caddy)".
    echo
    echo "Edge / reverse proxy (Caddy) hostnames:"
    echo "  localhost                → this machine only (local self-signed TLS)"
    echo "  a public domain          → auto-TLS via Let's Encrypt (must resolve here, 80/443 open)"
    echo "  a LAN name (bp.lan)      → LAN access; Caddy internal-CA TLS (browser trust needed)"
    echo "  a bare IP (192.168.x.x)  → LAN access; webapp moves to a separate HTTPS port"
    echo "  (TLS upstream? a later prompt switches Caddy to plain HTTP at the origin.)"
    ask PUBLIC_DOMAIN "Public domain — router + admin UI host" "localhost"
    # Webapp identity. A bare IP can't host `app.<ip>` (no DNS), so default it
    # to a PORT on the same IP; anything else gets the `app.` subdomain.
    local IS_BARE_IP=0
    if [[ "$PUBLIC_DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        IS_BARE_IP=1
        WEBAPP_HTTPS_PORT="$DEFAULT_WEBAPP_HTTPS_PORT"
        ask WEBAPP_DOMAIN "Webapp (browser channel) host:port" "${PUBLIC_DOMAIN}:${WEBAPP_HTTPS_PORT}"
    else
        ask WEBAPP_DOMAIN "Webapp (browser channel) host" "app.${PUBLIC_DOMAIN}"
    fi
    # If the webapp identity carries a non-443 port, publish that port.
    WEBAPP_HTTPS_PORT="$(printf '%s' "$WEBAPP_DOMAIN" | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')"
    WEBAPP_HTTPS_PORT="${WEBAPP_HTTPS_PORT:-443}"

    # Edge scheme. Default: Caddy terminates TLS (https). Answer y when TLS is
    # handled UPSTREAM — Cloudflare Tunnel, an external load balancer, ngrok —
    # so Caddy serves plain HTTP at the origin (automatic HTTPS off). The
    # public scheme stays https, so secrets/cookies are unaffected.
    echo
    echo "Is TLS terminated UPSTREAM (Cloudflare Tunnel / external LB)? If yes,"
    echo "Caddy serves plain HTTP at the origin instead of provisioning certs."
    ask UPSTREAM_TLS "TLS terminated upstream? [y/N]" "n"
    if yesish "$UPSTREAM_TLS"; then EDGE_SCHEME=http; else EDGE_SCHEME=https; fi

    # Bare-IP https needs two Caddy nudges (both no-ops for a domain/localhost):
    #
    # EDGE_TLS="tls internal" — pin the internal CA. A PUBLIC IP otherwise
    #   "qualifies" for a public cert, so Caddy asks Let's Encrypt, which REFUSES
    #   to issue for a bare IP, leaving :443 certless. (A private IP already
    #   auto-uses the internal CA, so this is belt-and-suspenders there.)
    #
    # EDGE_GLOBAL="default_sni <ip>" — name the cert to serve when the client
    #   sends NO SNI. A browser hitting https://<ip> never sends SNI (it only
    #   carries hostnames, not IP literals), and behind Docker's port publishing
    #   the local address Caddy sees is the CONTAINER IP, not the cert's IP SAN —
    #   so without default_sni Caddy can't match a cert and aborts the handshake
    #   with `tls: internal error` (→ browser ERR_SSL_PROTOCOL_ERROR). This is
    #   THE fix for the "https on a bare IP just SSL-errors" symptom.
    #
    # Both injected into deploy/Caddyfile (EDGE_TLS per-site, EDGE_GLOBAL into
    # the global-options block). Empty otherwise: a real domain keeps auto
    # Let's Encrypt + SNI routing; EDGE_SCHEME=http (TLS upstream) serves plain
    # HTTP with no cert at all.
    local EDGE_TLS="" EDGE_GLOBAL=""
    if [[ "$EDGE_SCHEME" == "https" && $IS_BARE_IP -eq 1 ]]; then
        EDGE_TLS="tls internal"
        EDGE_GLOBAL="default_sni $PUBLIC_DOMAIN"
    fi

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
    #
    # EMBEDDING_DIM must equal the VECTOR LENGTH the embedding preset emits — it
    # becomes SUITE_EMBEDDING_DIM, which the knowledge_base/memory agents bake
    # into their LanceDB table schema (pa.list_(float32, dim)) on FIRST write.
    # A wrong value makes every KB/memory write fail, and the dimension is
    # frozen once the lancedb_data volume exists (a later change needs a reset).
    # All presets below resolve to 1536: Gemini `default_embedding` pins
    # output_dimensionality=1536 (presets_catalog.jsonc); OpenAI
    # text-embedding-3-small is natively 1536. Keep this in sync with the preset
    # if you repoint it (e.g. text-embedding-3-large → 3072).
    case "$PROVIDER" in
        anthropic)
            KEY_VAR=ANTHROPIC_API_KEY
            PRESET_LITE=claude-haiku; PRESET_BALANCED=claude; PRESET_PRO=claude-opus
            PRESET_EMBEDDING=default_embedding; EMBEDDING_DIM=1536 ;;   # Anthropic has no embeddings → Gemini
        gemini)
            KEY_VAR=GEMINI_API_KEY
            PRESET_LITE=gemini-lite; PRESET_BALANCED=gemini; PRESET_PRO=gemini-pro
            PRESET_EMBEDDING=default_embedding; EMBEDDING_DIM=1536 ;;
        openai)
            KEY_VAR=OPENAI_API_KEY
            PRESET_LITE=gpt-nano; PRESET_BALANCED=gpt; PRESET_PRO=gpt-pro
            PRESET_EMBEDDING=text-embedding-3-small; EMBEDDING_DIM=1536 ;;
        custom)
            KEY_VAR=""
            PRESET_LITE=lite; PRESET_BALANCED=default; PRESET_PRO=pro
            PRESET_EMBEDDING=default_embedding; EMBEDDING_DIM=1536 ;;
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

    # Volume-baked secrets: capture BEFORE the `{ } > "$OUT"` block truncates
    # the file. Reuse the existing value when a previous $OUT is present — so a
    # rebuild over SURVIVING data volumes keeps the secret the volume was
    # initialised with (else Postgres/SeaweedFS keep the OLD secret and the NEW one
    # in .env fails auth → migrate exit 1). Mint fresh only when $OUT is absent
    # (true first build). After `reset` (down -v) the volumes are gone but $OUT
    # remains, so the reused secret simply re-initialises the now-empty volume —
    # consistent either way. A reused-vs-minted count is reported after write.
    local PG_PW SUITE_DB_PW S3_AK S3_SK REUSED=0 MINTED=0
    PG_PW="$(keep_or_gen PG_PASSWORD 32)"
    SUITE_DB_PW="$(keep_or_gen SUITE_DB_PASSWORD 32)"
    S3_AK="$(keep_or_gen S3_ACCESS_KEY 20)"
    S3_SK="$(keep_or_gen S3_SECRET_KEY 40)"
    local _v
    for _v in PG_PASSWORD SUITE_DB_PASSWORD S3_ACCESS_KEY S3_SECRET_KEY; do
        if [[ -n "$(env_val "$_v")" ]]; then REUSED=$((REUSED+1)); else MINTED=$((MINTED+1)); fi
    done

    {
        echo "# Generated by scripts/prod.sh on $(date -u +%FT%TZ)."
        echo "# Compose interpolation vars for docker-compose.prod.yml. KEEP SECRET."
        echo
        echo "# --- Edge / reverse proxy (Caddy) ---"
        echo "# PUBLIC_DOMAIN: router + admin UI host. WEBAPP_DOMAIN: browser"
        echo "# channel host (separate — webapp serves from /). 'localhost' or a"
        echo "# *.localhost / LAN name → Caddy internal-CA TLS; a public domain"
        echo "# resolving here → auto Let's Encrypt. A bare IP can't host"
        echo "# app.<ip>, so WEBAPP_DOMAIN gets a port (<ip>:WEBAPP_HTTPS_PORT),"
        echo "# which compose publishes. See docs/deployment.md."
        echo "PUBLIC_DOMAIN=$PUBLIC_DOMAIN"
        echo "WEBAPP_DOMAIN=$WEBAPP_DOMAIN"
        if [[ "$WEBAPP_HTTPS_PORT" != "443" ]]; then
            echo "WEBAPP_HTTPS_PORT=$WEBAPP_HTTPS_PORT"
        fi
        if [[ "$EDGE_SCHEME" != "https" ]]; then
            echo "# TLS terminated upstream — Caddy serves plain HTTP at the origin."
            echo "EDGE_SCHEME=$EDGE_SCHEME"
        fi
        if [[ -n "$EDGE_TLS" ]]; then
            echo "# Bare-IP https (see deploy/Caddyfile). EDGE_TLS pins Caddy's"
            echo "# internal CA — Let's Encrypt won't issue for an IP, so the"
            echo "# default issuer leaves :443 certless. EDGE_GLOBAL sets default_sni:"
            echo "# a client hitting https://<ip> sends no SNI, and behind Docker NAT"
            echo "# Caddy can't match a cert without it → tls handshake 'internal"
            echo "# error' (browser ERR_SSL_PROTOCOL_ERROR)."
            echo "EDGE_TLS=$EDGE_TLS"
            echo "EDGE_GLOBAL=$EDGE_GLOBAL"
        fi
        echo
        echo "# --- Postgres (router + suite DBs share this server; suite connects as postgres) ---"
        echo "PG_USER=postgres"
        echo "PG_PASSWORD=$PG_PW"
        echo "SUITE_DB_PASSWORD=$SUITE_DB_PW"
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
        echo "# --- Object store (SeaweedFS / S3) ---"
        echo "S3_BUCKET=bp-files"
        echo "S3_ACCESS_KEY=$S3_AK"
        echo "S3_SECRET_KEY=$S3_SK"
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
        echo "# Vector length the embedding preset emits — baked into the KB/memory"
        echo "# LanceDB schema on first write and frozen for the life of the"
        echo "# lancedb_data volume. MUST match $PRESET_EMBEDDING; change it (and reset"
        echo "# the volume) only if you repoint the embedding preset to a different dim."
        echo "SUITE_EMBEDDING_DIM=$EMBEDDING_DIM"
        echo
        echo "# --- Channel ---"
        echo "SUITE_TELEGRAM_BOT_TOKEN=$TELEGRAM"
        echo
        echo "# --- Web search (research agent) ---"
        if [[ "$SEARXNG_URL" == "$BUNDLED_SEARXNG_URL" ]]; then
            echo "# bundled SearXNG — prod.sh auto-adds '--profile search' on start/restart."
            echo "# deploy/searxng/settings.yml enables the json format + GET method the"
            echo "# research agent needs (the stock image defaults to html/POST → 403)."
            echo "# SEARXNG_SECRET overrides the instance secret_key (compose passes it"
            echo "# through). Only emitted for the bundled instance; an external SearXNG"
            echo "# is the operator's to configure."
            echo "SEARXNG_SECRET=$(gen 32)"
        fi
        echo "SUITE_SEARXNG_URL=$SEARXNG_URL"
    } > "$OUT"
    chmod 600 "$OUT"
    # Invitation tokens are the ONE thing not written above: they're single-use
    # and minted fresh on every start/restart by refresh_invitations(), which is
    # the single source. Seed them once here too, so a build-then-stop/exit (no
    # compose action) still leaves a complete, registerable env file.
    refresh_invitations

    echo
    echo "Wrote $OUT (chmod 600)."
    echo "  edge: admin/router=$EDGE_SCHEME://$PUBLIC_DOMAIN  webapp=$EDGE_SCHEME://$WEBAPP_DOMAIN"
    [[ "$WEBAPP_HTTPS_PORT" != "443" ]] && \
        echo "        (webapp on a separate port $WEBAPP_HTTPS_PORT — compose publishes it; open it on any firewall)"
    if [[ "$EDGE_SCHEME" == "http" ]]; then
        echo "        (Caddy serves plain HTTP — TLS is terminated upstream; clients still use https)"
    fi
    echo "  provider: $PROVIDER   tiers: lite=$PRESET_LITE balanced=$PRESET_BALANCED pro=$PRESET_PRO"
    [[ $GENERATED_PW -eq 1 ]] && echo "  generated admin password: $ADMIN_PW   (save it!)"
    if [[ $REUSED -gt 0 ]]; then
        echo "  reused $REUSED volume-baked secret(s) from the previous $OUT"
        echo "        (PG/SUITE DB passwords, S3 keys — kept so they still match the"
        echo "        surviving data volumes; a fresh value would fail auth. Use"
        echo "        'reset' to wipe volumes AND mint new ones together.)"
    fi
    # Internal-CA TLS warning only applies when CADDY is the TLS terminator.
    if [[ "$EDGE_SCHEME" == "https" ]]; then
        case "$PUBLIC_DOMAIN" in
            localhost|*.localhost|127.*|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
                echo "  NOTE: '$PUBLIC_DOMAIN' → Caddy serves internal-CA / self-signed TLS"
                echo "        (browsers warn until you trust Caddy's root CA — see"
                echo "        docs/deployment.md). A public domain resolving here gets"
                echo "        automatic Let's Encrypt TLS instead." ;;
        esac
        if [[ -n "$EDGE_TLS" ]]; then
            echo "  NOTE: bare IP '$PUBLIC_DOMAIN' over https → pinned Caddy's internal"
            echo "        CA + default_sni (EDGE_TLS / EDGE_GLOBAL). default_sni is what"
            echo "        lets the handshake succeed: an IP client sends no SNI, and"
            echo "        Caddy can't match a cert behind Docker NAT without it (the"
            echo "        'SSL protocol error' symptom). Browsers still warn on the"
            echo "        self-signed cert until you trust Caddy's root CA — for"
            echo "        warning-free TLS use a domain/LAN name that resolves here."
        fi
    fi
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

_INVITE_HEADER="# --- Agent invitation tokens (registered by the compose 'bootstrap' service) ---"

# Invitation tokens are SINGLE-USE: each launch consumes them at onboard, so
# the next launch needs FRESH ones (the router now allows idempotent
# re-onboard with a *new* invitation — see bp_router/api/onboard.py). Rewrite
# just the `*_INVITATION` lines in the existing env file (strip the old block,
# re-append a freshly-generated one) so start/restart always ships unused
# tokens — without rebuilding the whole env. The agents read these via compose
# interpolation and `bootstrap` registers them, so both see the same value.
refresh_invitations() {
    local tmp
    tmp="$(mktemp)"
    # Drop the invitation header (fixed-string whole-line match — it contains
    # `()`/`'` regex metachars) AND every *_INVITATION= line, then re-append a
    # fresh block. Everything else is kept byte-for-byte.
    grep -vxF "$_INVITE_HEADER" "$OUT" \
        | grep -vE "^[A-Z_]*_INVITATION=" > "$tmp" || true
    # Trim trailing blank lines so they don't accumulate across launches.
    sed -i -e :a -e '/^\n*$/{$d;N;ba}' "$tmp" 2>/dev/null || true
    {
        echo
        echo "$_INVITE_HEADER"
        scripts/register-invitations.sh --gen
    } >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$OUT"
    TOKENS_MINTED=1
    echo "  refreshed agent invitation tokens (single-use — fresh per launch)"
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
echo "  1) start        (docker compose up -d)"
echo "  2) restart      (recreate containers)"
echo "  3) stop         (docker compose down — keeps data volumes)"
echo "  4) reset        (down -v — DELETES the DB + all data volumes; full fresh start)"
echo "  5) exit         (do nothing)"
ask ACTION "Choose 1-5" "1"

# start / restart can optionally rebuild images from this source first.
maybe_build_flag() {
    ask REBUILD "  Rebuild images from this source first? [y/N]" "n"
    yesish "$REBUILD" && printf '%s' "--build"
    return 0
}

case "${ACTION,,}" in
    1|start)
        BUILD_FLAG="$(maybe_build_flag)"
        [[ "$TOKENS_MINTED" == "1" ]] || refresh_invitations
        echo; echo "+ docker compose ${CARGS[*]} up -d ${BUILD_FLAG}"
        exec docker compose "${CARGS[@]}" up -d ${BUILD_FLAG:+$BUILD_FLAG} ;;
    2|restart)
        BUILD_FLAG="$(maybe_build_flag)"
        [[ "$TOKENS_MINTED" == "1" ]] || refresh_invitations
        # `up -d --force-recreate` recreates ALL containers (a true restart);
        # --build first when rebuilding images from this source.
        echo; echo "+ docker compose ${CARGS[*]} up -d --force-recreate ${BUILD_FLAG}"
        exec docker compose "${CARGS[@]}" up -d --force-recreate ${BUILD_FLAG:+$BUILD_FLAG} ;;
    3|stop)
        echo; echo "+ docker compose ${CARGS[*]} down"
        exec docker compose "${CARGS[@]}" down ;;
    4|reset)
        # `down -v` ALSO removes named volumes — the Postgres DB (agent rows,
        # users, tasks), Redis, SeaweedFS blobs, LanceDB, and every agent's
        # persisted credentials — for a genuine clean slate. (Normal
        # start/restart no longer needs this: invitations are refreshed each
        # launch and the router allows idempotent re-onboard, so agents
        # recover across redeploys. Use reset when you want to wipe DATA.)
        # Destructive — confirm.
        echo
        echo "  This DELETES ALL DATA: Postgres (users/tasks/agents), Redis,"
        echo "  object store, LanceDB, and agent credentials. Irreversible."
        ask CONFIRM "  Type 'reset' to confirm" ""
        if [[ "$CONFIRM" == "reset" ]]; then
            echo; echo "+ docker compose ${CARGS[*]} down -v"
            exec docker compose "${CARGS[@]}" down -v
        else
            echo "  aborted — nothing deleted."
        fi ;;
    5|exit|"")
        echo "nothing to do." ;;
    *) echo "invalid choice: $ACTION" >&2; exit 1 ;;
esac
