"""bp_agents.settings — SuiteSettings (env-driven).

The suite's own configuration, separate from the router's `Settings`.
Loaded from environment variables prefixed `SUITE_`.
"""

from __future__ import annotations

from pydantic import Field
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
    default_preset_embedding: str = "default"
    """Router LLM-preset names per tier (deep_reasoning / orchestrator /
    lite helpers / embeddings). Default to the router's seeded
    `default` preset until per-tier presets are configured."""


def load_suite_settings() -> SuiteSettings:
    return SuiteSettings()  # type: ignore[call-arg]
