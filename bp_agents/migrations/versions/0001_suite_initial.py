"""suite initial schema — sessions, history, user config, mappings

Creates the suite's Postgres tables ([data-model.md] §1):
session_info, session_history, user_config, suite_platform_mappings.
Cron tables (cron_jobs, cron_executions) land in a later migration
(Phase 4) so the v1 vertical slice doesn't carry unused schema.

Revision ID: 0001_suite_initial
Revises:
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op

revision = "0001_suite_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # session_info — one row per session (channel-owned writes).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE session_info (
            session_id        text PRIMARY KEY,
            user_id           text NOT NULL,
            channel           text NOT NULL
                              CHECK (channel IN ('chatbot_telegram', 'webapp')),
            chat_id           text,
            delegated_to      text,
            history_summary   text,
            delegate_summary  text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX session_info_user_idx ON session_info(user_id)")

    # ------------------------------------------------------------------
    # session_history — the conversation log. `agent_id` is the thread
    # key (set on `user` rows too). The composite index serves the
    # incumbent-reload query ([sessions.md] §2.1).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE session_history (
            id          bigserial PRIMARY KEY,
            session_id  text NOT NULL,
            agent_id    text NOT NULL,
            role        text NOT NULL
                        CHECK (role IN ('user', 'assistant', 'tool_call', 'tool_result')),
            message     text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            incumbent   boolean NOT NULL DEFAULT true,
            hidden      boolean NOT NULL DEFAULT false
        )
    """)
    op.execute(
        "CREATE INDEX session_history_reload_idx "
        "ON session_history (session_id, agent_id, incumbent, created_at)"
    )

    # ------------------------------------------------------------------
    # user_config — one row per user. Presets reference router LLM-preset
    # names; `default_session_id` is the cron-fallback pointer.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE user_config (
            user_id                  text PRIMARY KEY,
            full_name                text NOT NULL DEFAULT '',
            timezone                 text NOT NULL DEFAULT 'UTC',
            preset_pro               text NOT NULL DEFAULT 'default',
            preset_balanced          text NOT NULL DEFAULT 'default',
            preset_lite              text NOT NULL DEFAULT 'default',
            preset_embedding         text NOT NULL DEFAULT 'default',
            max_context_token_limit  integer NOT NULL DEFAULT 120000,
            verbose_default          boolean NOT NULL DEFAULT false,
            language                 text NOT NULL DEFAULT 'en',
            sandbox_uid              integer,
            default_session_id       text,
            custom_note              text NOT NULL DEFAULT '',
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ------------------------------------------------------------------
    # suite_platform_mappings — inbound identity (chat_id → user_id).
    # PK (platform, chat_id); reverse index on user_id.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE suite_platform_mappings (
            platform    text NOT NULL CHECK (platform IN ('telegram', 'web')),
            chat_id     text NOT NULL,
            user_id     text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (platform, chat_id)
        )
    """)
    op.execute(
        "CREATE INDEX suite_platform_mappings_user_idx "
        "ON suite_platform_mappings(user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS suite_platform_mappings")
    op.execute("DROP TABLE IF EXISTS user_config")
    op.execute("DROP TABLE IF EXISTS session_history")
    op.execute("DROP TABLE IF EXISTS session_info")
