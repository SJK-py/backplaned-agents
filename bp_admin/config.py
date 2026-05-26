"""bp_admin.config — settings for the admin BFF."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdminConfig(BaseSettings):
    """Loaded from env vars prefixed `ADMIN_`. Defaults are tuned for the
    same-process deployment (admin mounted on the router under `/admin`).
    """

    model_config = SettingsConfigDict(
        env_prefix="ADMIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    router_url: str = "http://127.0.0.1:8000"
    """Base URL the BFF uses for upstream calls. In same-process mode this
    is loopback to the router's own bind address."""

    session_secret: SecretStr
    """Signs the BFF's session cookie. Required — fail-fast if not set."""

    session_cookie_name: str = "bp_admin_session"
    session_cookie_max_age_s: int = 24 * 3600
    session_cookie_secure: bool = True
    """Cookie only sent over HTTPS. Defaults to True so production
    deployments are secure-by-default — a forgotten override no
    longer leaks the session over plain HTTP. For
    local dev over http://localhost, set
    ``ADMIN_SESSION_COOKIE_SECURE=false`` explicitly in `.env`."""

    session_cookie_same_site: Literal["lax", "strict", "none"] = "strict"

    upstream_timeout_s: float = 10.0

    refresh_buffer_s: int = 30
    """Refresh the upstream access token when this many seconds (or fewer)
    remain on the JWT — proactive, before the next request risks 401."""

    deployment_env: Literal["dev", "staging", "prod"] = "dev"

    log_level: str = "INFO"

    bind_host: str = "127.0.0.1"
    """Interface the standalone `bp-admin` server binds to. Loopback
    by default — operators behind a reverse proxy or in a container
    override with `ADMIN_BIND_HOST=0.0.0.0`. Ignored when admin is
    mounted under the router in the same process."""

    bind_port: int = Field(default=8001, ge=1, le=65535)
