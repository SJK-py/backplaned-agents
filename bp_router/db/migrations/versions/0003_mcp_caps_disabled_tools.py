"""mcp_servers: per-server capabilities + disabled tools.

Two admin-settable additions to an MCP server's row:

  * `capabilities` — extra agent capabilities merged into the per-server
    agent's auto-derived `mcp.bridge` / `mcp.tool.<tool>` set, so admins can
    tag a server for ACL targeting (agent-granular, like `groups`).
  * `disabled_tools` — tool names the bridge must NOT expose as modes (an
    on/off toggle per tool); the full tool list is still reported for the UI.

Both default to empty arrays.
"""

from __future__ import annotations

from alembic import op

revision = "0003_mcp_caps_disabled_tools"
down_revision = "0002_mcp_pending_invitation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE mcp_servers "
        "ADD COLUMN capabilities text[] NOT NULL DEFAULT '{}', "
        "ADD COLUMN disabled_tools text[] NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE mcp_servers "
        "DROP COLUMN capabilities, "
        "DROP COLUMN disabled_tools"
    )
