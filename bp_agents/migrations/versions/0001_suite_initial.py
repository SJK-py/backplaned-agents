"""suite initial schema (consolidated v1 — pre-release)

Creates the suite's Postgres tables ([data-model.md] §1):
session_info, session_history, user_config, suite_platform_mappings,
cron_jobs, cron_executions.

This is a CONSOLIDATED v1 baseline. The codebase is pre-release; no
deployment carries an intermediate schema, so the historical
incremental migrations have been folded into this single file — a
fresh deployment runs ONE migration and lands on the final schema.
Post-release schema changes get fresh sequence numbers (0002+).

Folded in (previously standalone migrations 0002–0004):
  * 0002 — cron_jobs / cron_executions tables.
  * 0003 — session_info.channel made nullable (the chatbot clears it
    on `/new` so the session is no longer Telegram-owned; declared
    nullable inline here, no DROP NOT NULL dance on a fresh schema).
  * 0004 — session_info.session_name (webapp display title).

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
    # `channel` is nullable (folded from 0003): the chatbot clears it
    # on `/new` to 'release' the session so the webapp can reopen/remove
    # it ([webapp.md] §4). NULL satisfies the CHECK (NULL → not
    # violated). `session_name` (folded from 0004) is the webapp's
    # human-friendly conversation title.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE session_info (
            session_id        text PRIMARY KEY,
            user_id           text NOT NULL,
            channel           text
                              CHECK (channel IN ('chatbot_telegram', 'webapp')),
            chat_id           text,
            delegated_to      text,
            history_summary   text,
            delegate_summary  text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            session_name      text
        )
    """)
    op.execute(
        "CREATE INDEX session_info_user_idx "
        "ON session_info(user_id, created_at DESC)"
    )

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
            preset_embedding         text NOT NULL DEFAULT 'default_embedding',
            max_context_token_limit  integer NOT NULL DEFAULT 120000,
            verbose_default          boolean NOT NULL DEFAULT false,
            language                 text NOT NULL DEFAULT 'en',
            sandbox_uid              integer,  -- DEAD: the sandbox now owns
                                              -- per-user uids in a local JSON
                                              -- store (it's network-isolated
                                              -- from this DB). Never written;
                                              -- kept to avoid a migration.
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

    # ------------------------------------------------------------------
    # cron_jobs (folded from 0002) — scheduled per-session prompts
    # ([data-model.md] §1.4).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE cron_jobs (
            cron_id           text PRIMARY KEY,
            user_id           text NOT NULL,
            session_id        text NOT NULL,
            cron_expression   text NOT NULL,
            timezone          text NOT NULL DEFAULT 'UTC',
            report            text NOT NULL DEFAULT 'case_by_case'
                              CHECK (report IN ('always', 'never', 'case_by_case')),
            cron_message      text NOT NULL,
            status            text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'inactive')),
            execute_until     timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            last_executed_at  timestamptz
        )
    """)
    op.execute("CREATE INDEX cron_jobs_user_idx ON cron_jobs(user_id)")
    # The scheduler scans active jobs; partial index keeps that cheap.
    op.execute(
        "CREATE INDEX cron_jobs_active_idx ON cron_jobs(status) "
        "WHERE status = 'active'"
    )

    # ------------------------------------------------------------------
    # cron_executions (folded from 0002) — one row per firing
    # ([data-model.md] §1.5).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE cron_executions (
            id          bigserial PRIMARY KEY,
            cron_id     text NOT NULL,
            user_id     text NOT NULL,
            session_id  text NOT NULL,
            fired_at    timestamptz NOT NULL DEFAULT now(),
            reported    boolean NOT NULL,
            reason      text,
            message     text,
            error       text
        )
    """)
    op.execute("CREATE INDEX cron_executions_cron_idx ON cron_executions(cron_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cron_executions")
    op.execute("DROP TABLE IF EXISTS cron_jobs")
    op.execute("DROP TABLE IF EXISTS suite_platform_mappings")
    op.execute("DROP TABLE IF EXISTS user_config")
    op.execute("DROP TABLE IF EXISTS session_history")
    op.execute("DROP TABLE IF EXISTS session_info")
