"""per-chat current session — decouple live routing from the cron fallback

Each `(platform, chat_id)` chat now tracks its OWN live session, so a user who
links two channels (e.g. Telegram + KakaoTalk via `/link`) keeps a separate
conversation on each instead of both interleaving into the single
`user_config.default_session_id`. `default_session_id` is demoted to purely the
cron fallback ([cron.md] §2); inbound routing reads `suite_platform_mappings.
session_id`, falling back to the default only when the chat has none yet.

Backfill: every existing mapping inherits the user's current
`default_session_id`, so single-channel users are unaffected (their one chat
keeps routing to the same session it does today).

Revision ID: 0003_per_chat_session
Revises: 0002_kakao_channel
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op

revision = "0003_per_chat_session"
down_revision = "0002_kakao_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE suite_platform_mappings ADD COLUMN session_id text"
    )
    # Seed each chat's current session from the user's existing default so
    # today's (single-channel) routing is preserved across the upgrade.
    op.execute(
        """
        UPDATE suite_platform_mappings m
        SET session_id = uc.default_session_id
        FROM user_config uc
        WHERE m.user_id = uc.user_id
          AND uc.default_session_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE suite_platform_mappings DROP COLUMN session_id"
    )
