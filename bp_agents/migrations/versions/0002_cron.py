"""cron jobs + executions ([data-model.md] §1.4-1.5)

Revision ID: 0002_cron
Revises: 0001_suite_initial
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op

revision = "0002_cron"
down_revision = "0001_suite_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
