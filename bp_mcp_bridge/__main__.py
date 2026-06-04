"""`python -m bp_mcp_bridge` entrypoint.

Loads `SupervisorConfig` from environment variables, constructs
an `AdminClient` + `Supervisor`, and runs forever. The supervisor
polls the `mcp_servers` table via the admin API and manages one
`ServerBridge` per row.

Logs to stderr; level controllable via `BP_MCP_BRIDGE_LOG_LEVEL`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from bp_mcp_bridge import metrics
from bp_mcp_bridge.admin_client import AdminClient
from bp_mcp_bridge.config import SupervisorConfig
from bp_mcp_bridge.supervisor import Supervisor


def _configure_logging() -> None:
    level = os.environ.get("BP_MCP_BRIDGE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


async def _main() -> None:
    _configure_logging()
    try:
        config = SupervisorConfig.from_env()
    except RuntimeError as exc:
        sys.stderr.write(f"bp_mcp_bridge: {exc}\n")
        sys.exit(2)

    # Start the Prometheus exposition endpoint before the supervisor
    # loop so metrics are scrapeable from the first reconcile. Never
    # raises — a metrics-server failure is logged and the bridge
    # carries on without observability rather than failing to boot.
    metrics.start_metrics_server(config.metrics_port)

    admin_client = AdminClient(
        config.router_admin_url,
        refresh_token=config.service_secret,
        state_dir=config.state_dir,
    )
    supervisor = Supervisor(
        admin_client=admin_client,
        router_url=config.router_url,
        state_dir=config.state_dir,
        poll_interval_s=config.poll_interval_s,
    )
    try:
        await supervisor.run()
    finally:
        await admin_client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
