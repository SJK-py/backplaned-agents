"""mcp_servers: stdio transport (command / args / env_refs).

Adds a third transport, `stdio`, where the bridge spawns a local subprocess
(e.g. `uvx some-mcp`) and speaks MCP over its stdin/stdout instead of
connecting to a URL.

  * `transport` CHECK gains `stdio`.
  * `url` becomes nullable (stdio has no URL).
  * `command` / `args` / `env_refs` describe the subprocess. `env_refs` is a
    JSON map `{ENV_NAME: "env://VAR" | "secret://..."}` resolved from the
    bridge's environment — never raw secrets in the table.
  * A transport-fields CHECK keeps the two shapes disjoint: stdio ⇒ command
    set + url null; sse/streamable_http ⇒ url set + command null.

The launcher allowlist (uvx/...) is enforced app-side (router + bridge), not in
the DB.
"""

from __future__ import annotations

from alembic import op

revision = "0004_mcp_stdio_transport"
down_revision = "0003_mcp_caps_disabled_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE mcp_servers ALTER COLUMN url DROP NOT NULL")
    op.execute(
        "ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_transport_check"
    )
    op.execute(
        "ALTER TABLE mcp_servers "
        "ADD COLUMN command text, "
        "ADD COLUMN args text[] NOT NULL DEFAULT '{}', "
        "ADD COLUMN env_refs jsonb NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE mcp_servers ADD CONSTRAINT mcp_servers_transport_check "
        "CHECK (transport IN ('sse', 'streamable_http', 'stdio'))"
    )
    op.execute(
        "ALTER TABLE mcp_servers ADD CONSTRAINT mcp_servers_transport_fields CHECK ("
        "  (transport = 'stdio' AND command IS NOT NULL AND url IS NULL)"
        "  OR (transport IN ('sse', 'streamable_http')"
        "      AND url IS NOT NULL AND command IS NULL)"
        ")"
    )


def downgrade() -> None:
    # stdio rows have a NULL url and would violate the re-added NOT NULL.
    op.execute("DELETE FROM mcp_servers WHERE transport = 'stdio'")
    op.execute(
        "ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_transport_fields"
    )
    op.execute(
        "ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_transport_check"
    )
    op.execute(
        "ALTER TABLE mcp_servers "
        "DROP COLUMN command, DROP COLUMN args, DROP COLUMN env_refs"
    )
    op.execute(
        "ALTER TABLE mcp_servers ADD CONSTRAINT mcp_servers_transport_check "
        "CHECK (transport IN ('sse', 'streamable_http'))"
    )
    op.execute("ALTER TABLE mcp_servers ALTER COLUMN url SET NOT NULL")
