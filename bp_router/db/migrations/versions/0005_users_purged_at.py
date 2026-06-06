"""users: purged_at — permanent-delete (GDPR erasure) marker.

`soft_delete_user` sets `deleted_at` (deactivation, recoverable-ish). A
permanent `purge_user` goes further: it hard-deletes the user's content
(sessions/tasks/files), scrubs PII (`email`/`auth_secret_hash` → NULL), and
sets `purged_at`. The row itself is kept as a tombstone (8 `ON UPDATE CASCADE`
FKs reference it, and the append-only audit chain must stay intact), so
`purged_at` is the durable signal the suite reconcile loop keys off to erase
the user's suite-store rows + per-user LanceDB.
"""

from __future__ import annotations

from alembic import op

revision = "0005_users_purged_at"
down_revision = "0004_mcp_stdio_transport"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN purged_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN purged_at")
