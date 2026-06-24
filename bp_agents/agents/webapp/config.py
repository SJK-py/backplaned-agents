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

    sso_enabled: bool = False
    """Show the 'Sign in with SSO' button and enable the OIDC browser flow.
    The router must also have ROUTER_OIDC_ENABLED=true; this is just the
    frontend toggle."""

    public_base_url: str | None = None
    """The webapp's externally-reachable base URL (e.g. https://app.example),
    used to build the OIDC `redirect_uri` (`<base>/auth/sso/callback`). Must be
    set when `sso_enabled`, registered at the OP, and present in the router's
    ROUTER_OIDC_ALLOWED_REDIRECT_URIS allowlist."""

    password_login_enabled: bool = True
    """Whether the webapp offers email + password sign-in (and the password-
    credential paths: `/register`, `/set-password`). Set false to make the
    webapp **SSO-only** once OIDC is configured — the password form, the
    "request access" / "set with a token" links, and the `/login`,
    `/register`, `/set-password` POST handlers are all refused. Requires
    `sso_enabled` (disabling it without SSO would lock everyone out)."""

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
    def _sso_needs_base_url(self) -> WebappConfig:
        if self.sso_enabled:
            base = (self.public_base_url or "").rstrip("/")
            if not base.startswith(("http://", "https://")):
                raise ValueError(
                    "WEBAPP_PUBLIC_BASE_URL must be an absolute URL when "
                    "WEBAPP_SSO_ENABLED=true (used for the OIDC redirect_uri)"
                )
        return self

    @model_validator(mode="after")
    def _no_password_only_lockout(self) -> WebappConfig:
        if not self.password_login_enabled and not self.sso_enabled:
            raise ValueError(
                "WEBAPP_PASSWORD_LOGIN_ENABLED=false requires "
                "WEBAPP_SSO_ENABLED=true — disabling password login without "
                "SSO would leave no way to sign in"
            )
        return self

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
