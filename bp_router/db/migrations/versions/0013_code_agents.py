"""code_agents: operator-authored Python functions bridged onto the backplane.

A THIRD kind of bridge-provisioned agent, alongside `mcp_servers` and
`custom_agents`. An operator authors a Python function in the admin UI; the
bridge stands up one single-mode backplane `Agent` (`code_<slug>`) whose
handler runs that function in a uid-dropped subprocess.

Why a separate table rather than a `kind` column on `custom_agents`:
`custom_agents.preset_name` is `NOT NULL REFERENCES llm_presets(name)`, and a
discriminator would force it nullable — dropping a real constraint on every
existing LLM row to accommodate a kind that will never pick a preset. The two
kinds share about six columns and differ in about eight; what they share is
CODE, not schema (`bp_mcp_bridge/agent_common.py`).

Shape notes that are load-bearing:

  * `agent_id` is the full backplane id and PK — `code_<slug>`.
  * `parameters` is a JSON list of `{name, type, description, required}`.
    Unlike `custom_agents`, `type` is a real JSON-Schema type: this kind's
    handler receives a dict, so the string-only rule (which exists for
    `$`-prompt-templating safety) does not transfer.
  * `secret_refs` holds `env://VAR` REFERENCES, never literal secrets — the
    same posture as `mcp_servers.auth_value_ref`, enforced by the admin API.
  * `timeout_s` / `memory_mb` are the bridge's inner bounds on one call; the
    router's task deadline is the outer one.
  * There is deliberately NO `network` column. Per-agent egress control needs
    CAP_NET_ADMIN, which the bridge does not have and should not get, so a
    column here would read as a guarantee it cannot make. See
    `docs/design/bridge-python-code-agents.md` §3.4.
  * Provisioning mirrors `mcp_servers` / `custom_agents` exactly: a short-TTL
    `pending_invitation_token` the bridge consumes on its next poll.
"""

from __future__ import annotations

from alembic import op

revision = "0013_code_agents"
down_revision = "0012_invitation_roster"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE code_agents (
            agent_id      text PRIMARY KEY
                          CHECK (agent_id ~ '^code_[a-z][a-z0-9_]*$'),
            description   text NOT NULL DEFAULT '',
            code          text NOT NULL DEFAULT '',
            entrypoint    text NOT NULL DEFAULT 'run'
                          CHECK (entrypoint ~ '^[a-z_][a-z0-9_]*$'),
            parameters    jsonb NOT NULL DEFAULT '[]'::jsonb,
            returns       jsonb,
            secret_refs   jsonb NOT NULL DEFAULT '{}'::jsonb,
            timeout_s     integer NOT NULL DEFAULT 30
                          CHECK (timeout_s BETWEEN 1 AND 300),
            memory_mb     integer NOT NULL DEFAULT 512
                          CHECK (memory_mb BETWEEN 64 AND 4096),
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
    op.execute("DROP TABLE IF EXISTS code_agents")
