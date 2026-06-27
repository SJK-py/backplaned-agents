"""custom_agents: v2 agent-loop columns.

Adds the optional agent-loop surface to `custom_agents` (additive; all
default off, so existing single-completion rows are unchanged):

  * `agent_loop_enabled` — run a bounded tool-use loop instead of one
    completion.
  * `max_rounds` — loop budget (1..16).
  * `file_access` — file-store tools given to the loop: none | read_only | full.
  * `peer_tools_enabled` — expose the ACL-visible peer agents as tools.

The loop itself runs in the bridge on bp_sdk primitives only. See
`docs/design/mcp-bridge-custom-llm-agents.md` §9.
"""

from __future__ import annotations

from alembic import op

revision = "0009_custom_agent_loop"
down_revision = "0008_custom_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE custom_agents
          ADD COLUMN agent_loop_enabled boolean NOT NULL DEFAULT false,
          ADD COLUMN max_rounds integer NOT NULL DEFAULT 4
              CHECK (max_rounds BETWEEN 1 AND 16),
          ADD COLUMN file_access text NOT NULL DEFAULT 'none'
              CHECK (file_access IN ('none', 'read_only', 'full')),
          ADD COLUMN peer_tools_enabled boolean NOT NULL DEFAULT false
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE custom_agents
          DROP COLUMN agent_loop_enabled,
          DROP COLUMN max_rounds,
          DROP COLUMN file_access,
          DROP COLUMN peer_tools_enabled
        """
    )
