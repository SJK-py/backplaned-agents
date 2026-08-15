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


class UserConfigRow(_Row):
    """One row per user ([data-model.md] §1.3).

    Two fields wide, because the user's SETTINGS moved to the router's
    user-scoped state (`bp_agents.user_prefs`). What is left is what cannot
    live there: both are read OUTSIDE any task — by the cron scheduler and
    the sandbox host — where neither an agent's `ctx.history` nor a steward's
    carrier session exists."""

    user_id: str
    sandbox_uid: int | None = None
    default_session_id: str | None = None
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
