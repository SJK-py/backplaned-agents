"""custom_agents: operator-defined LLM-backed agents bridged onto the backplane.

A second kind of bridge-provisioned agent (alongside `mcp_servers`): an operator
authors a system prompt, a user-prompt template, a list of string parameters and
a model preset in the admin UI, and the bridge stands up one single-mode backplane
`Agent` (`custom_<id>`) whose handler runs an LLM completion instead of forwarding
to an MCP `tools/call`.

  * `agent_id` is the full backplane id and PK — `custom_<slug>`.
  * `preset_name` FKs `llm_presets.name` — a referenced preset can't be dropped.
  * `parameters` is a JSON list of `{name, description, required}`; all params are
    type "string" (v1). The names are the single mode's `accepts_schema` keys and
    the `$name` substitution keys in the prompts.
  * Provisioning mirrors `mcp_servers`: a short-TTL `pending_invitation_token` the
    bridge consumes on its next poll, cleared once it connects.

See `docs/design/mcp-bridge-custom-llm-agents.md`.
"""

from __future__ import annotations

from alembic import op

revision = "0008_custom_agents"
down_revision = "0007_user_oidc_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE custom_agents (
            agent_id      text PRIMARY KEY
                          CHECK (agent_id ~ '^custom_[a-z][a-z0-9_]*$'),
            description   text NOT NULL DEFAULT '',
            preset_name   text NOT NULL REFERENCES llm_presets(name),
            system_prompt text NOT NULL DEFAULT '',
            user_prompt   text NOT NULL DEFAULT '',
            parameters    jsonb NOT NULL DEFAULT '[]'::jsonb,
            groups        jsonb NOT NULL DEFAULT '[]'::jsonb,
            capabilities  jsonb NOT NULL DEFAULT '[]'::jsonb,
            expose_to_llm boolean NOT NULL DEFAULT true,
            output_as_file boolean NOT NULL DEFAULT false,
            enabled       boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            created_by    text REFERENCES users(user_id),
            pending_invitation_token      text,
            pending_invitation_expires_at timestamptz
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS custom_agents")
