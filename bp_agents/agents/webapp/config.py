"""bp_agents.agents.webapp.config — settings for the webapp web server.

The web-server-specific knobs (session cookie signing, bind address,
token-refresh buffer) live here, loaded from `WEBAPP_`-prefixed env vars.
The suite DB / Redis come from `SuiteSettings` (`SUITE_`); the router WS
URL + agent invitation come from the SDK `AgentConfig` (`AGENT_`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebappConfig(BaseSettings):
    """Loaded from env vars prefixed `WEBAPP_`. The webapp is a standalone
    process fronted by the edge proxy (never mounted under the router), so
    it serves from root — no mount-prefix handling."""

    model_config = SettingsConfigDict(
        env_prefix="WEBAPP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    session_secret: SecretStr
    """Signs the session cookie. Required — fail-fast if not set."""

    session_cookie_name: str = "bp_webapp_session"
    session_cookie_max_age_s: int = 24 * 3600
    session_cookie_secure: bool = True
    """Cookie only sent over HTTPS. Secure-by-default; for local dev over
    http://localhost set ``WEBAPP_SESSION_COOKIE_SECURE=false``."""

    session_cookie_same_site: Literal["lax", "strict", "none"] = "lax"

    upstream_timeout_s: float = 10.0

    refresh_buffer_s: int = 30
    """Refresh the user's access token when this many seconds (or fewer)
    remain on the JWT — proactive, before the next request risks 401."""

    deployment_env: Literal["dev", "staging", "prod"] = "dev"

    use_built_css: bool = False
    """Serve the pre-built `/static/tailwind.css` instead of the Tailwind Play
    CDN. Opt-in (default False) so a deploy that hasn't built the CSS yet keeps
    working on the CDN. Set true in prod AFTER building the stylesheet."""

    bind_host: str = "0.0.0.0"  # noqa: S104 — container-internal; edge proxy fronts it
    bind_port: int = Field(default=8002, ge=1, le=65535)

    @field_validator("session_secret")
    @classmethod
    def _session_secret_min_length(cls, v: SecretStr) -> SecretStr:
        # The cookie is signed with this secret; a short/guessable value lets
        # an attacker forge a session. Mirror the router's jwt_secret floor
        # (OWASP: HMAC key ≥ the hash output, 32 bytes). Fail at startup so a
        # weak secret can't ship silently. Generate via `openssl rand -base64
        # 32` (44 chars).
        raw = v.get_secret_value() if v is not None else ""
        if len(raw.encode("utf-8")) < 32:
            raise ValueError(
                "WEBAPP_SESSION_SECRET must be at least 32 bytes "
                "(generate via `openssl rand -base64 32`)"
            )
        return v

    @model_validator(mode="after")
    def _prod_hardening(self) -> WebappConfig:
        # Fail-closed on prod misconfigurations that are silent in dev.
        if self.deployment_env != "prod":
            return self
        secret = self.session_secret.get_secret_value().lower()
        if "change-me" in secret or "insecure" in secret or "dev-" in secret:
            raise ValueError(
                "WEBAPP_SESSION_SECRET looks like a dev placeholder; set a "
                "real random value in prod (`openssl rand -base64 32`)"
            )
        if not self.session_cookie_secure:
            raise ValueError(
                "WEBAPP_SESSION_COOKIE_SECURE must be true in prod — the "
                "session cookie must only travel over HTTPS"
            )
        if not self.use_built_css:
            # The Tailwind Play CDN is an unpinned third-party runtime
            # dependency on an authenticated app and forces a permissive
            # script-src/style-src (it JIT-compiles in-page). Prod must serve
            # the self-hosted /static/tailwind.css (built into the image).
            raise ValueError(
                "WEBAPP_USE_BUILT_CSS must be true in prod — serve the "
                "self-hosted stylesheet, not the Tailwind Play CDN"
            )
        return self
