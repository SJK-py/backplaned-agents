"""invitations: an optional agent-name roster on a single token

Implements `docs/design/deployment-agent-host.md` §3.

Today a deployment mints one single-use invitation per agent — twelve
mandatory env vars, minted fresh on every launch because the launcher cannot
know whether the state volumes survived. And because `invitations` has no
`agent_id` column and `POST /v1/onboard` takes the name from the agent's own
`agent_info`, each of those twelve is an **unbound bearer credential**: any
one of them can onboard as any agent name.

A roster fixes both. `agent_ids` lists the names one token may produce;
`consumed` records which have been taken. The token stays live until the
roster is exhausted, so one host process can onboard the group it runs, and
a partially-provisioned group heals on restart (agents holding credentials
skip; agents without take their slot).

NULL `agent_ids` keeps the existing unbound single-use behaviour exactly, so
this is additive for every current caller.
"""

from __future__ import annotations

from alembic import op

revision = "0012_invitation_roster"
down_revision = "0011_user_llm_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE invitations
          ADD COLUMN agent_ids text[],
          ADD COLUMN consumed  text[] NOT NULL DEFAULT '{}'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE invitations
          DROP COLUMN agent_ids,
          DROP COLUMN consumed
        """
    )
