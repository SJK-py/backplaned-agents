"""bp_agents.agents.webapp.config — settings for the webapp web server.

The web-server-specific knobs (session cookie signing, bind address,
token-refresh buffer) live here, loaded from `WEBAPP_`-prefixed env vars.
The suite DB / Redis come from `SuiteSettings` (`SUITE_`); the router WS
URL + agent invitation come from the SDK `AgentConfig` (`AGENT_`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
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
