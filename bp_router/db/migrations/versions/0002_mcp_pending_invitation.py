"""mcp_servers: pending invitation token for bridge onboarding.

The MCP bridge no longer self-mints invitations (that needed full admin).
Instead an admin action (create / reconnect) mints a short-TTL service
invitation and stashes it on the server's row; the bridge consumes it on its
next poll to onboard the `mcp_<server>` agent, then it's cleared once the agent
connects (record_mcp_server_tools_refreshed). Both columns are nullable and
transient.
"""

from __future__ import annotations

from alembic import op

revision = "0002_mcp_pending_invitation"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE mcp_servers "
        "ADD COLUMN pending_invitation_token text, "
        "ADD COLUMN pending_invitation_expires_at timestamptz"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE mcp_servers "
        "DROP COLUMN pending_invitation_token, "
        "DROP COLUMN pending_invitation_expires_at"
    )
