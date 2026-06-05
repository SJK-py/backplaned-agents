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
from pathlib import Path

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


def _ensure_state_dir(state_dir: Path) -> None:
    """Make the state dir exist and be OWNED by the bridge process.

    The dir holds the rotated service refresh token + one credentials.json
    per server agent. Persistence (`bp_sdk.onboarding`) does
    `state_dir.mkdir()` plus atomic file writes — all of which need the
    bridge to own the tree, because it runs as root WITH cap_drop: ALL (no
    CAP_DAC_OVERRIDE), so even uid 0 is subject to the directory's mode bits.

    A named volume that was first populated by an EARLIER image — before the
    `/mcp-state` mount point was made root-owned 0700 — keeps its original
    ownership (Docker seeds an empty volume's owner only once, on first
    mount). Root then can't `mkdir` the per-server subdir and the agent
    crashes with `EACCES` on `/mcp-state/<agent>`, consuming its single-use
    invitation and looping forever on `awaiting_invitation`.

    Running as root we hold CAP_CHOWN, so re-own the whole tree to self
    (top-down so a foreign 0700 subdir becomes traversable as we descend).
    Best-effort: each step is guarded so an already-correct or read-only dir
    is a no-op, never a boot failure."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if os.geteuid() != 0:
        return
    uid, gid = os.geteuid(), os.getegid()
    log = logging.getLogger("bp_mcp_bridge")
    try:
        os.chown(state_dir, uid, gid)
        os.chmod(state_dir, 0o700)
    except OSError as exc:
        log.warning(
            "mcp_bridge_state_dir_chown_failed",
            extra={
                "event": "mcp_bridge_state_dir_chown_failed",
                "path": str(state_dir),
                "error": repr(exc),
            },
        )
        return
    # Re-own any inherited per-server subdirs/files (top-down: each dir is
    # chowned to root before os.walk descends into it, so the dropped-uid
    # 0700 dirs from a stale volume become traversable).
    for root, dirs, files in os.walk(state_dir):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except OSError:  # noqa: PERF203 — best-effort per entry
                pass


async def _main() -> None:
    _configure_logging()
    try:
        config = SupervisorConfig.from_env()
    except RuntimeError as exc:
        sys.stderr.write(f"bp_mcp_bridge: {exc}\n")
        sys.exit(2)

    # Self-heal the state-dir ownership BEFORE anything tries to persist a
    # token or per-server credential (see _ensure_state_dir).
    _ensure_state_dir(config.state_dir)

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
        stdio_policy=config.stdio_policy,
    )
    try:
        await supervisor.run()
    finally:
        await admin_client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
