"""session_info.channel nullable — 'release' on chatbot close

Revision ID: 0003_session_channel_nullable
Revises: 0002_cron
Create Date: 2026-05-29

When the chatbot closes a session (on `/new`) it clears `channel` so the
session is no longer Telegram-owned and the webapp can reopen/remove it
([webapp.md] §4). `NULL` already satisfies the existing
`channel IN ('chatbot_telegram','webapp')` CHECK (NULL → not violated), so
only the NOT NULL constraint is dropped.
"""

from __future__ import annotations

from alembic import op

revision = "0003_session_channel_nullable"
down_revision = "0002_cron"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE session_info ALTER COLUMN channel DROP NOT NULL")


def downgrade() -> None:
    # Reclaim any released rows to a concrete channel before re-imposing
    # NOT NULL, so the downgrade can't fail on existing NULLs.
    op.execute("UPDATE session_info SET channel = 'webapp' WHERE channel IS NULL")
    op.execute("ALTER TABLE session_info ALTER COLUMN channel SET NOT NULL")
