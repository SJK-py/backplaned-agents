"""kakao channel — widen channel / platform CHECK constraints

Admit the KakaoTalk chatbot channel into the two enum-style CHECK
constraints seeded by 0001:
  * session_info.channel             += 'chatbot_kakao'
  * suite_platform_mappings.platform += 'kakao'

No new tables — KakaoTalk writes through ChannelCore like any channel
([../../../docs/design/kakao-channel.md] §11). The 0001 checks are
anonymous inline column constraints, so Postgres auto-named them
`<table>_<column>_check`; we drop and recreate each with the widened set.

Revision ID: 0002_kakao_channel
Revises: 0001_suite_initial
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op

revision = "0002_kakao_channel"
down_revision = "0001_suite_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE session_info DROP CONSTRAINT session_info_channel_check"
    )
    op.execute(
        "ALTER TABLE session_info ADD CONSTRAINT session_info_channel_check "
        "CHECK (channel IN ('chatbot_telegram', 'webapp', 'chatbot_kakao'))"
    )
    op.execute(
        "ALTER TABLE suite_platform_mappings "
        "DROP CONSTRAINT suite_platform_mappings_platform_check"
    )
    op.execute(
        "ALTER TABLE suite_platform_mappings "
        "ADD CONSTRAINT suite_platform_mappings_platform_check "
        "CHECK (platform IN ('telegram', 'web', 'kakao'))"
    )


def downgrade() -> None:
    # Revert to the 0001 set. Safe only if no 'chatbot_kakao' / 'kakao'
    # rows exist (they would violate the narrower check); pre-release,
    # none do.
    op.execute(
        "ALTER TABLE suite_platform_mappings "
        "DROP CONSTRAINT suite_platform_mappings_platform_check"
    )
    op.execute(
        "ALTER TABLE suite_platform_mappings "
        "ADD CONSTRAINT suite_platform_mappings_platform_check "
        "CHECK (platform IN ('telegram', 'web'))"
    )
    op.execute(
        "ALTER TABLE session_info DROP CONSTRAINT session_info_channel_check"
    )
    op.execute(
        "ALTER TABLE session_info ADD CONSTRAINT session_info_channel_check "
        "CHECK (channel IN ('chatbot_telegram', 'webapp'))"
    )
