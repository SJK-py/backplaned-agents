"""bp_mcp_bridge — Backplane bridge for Model Context Protocol servers.

Phase 10c — multi-server, DB-driven. The `Supervisor` reads the
`mcp_servers` table via the router's admin API, reconciles
running `ServerBridge` instances against it, and forwards
`tools/call` ↔ `NewTaskFrame.payload` for each onboarded tool.

  * `config.SupervisorConfig` — env-loaded supervisor settings.
  * `admin_client.AdminClient` — thin httpx wrapper for
    `/v1/admin/mcp-servers` + invitation issuance.
  * `auth_resolver.resolve_auth_value` — resolves `env://VAR_NAME`
    refs. `secret://` deferred.
  * `mcp_client.StreamableHttpMcpClient` — minimal JSON-RPC 2.0
    client over Streamable HTTP. SSE: Phase 10d.
  * `tool_agent.build_server_agent` — projects one MCP server to
    ONE backplane `Agent` with one mode per MCP tool.
  * `server_bridge.ServerBridge` — orchestrates one MCP server's
    lifecycle (initialize, list_tools, onboard agent, reconcile
    modes via `Agent.set_modes` on `tools/list_changed`).
  * `supervisor.Supervisor` — multi-server orchestrator; reads
    config from the admin API, manages ServerBridge tasks.

Usage:
    BP_MCP_BRIDGE_ROUTER_URL=ws://router:8000/v1/agent \\
    BP_MCP_BRIDGE_ROUTER_ADMIN_URL=http://router:8000 \\
    BP_MCP_BRIDGE_ADMIN_TOKEN=<admin-jwt> \\
    BP_MCP_BRIDGE_STATE_DIR=/var/lib/bp_mcp_bridge \\
        python -m bp_mcp_bridge
"""

from __future__ import annotations

from bp_mcp_bridge.admin_client import AdminApiError, AdminClient
from bp_mcp_bridge.config import BridgeConfig, SupervisorConfig
from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow
from bp_mcp_bridge.supervisor import Supervisor

__all__ = [
    "AdminApiError",
    "AdminClient",
    "BridgeConfig",
    "ServerBridge",
    "ServerBridgeRow",
    "Supervisor",
    "SupervisorConfig",
]
