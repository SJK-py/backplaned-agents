"""bp_router.settings — Pydantic Settings, validated at startup.

See `docs/backplaned/router/storage.md` §5 for the configuration spec.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Sensible *dev* default for the asyncpg pool ceiling. Also the
# threshold for the staging/prod under-provisioning advisory: an
# operator who never bumped it past the dev default in non-dev is
# almost certainly under-provisioned (one chatty workload, a
# delegation ack-storm, or a fleet reconnect can exhaust 10 conns
# and stall every other router DB op).
_DB_POOL_DEV_DEFAULT = 10


class Settings(BaseSettings):
    """Single source of router configuration.

    Loaded from environment variables prefixed `ROUTER_`. Values may
    reference secrets stored in a backend via the `secret_ref` resolver
    (`bp_router.security.secrets`). Fail-fast on missing/invalid values.
    """

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Database / cache
    # ------------------------------------------------------------------

    # Numeric fields below carry per-field range bounds via
    # `Field(ge=..., le=...)` so misconfigurations fail fast at
    # startup rather than as runtime weirdness — `bind_port=70000`,
    # `db_pool_max_size=0`, `spawn_max_depth=-1` are all silently
    # accepted on a vanilla `Settings()`. Caught loudly here.

    db_url: str
    """Postgres DSN, e.g. `postgresql://user:pass@host:5432/db`."""

    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=_DB_POOL_DEV_DEFAULT, ge=1, le=1000)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1)

    valkey_url: str | None = None
    """Required for multi-worker deployments and any non-dev
    `deployment_env`. Single-worker dev may omit. Without Redis,
    JWT revocation (`bp_router.security.jwt.revoke_jti`) and the
    per-user admit-rate quota (`quota_admit_*`) silently fall
    back to per-process state — correct for single worker, a
    silent security/throughput foot-gun across replicas. The
    `_redis_required_in_non_dev` validator below rejects the
    misconfigured case at startup."""

    # ------------------------------------------------------------------
    # File storage backend
    # ------------------------------------------------------------------

    file_store: Literal["local", "s3", "gcs", "r2"] = "local"
    file_store_options: dict[str, Any] = Field(default_factory=dict)
    """Backend-specific options (bucket, region, endpoint, etc.)."""

    file_download_presigned: bool = False
    """Whether a download may 302-redirect to a backend-direct presigned URL
    (S3/GCS/R2) instead of streaming the bytes through the router.

    OFF by default: every current consumer of GET /v1/files/{id} is a
    SERVER-SIDE, in-cluster caller — the SDK agents, the chatbot's
    `fetch_file`, and the webapp backend's `fetch_file` (which proxies bytes to
    the browser). None of them are on the object store's private network, so a
    presigned URL pointing at e.g. `seaweedfs:8333` is unresolvable to them
    (`ConnectError: Name or service not known`). Streaming through the router
    always works. Enable ONLY if you front the object store with a hostname
    that download clients can actually reach (and want to offload bytes from
    the router). The download hardening — forced `attachment` + MIME downgrade
    — is applied identically either way."""

    file_default_ttl_s: int = Field(default=604_800, ge=1)  # 7 days

    file_upload_token_ttl_s: int = Field(default=300, ge=1, le=3600)
    """TTL of a ws-negotiated one-shot `file-upload` token. Short by
    design — the token is content-bound (sha256+size) and not
    revocation-checked, so the TTL is the only window a leaked
    grant is usable. Just long enough to stream one file."""

    file_fetch_token_ttl_s: int = Field(default=3600, ge=60)
    """TTL of the `file-fetch` key the router mints for a stash-file
    download (`FileResult.fetch_token`). Must comfortably outlive the
    destination handler's execution (it fetches the file mid-handler,
    possibly long after the name was resolved) — hence generous and
    operator-raisable, unlike the upload token."""

    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)  # 25 MiB
    """Hard cap on a single upload to `/v1/files`. Enforced mid-stream
    so an attacker can't trickle gigabytes into RAM. Operators raise
    this for video / model-weight workloads; presigned-URL uploads are
    out of scope and have no router-side limit."""

    llm_attachment_inline_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1024
    )  # 5 MiB
    """Phase-1 cap on a `file_ref` LLM attachment the router will
    base64-inline into a provider request. Resolved bytes over this
    fail the LLM call with a clear error (provider-native upload is
    the Phase-2 path). Conservative vs. the provider request ceilings
    (Gemini ≈20 MiB); the keyed model authorises *access*, this caps
    *volume*."""

    llm_request_max_file_refs: int = Field(default=16, ge=1)
    """Max `file_ref` parts the router resolves per LLM request.
    Bounds the stream/base64 work one request can trigger — keyed
    access does not imply unbounded fan-out."""

    llm_image_max_long_side_px: int = Field(default=1568, ge=0)
    """Downscale an inlined image so its LONGER side is at most this many
    pixels BEFORE base64-feeding it to the provider. Multimodal token cost
    is dimension-based (Anthropic/Gemini), so this is the main lever on the
    per-image token bill. Aspect ratio is preserved and images are only ever
    shrunk (never upscaled). `0` disables resizing. The 1568 default matches
    Anthropic's own internal long-edge downscale, so above it there's
    effectively no quality loss; lower it to trade detail for fewer tokens.
    Best-effort: an undecodable image is fed as-is."""

    llm_image_rescale_source_max_bytes: int = Field(
        default=20 * 1024 * 1024, ge=1024
    )  # 20 MiB
    """When image downscaling is on (`llm_image_max_long_side_px > 0`), the
    router will LOAD an image up to this many source bytes — even past
    `llm_attachment_inline_max_bytes` — so an over-cap image can be RESCUED by
    shrinking it, then re-checked against the inline cap on the resized result.
    Bounds the decode memory one request can trigger (a stash blob is already
    ≤ `max_upload_bytes`; PIL's decompression-bomb guard caps pixels). Should
    be ≥ the inline cap; ignored when resizing is disabled (images then obey
    the inline cap directly, like documents)."""

    llm_preset_catalog_path: str | None = None
    """Path to a JSONC preset catalogue used to seed an empty `llm_presets`
    table (and as the in-memory fallback). Unset → the catalogue bundled with
    the package (`bp_router/llm/presets_catalog.jsonc`). Set it to keep a
    deployment's model list in a commentable file outside the package, so it
    can be updated as models change without editing source. Re-synced into the
    `llm_presets` table on EVERY boot (catalogue-managed rows are upserted,
    rows dropped from the catalogue are pruned) and used as the pre-DB
    fallback; admin-CREATED presets are never touched by the sync."""

    llm_preset_overlay_path: str | None = None
    """Path to an OPTIONAL operator preset overlay (JSONC), merged OVER the base
    catalogue — a custom entry wins on a name collision, new names are added.
    Unlike `llm_preset_catalog_path` (which replaces the catalogue wholesale),
    the overlay keeps the built-ins and only lists overrides/additions. Missing
    file → ignored; malformed → loud at boot. Re-applied on EVERY boot like the
    base catalogue, with the same pinned-field rule: an entry overwrites only
    the fields it LISTS, so listing a field here pins it durably (re-applied
    every boot, overriding admin-UI edits), while a field left OUT stays under
    operator control. Wired in prod to deploy/presets.custom.jsonc."""

    max_request_body_bytes: int = Field(default=64 * 1024, ge=1024)  # 64 KiB
    """Per-request HTTP body cap enforced by `BodySizeLimitMiddleware`
    for every endpoint EXCEPT `/v1/files` (which has its own larger
    streaming cap via `max_upload_bytes`). 64 KiB is generous for the
    admin/auth payloads bundled with the router; operators hosting
    custom endpoints with larger payloads should raise it."""

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    bind_host: str = "0.0.0.0"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    public_url: str
    """External base URL, e.g. `https://router.example.com`."""

    shutdown_grace_s: float = Field(default=25.0, ge=0.0)
    """How long uvicorn waits for in-flight connections to close on SIGTERM
    (``timeout_graceful_shutdown``) before forcing them. Keep this BELOW the
    container's ``stop_grace_period`` (30s in docker-compose.prod.yml) so the
    lifespan drain — closing WS sockets with 1001, cancelling in-flight LLM
    tasks, draining loops — completes before Docker SIGKILLs the container."""

    # ------------------------------------------------------------------
    # Auth / tokens
    # ------------------------------------------------------------------

    jwt_secret: SecretStr
    """Symmetric secret used for HS256 token signing. MUST be at least
    32 bytes (matches SHA-256 output / OWASP guidance for HMAC keys).
    Generate via `openssl rand -base64 32`. The `_jwt_secret_min_length`
    validator below rejects anything shorter at startup so a deploy
    with a placeholder value (`x`, `change-me`) fails loudly rather
    than running with a brute-forceable token signer."""

    jwt_algorithm: Literal["HS256"] = "HS256"
    """Token signing algorithm. EdDSA was previously listed but
    `jwt_secret: SecretStr` only carries a symmetric secret string —
    selecting EdDSA would either crash at startup (PyJWT rejecting
    the non-PEM secret) or sign with a non-key value.
    Restore EdDSA only after wiring proper Ed25519 keypair
    parsing through a separate setting."""
    jwt_key_version: int = Field(default=1, ge=1)
    session_jwt_ttl_s: int = Field(default=900, ge=60)       # 15 min, ≥1m
    refresh_token_ttl_s: int = Field(default=86_400, ge=60)  # 24 h
    agent_token_ttl_s: int = Field(default=86_400, ge=60)    # 24 h

    # ------------------------------------------------------------------
    # First-admin bootstrap
    # ------------------------------------------------------------------
    #
    # `POST /v1/admin/users` requires `Depends(require_admin)` —
    # chicken-and-egg if no admin exists yet. These env vars let
    # operators seed the first admin on initial boot without
    # needing to write Python or hit the DB directly.
    #
    # Idempotent: only creates a new row when no user with this
    # email already exists. Safe to leave set across restarts —
    # a re-boot of an already-seeded deployment is a no-op (logs
    # `bootstrap_admin_exists`).
    #
    # Both must be set together; setting one without the other is
    # rejected at startup so a half-configured deployment fails
    # fast rather than silently skipping the seed.

    bootstrap_admin_email: EmailStr | None = None
    """Email for the first admin user. Paired with
    `bootstrap_admin_password`. Both unset = no bootstrap (the
    operator is using a different mechanism).

    Validated as `EmailStr` (NOT plain `str`) — matches the
    `LoginRequest.email: EmailStr` validator on
    `POST /v1/auth/login`. Without this match, a deployment
    could bootstrap a row with an email that the auth endpoint
    later rejects (e.g. `.test` / `.example` TLDs flagged as
    "special-use" by `email-validator`), leaving an admin user
    in the DB who can never sign in. Test-drive finding."""

    bootstrap_admin_password: SecretStr | None = None
    """Password for the first admin user. Hashed via
    `bp_router.security.passwords.hash_password` before being
    written to `users.auth_secret_hash`."""

    mcp_bridge_secret: SecretStr | None = None
    """Shared secret for the MCP bridge's `service_mcp` principal
    (`ROUTER_MCP_BRIDGE_SECRET`). When set, startup idempotently seeds a fixed
    `level=service` user `service_mcp` and arms this value as its refresh token
    (see `app._bootstrap_mcp_bridge_user`); the bridge presents it to
    `/v1/auth/refresh` for short-lived access tokens, rotating + persisting like
    any other service principal. Unset = the MCP bridge is not provisioned."""

    # ------------------------------------------------------------------
    # Protocol limits / runtime parameters
    # ------------------------------------------------------------------

    heartbeat_interval_ms: int = Field(default=20_000, ge=1000)
    max_payload_bytes: int = Field(default=1_048_576, ge=1024)
    per_socket_outbox_max: int = Field(default=256, ge=1)
    caller_agent_cache_max: int = Field(default=10_000, ge=100)
    """Hard cap on `state.caller_agent_cache` (task_id → caller_agent_id
    lookup avoidance for Progress fan-out). Pre-R8 this was an
    unbounded dict — multi-worker deployments leaked entries
    forever because terminal-task eviction only fires on the same
    worker that admitted the task. R8 adds LRU eviction past the
    cap. At ~50 bytes/entry the default 10k cap means ≤500 KiB
    RSS impact under sustained load."""
    pending_ack_timeout_s: float = Field(default=30.0, gt=0.0)
    default_task_deadline_s: int = Field(default=900, ge=1)
    resume_window_s: int = Field(default=30, ge=0)
    """0 disables the resume window — every disconnect goes straight to
    `fail_inflight_for_agent`. Negative values would underflow the
    parked-entry expiry math, hence `ge=0`."""

    # `spawn_max_depth` has both a logical lower bound (must allow at
    # least one spawn) and an upper bound matching the
    # `_MAX_TASK_TREE_DEPTH = 64` ceiling in `bp_router.db.queries`
    # — anything above that would be silently capped by the recursive
    # CTE.
    spawn_max_depth: int = Field(default=16, ge=1, le=64)
    """Maximum ancestor-chain depth a single `peers.spawn(...)` may
    create. Without a cap, agent A spawning B spawning A → ... is
    unbounded and exhausts the connection pool, the WS outbox, and
    `tasks` rows under a runaway recursion bug or an adversarial
    agent topology. 16 covers every legitimate
    multi-step orchestration we've seen; bump per-deployment if a
    workflow genuinely needs deeper trees. Defense-in-depth bound:
    `_MAX_TASK_TREE_DEPTH = 64` in `bp_router.db.queries` is the
    hard ceiling on the recursive CTE itself, set well above this
    user-facing limit so a legitimate deep chain trips the typed
    `spawn_depth_exceeded` AdmitError before ever exercising the
    CTE bound."""

    task_delegation_max_depth: int = Field(default=32, ge=1, le=128)
    """Maximum number of delegations a single task may go through.
    Without this cap, an LLM agent that misuses `delegate(...)` can
    bounce A→B→A→B... indefinitely; each hop consumes a dispatcher
    PendingMap slot, an ack timeout, a task_event row, and an audit
    row, all keyed off the same `task_id`. The default (32) covers
    every legitimate orchestration; production tasks rarely exceed
    3 hops. The hard cap (128) protects the `LIMIT` on the cycle-
    detection query in `Scope.list_delegation_destinations`."""

    closed_session_retention_days: int = 90
    """How long a **closed** session is kept before the background
    `session_gc_loop` hard-deletes it and its router-side data (tasks, task
    events, file-name directory; `files` rows are detached for the reclaim
    sweep). 0 disables closed-session GC. The suite store (conversation
    history) is reaped separately by the suite's reconcile loop, which only
    purges sessions this GC has already removed."""

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    otel_endpoint: str | None = None
    otel_service_name: str = "bp_router"
    log_level: str = "INFO"

    access_log_quiet_paths: list[str] = [
        "/healthz", "/metrics", "/v1/admin/serviced-sessions",
        "/v1/admin/mcp-servers", "/v1/admin/metrics",
    ]
    """Path prefixes whose *successful* (`<400`) GET access-log lines are
    suppressed, so routine health/poll traffic (e.g. the channel's
    serviced-sessions approval poll, the admin UI's MCP-servers refresh poll,
    or the admin dashboard's metrics-summary auto-refresh) doesn't flood
    `uvicorn.access`. Matched by prefix, so `/v1/admin/mcp-servers` also covers
    the per-server detail GET and `/v1/admin/metrics` covers
    `/v1/admin/metrics/summary`. Errors on these paths still log. Set to `[]`
    to log everything."""

    deployment_env: Literal["dev", "staging", "prod"] = "dev"

    metrics_token: SecretStr | None = None
    """Bearer required to scrape `GET /metrics`. Open when None —
    fine in dev / when IP-restricted by a reverse proxy. Required
    in staging / prod (validator below) because the exposition
    leaks the agent ID list and per-endpoint counters."""

    # ------------------------------------------------------------------
    # Admin web UI (bp_admin)
    # ------------------------------------------------------------------

    serve_admin_ui: bool = True
    """Mount bp_admin at /admin in this router process. Disable to run
    the admin UI as a separate process (set ROUTER_SERVE_ADMIN_UI=false
    and run `bp-admin` with ADMIN_ROUTER_URL pointing here)."""

    admin_session_secret: SecretStr | None = None
    """Signs the admin UI's session cookie. Required when
    serve_admin_ui=True. Generate via `openssl rand -base64 32`."""

    # ------------------------------------------------------------------
    # ACL
    # ------------------------------------------------------------------

    acl_max_tier: int = Field(default=3, ge=0)
    """Highest tier index probed at catalog construction.
    `deployment_levels(max_tier)` = ["admin", "service", "tier0", …, f"tier{max_tier}"].
    Rules referencing tiers above this still validate, they just don't
    surface in the per-entry `callable_user_levels` projection."""

    admin_test_allow_act_as: bool = False
    """When True, `POST /v1/admin/tasks/test` accepts an
    `act_as_user_id` field that lets the admin send a test task on
    behalf of a different user (admit-time `user_level` reflects the
    impersonated user). Off by default — enable per deployment when
    you've thought through audit semantics for impersonated calls."""

    base_url_allowed_hosts: str = ""
    """Comma-separated allowlist of hostnames permitted in preset
    `base_url` fields, regardless of address class. Used to bypass the
    SSRF host blocklist for known private-VPC gateways (LiteLLM,
    Portkey, etc.). Hostnames are matched case-insensitively against
    the URL's hostname; IP literals must be repeated literally.
    Example: `gateway.internal,llm-proxy.svc.cluster.local`."""

    mcp_allowed_launchers: list[str] = ["uvx"]
    """Allowlist of launcher executables a `stdio` MCP server's `command` may
    use (`ROUTER_MCP_ALLOWED_LAUNCHERS`, JSON array). Default: `uvx` only. The
    bridge enforces the same allowlist at spawn time
    (`BP_MCP_BRIDGE_ALLOWED_LAUNCHERS`); keep them in sync."""

    # ------------------------------------------------------------------
    # Quota enforcement
    # ------------------------------------------------------------------
    #
    # Per-user admit-rate token bucket. Per-tier rate + burst dicts
    # follow the ACL user-level grammar (admin | service | tierN).
    # `None` = no cap; a deployment that wants no quota at all sets
    # every level to None (or leaves the dicts as defaults for admin /
    # service and overrides the tier rows).
    #
    # See `docs/design/quota-enforcement.md` §5. The dict-shaped fields
    # take overrides via env as JSON: e.g.
    # `ROUTER_QUOTA_ADMIT_RATE_PER_S='{"tier1": 5.0}'`.
    #
    # Default tier values are conservative starting points. Adjust per
    # deployment by measuring legitimate traffic and tuning above the
    # P99.

    quota_admit_rate_per_s: dict[str, float | None] = Field(
        default_factory=lambda: {
            "admin": None,
            "service": None,
            "tier0": 100.0,
            "tier1": 20.0,
            "tier2": 5.0,
            "tier3": 1.0,
        }
    )
    """Per-tier per-second refill rate for the admit-time token bucket.
    `None` disables the cap for that tier. Bucket key is
    `quota:admit:{user_id}:{level}` so two users at the same tier
    don't share a bucket."""

    quota_admit_burst: dict[str, int | None] = Field(
        default_factory=lambda: {
            "admin": None,
            "service": None,
            "tier0": 200,
            "tier1": 40,
            "tier2": 10,
            "tier3": 2,
        }
    )
    """Per-tier bucket capacity (and starting fill). Defaults to 2× the
    rate for ~2 s of burst capacity. `None` MUST be paired with
    `quota_admit_rate_per_s[level] = None` — a cap-without-rate or
    rate-without-cap is rejected at startup."""

    file_storage_quota_bytes: dict[str, int | None] = Field(
        default_factory=lambda: {
            "admin": None,
            "service": None,
            "tier0": None,
            "tier1": 1024 * 1024 * 1024,       # 1 GiB
            "tier2": 256 * 1024 * 1024,        # 256 MiB
            "tier3": 64 * 1024 * 1024,         # 64 MiB
        }
    )
    """Per-user-level ceiling on the router-managed named file store
    (`docs/design/router-managed-file-store.md` §7). The usage
    figure is `SUM(byte_size)` over the user's `file_names`
    directory rows (session + persist; a name pointing at a shared
    blob still counts — it's the user-facing namespace accounting,
    not physical storage). Enforced at every byte-adding op (store /
    write / copy-without-move); over-ceiling is refused before bytes
    are spooled. `None` disables the cap for that level (admin /
    service / tier0 unlimited by default). Keys MUST match the
    user-level vocabulary used by `quota_admit_*`."""

    # ------------------------------------------------------------------
    # Authentication rate limits (credential-stuffing defence)
    # ------------------------------------------------------------------
    #
    # Argon2's 50-100 ms cost helps against bursts but isn't a
    # substitute for online rate-limiting. Two independent buckets
    # per login request:
    #   - per-IP: stops a single attacker from sweeping many emails
    #   - per-email: stops a distributed attack on one account
    # Either bucket exhausting → 429 with Retry-After. Defaults are
    # generous for a human, tight enough to throttle stuffing into
    # uselessness.

    login_rate_limit_per_ip_per_s: float = Field(default=0.2, ge=0.0)
    login_rate_limit_per_ip_burst: int = Field(default=5, ge=1)
    login_rate_limit_per_email_per_s: float = Field(default=0.1, ge=0.0)
    login_rate_limit_per_email_burst: int = Field(default=5, ge=1)

    # Refresh-token endpoint is hotter than login (BFFs refresh every
    # ~14 min per agent), so the per-IP bucket is looser.
    refresh_rate_limit_per_ip_per_s: float = Field(default=2.0, ge=0.0)
    refresh_rate_limit_per_ip_burst: int = Field(default=20, ge=1)

    # WS `/v1/agent` handshake per-IP bucket. Bounds unauthenticated
    # handshake floods BEFORE we spend CPU on JWT verify + Redis
    # revocation lookup. Set rate=0 to disable (the helper short-
    # circuits). Defaults are generous for legit reconnect storms
    # (network blip recovers ~20 agents at once) and tight enough
    # to throttle a flooding IP into uselessness.
    ws_handshake_rate_limit_per_ip_per_s: float = Field(default=5.0, ge=0.0)
    ws_handshake_rate_limit_per_ip_burst: int = Field(default=20, ge=1)

    # Thundering-herd guard for fleet reconnect (router restart /
    # network blip → the whole fleet handshakes in lockstep). Each
    # handshake does a `get_agent` + a full `list_agents` scan +
    # `update_agent_last_seen`; N concurrent handshakes = N pool
    # checkouts and (without the cache below) an O(N) scan each,
    # i.e. O(N²) DB work that starves every other router DB op out
    # of the default 10-conn pool.
    #
    # `ws_handshake_max_concurrent` caps how many handshakes run the
    # DB-heavy section at once. Keep it < `db_pool_max_size` so a
    # reconnect storm can never consume the whole pool; the default
    # 8 leaves headroom under the default pool of 10. The per-IP
    # bucket above is the first line of defence (rejects floods
    # pre-auth); this semaphore bounds the *authenticated* storm.
    ws_handshake_max_concurrent: int = Field(default=8, ge=1)
    # TTL for the single-flight shared `list_agents` cache that backs
    # the Welcome catalog. A reconnecting fleet collapses to ~1 DB
    # scan per TTL instead of one per handshake. Staleness is benign:
    # catalog membership is "registered + rule-allowed", not
    # "currently online" (see `visibility.available_destinations`),
    # so a peer registered within the last few seconds simply shows
    # up in catalogs a beat late. `0.0` disables the cache (every
    # handshake scans fresh — only sensible at tiny fleet sizes).
    ws_handshake_catalog_cache_ttl_s: float = Field(default=5.0, ge=0.0)

    # Change-password is authenticated, so per-user (not per-IP) is
    # the right axis. Defaults are extremely tight — a human changes
    # their password rarely.
    change_password_rate_limit_per_user_per_s: float = Field(default=0.05, ge=0.0)
    change_password_rate_limit_per_user_burst: int = Field(default=3, ge=1)

    # F7: registration submit bucket. Per-(channel, external_id) —
    # rate-limits the per-chat retry storm, not the channel agent in
    # aggregate. ≈5/h matches the volume a noisy unauthenticated chat
    # would generate.
    registration_rate_limit_per_external_per_s: float = Field(
        default=0.0014, ge=0.0
    )
    registration_rate_limit_per_external_burst: int = Field(default=5, ge=1)

    # Aggregate cap per SUBMITTING principal, across ALL external_ids. The
    # per-external bucket alone is per-`(channel, external_id)`, so one
    # authenticated caller (typically a service channel agent) can enumerate
    # distinct external_ids — each getting its own fresh bucket — to create
    # unbounded `pending_user_registrations` rows (a table-growth /
    # admin-queue-flood DoS). This second bucket bounds the aggregate rate one
    # principal can create registrations at. Default is generous (a busy
    # channel legitimately onboards many users) but FINITE, so growth from any
    # one principal is bounded by rate × time instead of unbounded.
    registration_rate_limit_per_submitter_per_s: float = Field(
        default=1.0, ge=0.0
    )
    registration_rate_limit_per_submitter_burst: int = Field(default=60, ge=1)

    # Public self-service web signup (`POST /v1/registrations/public`). This
    # endpoint is UNAUTHENTICATED — there's no submitting principal to bucket
    # on, so we cap per source IP (mirrors the password-reset consume cap).
    # Tight by default: a real person signs up once. The per-(channel,
    # external_id=email) bucket above still dedups retry storms per address.
    registration_web_rate_limit_per_ip_per_s: float = Field(
        default=0.0014, ge=0.0
    )  # ≈5/h
    registration_web_rate_limit_per_ip_burst: int = Field(default=5, ge=1)

    # Phase 10e: AgentInfo updates. Each update triggers a
    # CatalogUpdate broadcast (O(agents²) per push); rate-limit
    # per-agent to bound the load. Default 1/sec, burst 5 — fine
    # for normal usage (admin actions, MCP-bridge incremental
    # reconcile after a tools/list_changed), tight enough to
    # contain a misbehaving agent.
    agent_info_update_rate_limit_per_agent_per_s: float = Field(
        default=1.0, ge=0.0
    )
    agent_info_update_rate_limit_per_agent_burst: int = Field(default=5, ge=1)

    # File-upload negotiation, per-agent. Each accepted request
    # mints a token + the agent then streams a file; bound tight
    # enough to contain a misbehaving agent spamming the channel
    # but generous enough for legitimate multi-attachment spawns.
    file_upload_request_rate_limit_per_agent_per_s: float = Field(
        default=5.0, ge=0.0
    )
    file_upload_request_rate_limit_per_agent_burst: int = Field(
        default=20, ge=1
    )

    # F9: password-reset token TTL + per-target mint cap + per-IP
    # consume cap. Mint defaults are extremely tight (≈3/h per
    # target) so a compromised service principal can't flood the
    # password_reset_tokens table for any one user. Consume is
    # per-IP (the token is the auth — no user_id known yet).
    password_reset_token_ttl_s: int = Field(default=600, ge=60)  # 10 min
    password_reset_mint_rate_limit_per_target_per_s: float = Field(
        default=0.000833, ge=0.0
    )  # ≈3/h
    password_reset_mint_rate_limit_per_target_burst: int = Field(
        default=3, ge=1
    )
    password_reset_consume_rate_limit_per_ip_per_s: float = Field(
        default=2.0, ge=0.0
    )
    password_reset_consume_rate_limit_per_ip_burst: int = Field(
        default=20, ge=1
    )

    # Self-service channel-link tokens. A logged-in user mints a single-use
    # token for THEMSELVES (`POST /v1/auth/link-tokens`) to paste into a chat
    # bot's `/link`. Reuses the password_reset_tokens table; shorter TTL since
    # the user acts on it immediately. Per-user mint cap bounds churn.
    link_token_ttl_s: int = Field(default=900, ge=60)  # 15 min
    link_token_mint_rate_limit_per_user_per_s: float = Field(
        default=0.0028, ge=0.0
    )  # ≈10/h
    link_token_mint_rate_limit_per_user_burst: int = Field(default=5, ge=1)

    # F8: per-target cap on service-minted refresh tokens. Defends
    # `serviced_by` users against mass-mint by a compromised service
    # principal.
    service_mint_refresh_token_rate_limit_per_target_per_s: float = Field(
        default=0.00333, ge=0.0
    )  # ≈12/h
    service_mint_refresh_token_rate_limit_per_target_burst: int = Field(
        default=5, ge=1
    )

    # ------------------------------------------------------------------
    # OIDC / SSO for the webapp — see docs/design/oidc-webapp.md
    # ------------------------------------------------------------------
    # The router is the identity authority: it does discovery, code
    # exchange (with the client secret), id_token validation, user
    # provisioning, and issues the normal first-party TokenPair. The
    # browser redirects + transient state live in the frontend BFF.
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    """OP base URL; discovery at `<issuer>/.well-known/openid-configuration`."""
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    """Confidential-client secret. May be a secret_ref (`env://VAR`, …),
    resolved via `bp_router.security.secrets.resolve_secret_ref`."""
    oidc_scopes: str = "openid email profile"
    oidc_allowed_redirect_uris: list[str] = []
    """Exact-match allowlist for the browser callback URL a frontend may pass
    to the OIDC endpoints — stops the router being used as an open redirector
    / code-exchange oracle for arbitrary URIs."""
    oidc_jit_provisioning: bool = True
    """Create a local user on first successful SSO login (gated by the group /
    allowlist rules below). When false, only already-linked subjects sign in."""
    oidc_default_level: str = "tier1"
    """Level for a JIT-provisioned user when no group mapping matched."""
    oidc_group_claim: str = "groups"
    oidc_group_to_level: dict[str, str] = Field(default_factory=dict)
    """Map an IdP group → router level. First entry (by config order) whose
    group the user carries wins; otherwise `oidc_default_level`."""
    oidc_allowed_groups: list[str] = []
    """If non-empty, the user MUST carry at least one of these groups to be
    admitted — defense-in-depth on top of the OP's own access policy."""
    oidc_auto_link_by_verified_email: bool = False
    """DANGER (default off): on first SSO login, auto-link to an existing
    account sharing the same `email_verified` address. Trusts an OP-verified
    email against a router-UNVERIFIED stored email — an operator assertion of
    a single trust domain, not a guarantee. See docs/design/oidc-webapp.md §6."""
    oidc_discovery_cache_ttl_s: int = Field(default=3600, ge=0)
    oidc_http_timeout_s: float = Field(default=10.0, gt=0)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @field_validator("public_url")
    @classmethod
    def _public_url_must_be_absolute(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("public_url must be an absolute URL")
        return v.rstrip("/")

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_min_length(cls, v: SecretStr) -> SecretStr:
        # OWASP recommends HMAC keys at least as long as the hash
        # output: HS256 = SHA-256 = 32 bytes. Reject placeholder /
        # truncated values at startup so a misconfigured deploy
        # fails loudly rather than running with a brute-forceable
        # signer. Operators can generate a
        # compliant secret with `openssl rand -base64 32` (44 chars)
        # or a longer hex string.
        raw = v.get_secret_value() if v is not None else ""
        if len(raw.encode("utf-8")) < 32:
            raise ValueError(
                "jwt_secret must be at least 32 bytes "
                "(generate via `openssl rand -base64 32`)"
            )
        return v

    # Cross-field validator runs as `model_validator(mode="after")`
    # so the WHOLE model is populated before it executes. The previous
    # `field_validator` form depended on Pydantic's field-by-field
    # ordering: a field validated BEFORE the cross-referenced field
    # would see `info.data.get(...)` return None and silently pass —
    # exactly what should fail.
    @model_validator(mode="after")
    def _admin_secret_required_when_mounted(self) -> Settings:
        if self.serve_admin_ui and self.admin_session_secret is None:
            raise ValueError(
                "ROUTER_ADMIN_SESSION_SECRET must be set when serve_admin_ui=true"
            )
        return self

    @model_validator(mode="after")
    def _oidc_config_complete(self) -> Settings:
        # When SSO is on, the confidential-client essentials must all be
        # present (fail fast rather than 500 on first login), and every
        # configured level must be real.
        from bp_router.principals import is_valid_level  # noqa: PLC0415

        if self.oidc_enabled:
            missing = [
                name for name, val in (
                    ("ROUTER_OIDC_ISSUER", self.oidc_issuer),
                    ("ROUTER_OIDC_CLIENT_ID", self.oidc_client_id),
                    ("ROUTER_OIDC_CLIENT_SECRET", self.oidc_client_secret),
                ) if not val
            ]
            if missing:
                raise ValueError(
                    f"oidc_enabled=true requires {', '.join(missing)}"
                )
            if not self.oidc_allowed_redirect_uris:
                raise ValueError(
                    "oidc_enabled=true requires ROUTER_OIDC_ALLOWED_REDIRECT_URIS "
                    "(exact-match allowlist for the frontend callback URL)"
                )
            if self.oidc_issuer and not self.oidc_issuer.startswith("https://"):
                raise ValueError("oidc_issuer must be an https:// URL")
        # Validate levels even when disabled — a typo'd default/mapping
        # shouldn't lurk until the flag is flipped.
        bad = [
            lvl for lvl in (self.oidc_default_level, *self.oidc_group_to_level.values())
            if not is_valid_level(lvl)
        ]
        if bad:
            raise ValueError(
                f"invalid OIDC level(s) {bad!r}: must be admin|service|tierN"
            )
        return self

    @model_validator(mode="after")
    def _bootstrap_admin_pair_consistent(self) -> Settings:
        # Both must be set together. Setting one
        # without the other is almost always a misconfigured
        # deployment — fail fast rather than silently skipping
        # the seed at lifespan time.
        email_set = self.bootstrap_admin_email is not None
        pw_set = self.bootstrap_admin_password is not None
        if email_set != pw_set:
            raise ValueError(
                "ROUTER_BOOTSTRAP_ADMIN_EMAIL and "
                "ROUTER_BOOTSTRAP_ADMIN_PASSWORD must be set together "
                "(or both unset to skip the bootstrap step)"
            )
        return self

    @model_validator(mode="after")
    def _metrics_token_required_in_non_dev(self) -> Settings:
        # Prometheus `/metrics` leaks the live agent ID list (cardinality
        # labels), per-endpoint request rates, queue depths, error
        # taxonomy, etc. Open in dev is fine; open in staging / prod is
        # a passive recon surface for anyone who can reach the router.
        # Pattern mirrors `_redis_required_in_non_dev` — fail at
        # startup so the upgrade doesn't run unnoticed.
        if (
            self.deployment_env in ("staging", "prod")
            and self.metrics_token is None
        ):
            raise ValueError(
                "ROUTER_METRICS_TOKEN is required when deployment_env="
                f"{self.deployment_env!r}. /metrics exposes the agent ID "
                "list and per-endpoint counters; an open scrape "
                "endpoint is a recon surface. Generate via "
                "`openssl rand -base64 32` and set "
                "Authorization: Bearer on the Prometheus scrape config."
            )
        return self

    @model_validator(mode="after")
    def _redis_required_in_non_dev(self) -> Settings:
        # Multi-worker correctness: `revoke_jti`, the admit-quota
        # bucket, and the login-quota bucket silently no-op without
        # Redis. In `dev` the operator is presumably running a single
        # worker and accepts that; in `staging` / `prod` the silent
        # fallback is a security and throughput foot-gun (a logout on
        # worker-1 doesn't propagate to worker-2 until JWT expiry; the
        # per-user rate cap is per-process so the effective ceiling is
        # N×worker-count; the credential-stuffing defence loses
        # cross-worker correctness in the same way).
        if self.deployment_env in ("staging", "prod") and self.valkey_url is None:
            raise ValueError(
                "ROUTER_VALKEY_URL is required when deployment_env="
                f"{self.deployment_env!r}. JWT revocation, the "
                "per-user admit-rate quota, and the login / refresh / "
                "change-password rate limits fall back to per-process "
                "state without Redis, which is incorrect across "
                "multiple workers (silent revocation bypass, "
                "fan-out of quota caps). Set ROUTER_VALKEY_URL or "
                "switch ROUTER_DEPLOYMENT_ENV to 'dev'."
            )
        return self

    @model_validator(mode="after")
    def _quota_admit_rate_burst_paired(self) -> Settings:
        # `None` means "no cap" — but only if BOTH the rate and burst
        # are None for that level. A `rate=20.0, burst=None` would
        # produce a divide-by-zero or a runaway bucket; a
        # `rate=None, burst=40` configures a bucket nothing refills.
        # Reject the inconsistent shapes at startup.
        rates = self.quota_admit_rate_per_s
        bursts = self.quota_admit_burst
        for level in set(rates) | set(bursts):
            r = rates.get(level)
            b = bursts.get(level)
            if (r is None) != (b is None):
                raise ValueError(
                    f"quota_admit_rate_per_s[{level!r}] and "
                    f"quota_admit_burst[{level!r}] must both be set "
                    "or both be None (got rate="
                    f"{r!r}, burst={b!r})"
                )
            if r is not None and r <= 0:
                raise ValueError(
                    f"quota_admit_rate_per_s[{level!r}] must be > 0, got {r}"
                )
            if b is not None and b <= 0:
                raise ValueError(
                    f"quota_admit_burst[{level!r}] must be > 0, got {b}"
                )
        return self

    @model_validator(mode="after")
    def _file_storage_quota_positive(self) -> Settings:
        # `None` = no cap. A non-positive ceiling is a misconfig: 0
        # would silently block every store, a negative is nonsense.
        for level, cap in self.file_storage_quota_bytes.items():
            if cap is not None and cap <= 0:
                raise ValueError(
                    f"file_storage_quota_bytes[{level!r}] must be > 0 "
                    f"or None (no cap), got {cap}"
                )
        return self

    @model_validator(mode="after")
    def _db_pool_bounds_consistent(self) -> Settings:
        # Per-field `ge=1` already prevents either bound from being
        # below 1; this catches the cross-field case where min > max
        # asyncpg would otherwise raise an
        # opaque error on the first `pool.acquire()` rather than at
        # startup.
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError(
                f"db_pool_min_size ({self.db_pool_min_size}) cannot exceed "
                f"db_pool_max_size ({self.db_pool_max_size})"
            )
        return self

    @model_validator(mode="after")
    def _warn_db_pool_small_in_non_dev(self) -> Settings:
        # WARN, never raise: the right ceiling is workload-specific
        # and some small staging/prod deployments genuinely run fine
        # at the dev default — a hard startup failure here would be
        # user-hostile and is not what an advisory should do (cf.
        # `_redis_required_in_non_dev`, which DOES raise because the
        # silent fallback is a correctness/security bug, not a
        # capacity hint). This only makes the likely
        # under-provisioned case visible at boot so the
        # `router_db_pool_connections{state="in_use"}` saturation
        # signal isn't the first time anyone notices.
        if (
            self.deployment_env in ("staging", "prod")
            and self.db_pool_max_size <= _DB_POOL_DEV_DEFAULT
        ):
            logger.warning(
                "db_pool_max_size_at_dev_default_in_non_dev",
                extra={
                    "event": "db_pool_small_non_dev",
                    "db_pool_max_size": self.db_pool_max_size,
                    "deployment_env": self.deployment_env,
                },
            )
        return self


def load_settings() -> Settings:
    """Resolve `Settings` from env. Call once at process startup."""
    return Settings()  # type: ignore[call-arg]
