"""bp_agents.settings — SuiteSettings (env-driven).

The suite's own configuration, separate from the router's `Settings`.
Loaded from environment variables prefixed `SUITE_`.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SuiteSettings(BaseSettings):
    """Per-process configuration for the agent suite.

    The suite keeps its own Postgres (`database_url`), distinct from
    the router's DB — joined only by `user_id` / `session_id`.
    """

    model_config = SettingsConfigDict(
        env_prefix="SUITE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://postgres:bp@127.0.0.1:5432/bp_suite"
    """asyncpg DSN for the suite's Postgres."""

    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)
    """Per-connection `statement_timeout` so a runaway query can't pin
    a pool connection indefinitely."""

    redis_url: str | None = None
    """Optional Redis DSN (`SUITE_REDIS_URL`). When set, the channel's
    per-session lock becomes cross-process (a distributed lock) instead of
    an in-process `asyncio.Lock` — the prerequisite for running more than
    one channel instance (e.g. a webapp alongside the Telegram bot). When
    unset, the lock is in-process only (correct for a single instance).
    May point at the same Redis the router uses; keys are prefixed."""

    delegatable_agents: list[str] = ["research", "computer_use", "deep_reasoning"]
    """Agent ids a user may hand the conversation to via `/delegate <id>`
    (the l1 specialists). The channel has no peer-catalog visibility, so
    this is the allow-list it validates `/delegate` targets against."""

    # ------------------------------------------------------------------
    # user_config defaults — seeded into a new `user_config` row at
    # registration approval; users tune them later via the config agent.
    # ------------------------------------------------------------------

    default_timezone: str = "UTC"
    default_language: str = "en"
    default_max_context_token_limit: int = Field(default=120_000, ge=1)
    """Soft summarization trigger; keep headroom below the provider's
    real window ([sessions.md] §3.2)."""

    default_preset_pro: str = "default"
    default_preset_balanced: str = "default"
    default_preset_lite: str = "default"
    default_preset_embedding: str = "default_embedding"
    """Router LLM-preset names per tier (deep_reasoning / orchestrator /
    lite helpers / embeddings). The three chat tiers default to the router's
    seeded `default` chat preset; embeddings default to `default_embedding`
    (a real embedding model — `default` is chat-only and can't embed)."""

    selectable_presets_pro: list[str] = []
    selectable_presets_balanced: list[str] = []
    selectable_presets_lite: list[str] = []
    """Preset names a user may self-select for each chat tier (deep_reasoning
    / orchestrator+research / lite helpers), via the config agent or the
    webapp settings form. EMPTY (the default) means that tier's preset is
    system-managed and NOT user-editable — preserving the prior behaviour.
    Populate per tier to opt in, e.g.
    `SUITE_SELECTABLE_PRESETS_BALANCED='["default","claude"]'`. Only list
    presets the router has actually seeded and that suit your users' level —
    the router still enforces each preset's `min_user_level` at call time as
    a backstop. Embeddings stay system-managed (not exposed)."""

    # ------------------------------------------------------------------
    # chatbot channel (Telegram)
    # ------------------------------------------------------------------

    verbose_detail_chars: int = Field(default=100, ge=0)
    """Verbose-mode progress `detail` cap: the last paragraph of the model's
    reasoning / tool message / tool result is trimmed to this many trailing
    characters (0 disables detail, leaving the bare kind + tool name)."""

    telegram_bot_token: str | None = None
    """Telegram bot token. When unset the chatbot connects but the poll
    loop is not launched (useful for tests / dry runs)."""

    telegram_base_url: str = "https://api.telegram.org"
    telegram_poll_timeout_s: int = Field(default=25, ge=0)
    """Long-poll `getUpdates` timeout."""

    dispatch_result_timeout_s: float = Field(default=600.0, gt=0.0)
    """How long the channel waits for an injected turn's result before
    surfacing a failure to the user."""

    # ------------------------------------------------------------------
    # chatbot channel (KakaoTalk)
    #
    # A second chatbot channel behind a stateless Cloudflare Worker relay
    # + Cloudflare Queue ([../docs/design/kakao-channel.md]). The agent is
    # egress-only: it PULLS jobs from the queue over HTTPS (it never
    # listens), mirroring the Telegram poll loop. Unset → the consumer is
    # never launched (boot is byte-for-byte identical to today). The
    # relay's own secret (`KAKAO_SKILL_SECRET`) lives on the Worker, not
    # here — the activation gate is the three queue-credential fields.
    # ------------------------------------------------------------------

    kakao_cf_account_id: str | None = None
    """Cloudflare account id owning the jobs queue. One of the three
    queue-credential fields that gate the KakaoTalk consumer."""

    kakao_cf_queue_id: str | None = None
    """Cloudflare Queue id the relay produces to and the agent pulls from."""

    kakao_cf_api_token: SecretStr | None = None
    """Cloudflare API token scoped to Queues pull+ack on the queue above.
    An outbound-only credential; it never sits on a public surface."""

    kakao_pull_batch_size: int = Field(default=10, ge=1)
    """Max messages pulled per CF Queues `messages/pull` call."""

    kakao_pull_visibility_timeout_s: int = Field(default=60, ge=1)
    """Lease/visibility timeout for a pulled batch — an unacked message
    reappears after this, so the loop retries a turn it failed to handle."""

    kakao_callback_ttl_s: float = Field(default=60.0, gt=0.0)
    """Kakao's own single-use `callbackUrl` lifetime, as a tunable (the
    documented value is ~60s; verify against current Kakao docs). The
    channel caps its delivery budget by the callback's remaining TTL, so a
    job pulled after an outage delivers via park + next-touch instead of a
    dead callback."""

    kakao_callback_deadline_s: float = Field(default=50.0, gt=0.0)
    """Budget for delivering a turn on Kakao's single-use `callbackUrl`
    before the channel falls back to park + next-touch delivery; kept
    below `kakao_callback_ttl_s`."""

    kakao_carry_ttl_s: int = Field(default=900, ge=1)
    """How long a parked (overran-the-callback) result waits in Redis for
    the user's next touch before it lapses and they must re-ask."""

    kakao_msg_char_limit: int = Field(default=1000, ge=10)
    """Per-bubble character cap for an outbound Kakao `simpleText` (a long
    reply is chunked below this). Floored well above the truncation marker."""

    # KakaoTalk outbound images (R2 / S3-compatible). Rendering an image in
    # KakaoTalk requires a PUBLIC url Kakao's servers fetch; the router blob
    # store is internal-only, so outbound images go to a dedicated bucket
    # and Kakao gets a short-TTL presigned GET ([kakao-channel.md] §8).
    # Inbound images reuse the router named store (no config here). Needs
    # the `kakaotalk` (aioboto3) extra.
    kakao_r2_endpoint_url: str | None = None
    kakao_r2_bucket: str | None = None
    kakao_r2_access_key_id: str | None = None
    kakao_r2_secret_access_key: SecretStr | None = None
    kakao_r2_url_ttl_s: int = Field(default=600, ge=1)
    """Lifetime of a presigned outbound-image url handed to Kakao."""

    # ------------------------------------------------------------------
    # deep_reasoning plan_mode ([agents.md] deep_reasoning)
    # ------------------------------------------------------------------

    plan_max_steps: int = Field(default=12, ge=1)
    """Hard cap on the number of steps a plan may hold (add_step beyond
    this is ignored), bounding plan size."""

    plan_max_iters: int = Field(default=24, ge=1)
    """Hard cap on planner decision rounds (mutations + executions +
    finalize attempts), so a plan always terminates."""

    plan_step_timeout_s: float = Field(default=240.0, gt=0.0)
    """Per-step `execute_step` → `orchestrator(subagent)` result timeout."""

    # ------------------------------------------------------------------
    # Per-user LanceDB (knowledge base + memory)
    # ------------------------------------------------------------------

    lance_root: str = "./suite_lance"
    """Root dir under which each user's LanceDB lives (`<root>/<user_id>`)."""

    embedding_dim: int = Field(default=1536, ge=1)
    """Dimension of the embedding preset's vectors (text-embedding-3-small
    = 1536). Must match the configured `preset_embedding` model."""

    kb_max_chunk_len: int = Field(default=2000, ge=1)
    kb_min_chunk_len: int = Field(default=1000, ge=1)
    kb_overlap_len: int = Field(default=100, ge=0)
    """Markdown chunking bounds ([data-model.md] §2.1)."""

    kb_meta_head_chars: int = Field(default=8000, ge=0)
    kb_meta_tail_chars: int = Field(default=2000, ge=0)
    """Head/tail window fed to the LLM for `store` metadata generation
    (title/tags/description) when the caller omits them ([agents.md])."""

    # ------------------------------------------------------------------
    # memory fact-graph ([memory.md])
    # ------------------------------------------------------------------

    memory_retrieve_pool: int = Field(default=50, ge=1)
    """Hybrid-search pool size before recency re-rank."""
    memory_reconcile_candidates: int = Field(default=5, ge=1)
    """Top-N similar facts enumerated to the reconcile LLM call."""
    memory_decay_start_days: int = Field(default=30, ge=0)
    memory_gc_horizon_days: int = Field(default=100, ge=1)
    memory_decay_floor: float = Field(default=0.5, ge=0.0, le=1.0)
    """Recency-decay floor for a fact approaching the GC horizon."""
    memory_gc_interval_s: float = Field(default=86_400.0, gt=0)
    """Period of the background GC sweep over per-user fact graphs (the
    decay path keeps surfaced facts alive, so a daily sweep suffices)."""

    # ------------------------------------------------------------------
    # sandbox (shared container, per-uid / per-user workspace)
    # ------------------------------------------------------------------

    sandbox_root: str = "/home"
    """Root under which each user's workspace lives (`<root>/<user_id>`)."""
    sandbox_bash_timeout_s: float = Field(default=120.0, gt=0.0)
    sandbox_max_inline_output: int = Field(default=8000, ge=1)
    """stdout above this many chars is saved to a file-store name instead
    of inlined."""

    # Per-user uid isolation. The sandbox drops each user's bash to a distinct
    # OS uid (filesystem/process isolation inside the shared container). The
    # `user_id → uid` map is owned locally by the sandbox in a JSON file on its
    # AGENT_STATE_DIR (`bp_agents.agents.sandbox.uid_store`) — NOT the suite DB,
    # which the sandbox is deliberately isolated from. uids are allocated
    # sequentially from the base. The drop only engages when the process runs
    # as root (prod); in rootless dev it's a no-op.
    sandbox_uid_base: int = Field(default=2_000, ge=1)
    """First uid handed out. Subsequent users get base+1, base+2, …

    MUST be a uid that is valid INSIDE the container. Under Docker
    userns-remap (or rootless Docker) the container maps a sub-uid range to
    container uids 0..65535 — a uid OUTSIDE that (e.g. the old 100000 default,
    which confused the HOST sub-uid range with the in-container space) makes
    chown fail with EINVAL ("Invalid argument") and every bash command error.
    Default 2000: above system/login uids (collision-safe) and inside the
    65536-wide namespace, so it works both with and without userns-remap. On a
    host with a different mapping, set SUITE_SANDBOX_UID_BASE/_MAX to a range
    your container actually maps."""
    sandbox_uid_max: int = Field(default=60_000, ge=1)
    """Upper bound of the uid range — kept below 65536 so the whole range stays
    inside a remapped container's uid space. A user arriving past this gets no
    uid drop (logged) rather than a colliding uid."""

    # Resource limits (rlimits) applied to every sandbox bash subprocess so a
    # single tenant's command can't starve the SHARED container (the wall-clock
    # timeout + uid drop don't bound resource use). Applied in the child
    # pre-exec regardless of the uid drop — a non-root process can still lower
    # its own limits. 0 disables a given cap.
    sandbox_rlimit_nproc: int = Field(default=256, ge=0)
    """RLIMIT_NPROC — max processes for the sandbox uid (fork-bomb guard)."""
    sandbox_rlimit_as_bytes: int = Field(default=2 * 1024**3, ge=0)
    """RLIMIT_AS — max virtual address space per process (memory-balloon
    guard). Default 2 GiB."""
    sandbox_rlimit_fsize_bytes: int = Field(default=1024**3, ge=0)
    """RLIMIT_FSIZE — max single-file size the command can write (disk-fill
    guard). Default 1 GiB."""
    sandbox_rlimit_cpu_s: int = Field(default=120, ge=0)
    """RLIMIT_CPU — max CPU-seconds (CPU-spin guard); pair with the wall-clock
    timeout, which a busy-loop wouldn't otherwise trip cleanly."""

    # ------------------------------------------------------------------
    # research web tools
    # ------------------------------------------------------------------

    searxng_url: str | None = None
    """Brave-API-compatible search endpoint (e.g. a SearXNG instance).
    When unset, web_search returns an unavailable notice."""
    web_fetch_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    web_fetch_timeout_s: float = Field(default=120.0, gt=0.0)
    web_fetch_user_agent: str = Field(
        default="Mozilla/5.0 (compatible; BackplanedBot/1.0)"
    )
    """User-Agent for agent web fetches. The default is HONEST — it identifies
    the fetcher via the well-behaved-crawler `(compatible; <bot>)` convention
    rather than impersonating a browser. Operators can override it (e.g. add
    their own bot name + a `+https://…` contact URL) to improve trust; a
    browser UA gets accepted more widely but is deceptive."""
    web_fetch_max_redirects: int = Field(default=3, ge=0)
    """Max redirect hops an agent fetch follows. Each hop is re-validated
    against the SSRF guard. `0` disables redirect-following."""


def load_suite_settings() -> SuiteSettings:
    return SuiteSettings()  # type: ignore[call-arg]
