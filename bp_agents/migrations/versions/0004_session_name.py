"""session_info.session_name — human-friendly conversation title

Revision ID: 0004_session_name
Revises: 0003_session_channel_nullable
Create Date: 2026-05-29

A nullable display name shown in the webapp session list instead of the raw
`session_id`. Auto-generated from the first user message (history_summarizer
`session_name` mode) and editable via the webapp Rename action.
"""

from __future__ import annotations

from alembic import op

revision = "0004_session_name"
down_revision = "0003_session_channel_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE session_info ADD COLUMN session_name text")


def downgrade() -> None:
    op.execute("ALTER TABLE session_info DROP COLUMN session_name")
