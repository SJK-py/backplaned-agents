"""bp_agents.db.models — row models for the suite's Postgres tables.

Pydantic-validated, instantiated from asyncpg `Record`s via
`Model.model_validate(dict(record))`. Schema is owned by the suite's
Alembic migrations; a column change means a migration AND a model edit.

Full schema reference: `docs/agent-suite/data-model.md` §1.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class SessionInfoRow(_Row):
    """One row per session — channel-written ([data-model.md] §1.1)."""

    session_id: str
    user_id: str
    channel: str | None = None  # chatbot_telegram | webapp | None (released)
    session_name: str | None = None  # human-friendly title (auto + rename)
    chat_id: str | None = None
    delegated_to: str | None = None
    history_summary: str | None = None
    delegate_summary: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionHistoryRow(_Row):
    """The conversation log ([data-model.md] §1.2). `agent_id` is the
    thread key (set on `user` rows too)."""

    id: int
    session_id: str
    agent_id: str
    role: str  # user | assistant | tool_call | tool_result
    message: str
    created_at: datetime
    incumbent: bool
    hidden: bool


class UserConfigRow(_Row):
    """One row per user ([data-model.md] §1.3)."""

    user_id: str
    full_name: str
    timezone: str
    preset_pro: str
    preset_balanced: str
    preset_lite: str
    preset_embedding: str
    max_context_token_limit: int
    verbose_default: bool
    language: str
    sandbox_uid: int | None = None
    default_session_id: str | None = None
    custom_note: str
    created_at: datetime
    updated_at: datetime


class PlatformMappingRow(_Row):
    """Inbound identity — `chat_id → user_id` ([data-model.md] §1.6).

    `session_id` is the chat's CURRENT live session (its own conversation);
    inbound routing rides it, falling back to `user_config.default_session_id`
    (the cron fallback) only when a chat has none yet. NULL right after a chat
    is mapped but before its first session is opened."""

    platform: str  # telegram | web | kakao
    chat_id: str
    user_id: str
    created_at: datetime
    session_id: str | None = None


class CronJobRow(_Row):
    """A scheduled job ([data-model.md] §1.4)."""

    cron_id: str
    user_id: str
    session_id: str
    cron_expression: str
    timezone: str
    report: str  # always | never | case_by_case
    cron_message: str
    status: str  # active | inactive
    execute_until: datetime | None = None
    created_at: datetime
    last_executed_at: datetime | None = None


class CronExecutionRow(_Row):
    """A cron fire-log entry ([data-model.md] §1.5)."""

    id: int
    cron_id: str
    user_id: str
    session_id: str
    fired_at: datetime
    reported: bool
    reason: str | None = None
    message: str | None = None
    error: str | None = None
