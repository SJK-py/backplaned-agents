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

    # ------------------------------------------------------------------
    # chatbot channel (Telegram)
    # ------------------------------------------------------------------

    telegram_bot_token: str | None = None
    """Telegram bot token. When unset the chatbot connects but the poll
    loop is not launched (useful for tests / dry runs)."""

    telegram_base_url: str = "https://api.telegram.org"
    telegram_poll_timeout_s: int = Field(default=25, ge=0)
    """Long-poll `getUpdates` timeout."""

    dispatch_result_timeout_s: float = Field(default=180.0, gt=0.0)
    """How long the channel waits for an injected turn's result before
    surfacing a failure to the user."""

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


def load_suite_settings() -> SuiteSettings:
    return SuiteSettings()  # type: ignore[call-arg]
