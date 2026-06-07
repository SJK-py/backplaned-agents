"""bp_sdk.settings — AgentConfig (env-driven)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """Per-process configuration for an external agent.

    Loaded from environment variables prefixed `AGENT_`. Embedded
    agents typically pass an `AgentConfig` constructed in Python
    instead of relying on env vars.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    embedded: bool = False
    """When True, the agent runs in-process via InProcessTransport."""

    router_url: str = "ws://localhost:8000/v1/agent"
    """WebSocket URL for external agents."""

    state_dir: Path = Field(default_factory=lambda: Path("./agent_state"))
    """Directory for persisted credentials, inbox files, etc."""

    auth_token: str | None = None
    """Bearer token. If absent at startup the SDK runs onboarding
    using `invitation_token`."""

    invitation_token: str | None = None

    onboard_url: str | None = None
    """HTTP base URL for onboarding (defaults derived from router_url)."""

    service_user_id: str | None = None
    """Co-located service principal's `user_id`, populated at onboarding
    when a service-provisioning invitation is used (None otherwise)."""

    service_refresh_token: str | None = None
    """Refresh token for `service_user_id`, redeemable at
    `/v1/auth/refresh`. Lets the agent act as a `level=service` user for
    HTTP control-plane ops (mint per-user tokens, submit registrations).
    Rotation is the consumer's responsibility — persist a rotated token
    with `bp_sdk.onboarding.persist_service_token`."""

    service_token_expires_at: str | None = None
    """ISO-8601 expiry of `service_refresh_token` (string, to match the
    credentials.json round-trip)."""

    pending_results_timeout_s: float = 480.0
    pending_acks_timeout_s: float = 30.0
    progress_buffer_size: int = 256
    reconnect_initial_backoff_s: float = 0.5
    reconnect_max_backoff_s: float = 30.0

    # ------------------------------------------------------------------
    # Tunables previously hard-coded in the SDK. Slow-network or
    # high-churn deployments can override these without forking the SDK.
    # ------------------------------------------------------------------

    recv_consecutive_failures_max: int = Field(default=16, ge=1)
    """Bail threshold for the dispatcher's recv-loop. After N
    consecutive parse / dispatch failures the SDK closes the socket
    and forces a reconnect via the supervisor.
    Default 16 catches a stuck loop within ~1-2s of churn while
    being well above any legitimate transient. Raise for noisy
    networks; lower in tests to surface bugs faster."""

    pending_buffer_window_s: float = Field(default=5.0, gt=0.0)
    """Window for buffering early `resolve()` calls before the
    awaiting `register()` lands. The unavoidable race between the
    receive loop and the spawning coroutine; 5s comfortably covers
    a single round-trip even on slow networks. Lower for
    latency-sensitive tests; raise for very slow links."""

    pending_buffer_max_size: int = Field(default=1024, ge=1)
    """Hard cap on the early-resolve buffer. Safety net for the
    window before `start_reaper` runs and for churn that exceeds
    the reaper's 1-second tick rate. Oldest entry is evicted on
    overflow."""

    ws_max_receive_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    """Hard ceiling on a single incoming WS message the agent's
    `websockets` client will accept (the library's `max_size`).
    Tripping it closes the socket with code 1009 — a hard teardown
    that loses in-flight state — so it MUST sit strictly above the
    router-negotiated `WelcomeFrame.max_payload_bytes` (= router's
    `Settings.max_payload_bytes`) with headroom for the per-frame
    envelope (header fields + JSON keys/escaping). The 2 MiB default
    pairs with the router's 1 MiB default cap — a 2× envelope budget
    that keeps the soft-fail-first ordering working
    (`FrameTooLargeError` fires before the hard 1009). If an
    operator raises `max_payload_bytes`, this MUST be raised in
    lockstep (rule of thumb: `≥ 2 × max_payload_bytes`), together
    with the router-side ASGI `ws_max_size` and any L7 proxy / ALB
    body-size limits — see `docs/backplaned/sdk/core.md` §7."""

    log_level: str = "INFO"


def load_agent_config() -> AgentConfig:
    return AgentConfig()  # type: ignore[call-arg]
