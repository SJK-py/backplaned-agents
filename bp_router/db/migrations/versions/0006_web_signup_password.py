"""pending_user_registrations: requested_password_hash — self-service web signup.

Chat-channel registrations (Telegram/Kakao) are submitted by a service
principal on behalf of a chat it controls; the user's password is set later
out-of-band (the bot mints a reset token via `serviced_by`). The webapp
self-service path has no such service principal and no email-delivery channel,
so the user CHOOSES their password on the public signup form. We carry its
argon2 hash on the pending row until an admin approves, then seed the new
user's `auth_secret_hash` directly — no token bootstrap, no `serviced_by`.

Nullable: chat-channel registrations leave it NULL and keep the existing
random-initial-password approval behavior.
"""

from __future__ import annotations

from alembic import op

revision = "0006_web_signup_password"
down_revision = "0005_users_purged_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pending_user_registrations "
        "ADD COLUMN requested_password_hash text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE pending_user_registrations "
        "DROP COLUMN requested_password_hash"
    )
