"""Multi-server bridge supervisor.

Reads the `mcp_servers` table via the router's admin API,
reconciles desired vs. running ServerBridge instances, and
manages their lifecycles. One process can bridge many MCP
servers — operators run separate supervisors only if they want
process-level isolation.

Reconciliation loop:

  1. Poll `GET /v1/admin/mcp-servers` every `poll_interval_s` (default 30s).
  2. Diff against the in-memory `_active` map keyed by server_id.
  3. New server_ids → spawn ServerBridge tasks.
  4. Removed server_ids → cancel their tasks.
  5. Changed config (URL / transport / auth / groups / expose) →
     restart (cancel + respawn). Brief outage acceptable.
  6. Same config but refresh_requested_at moved forward → restart
     (Phase 10c uses restart-based refresh; 10d will reconcile
     incrementally without dropping the running agents).

Cancellation rolls up cleanly: SIGINT / SIGTERM at the process
level cancels the supervisor's main task, which cancels every
ServerBridge task, which cancels per-agent run loops in turn.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from bp_mcp_bridge import metrics
from bp_mcp_bridge.admin_client import AdminApiError, AdminClient
from bp_mcp_bridge.config import StdioPolicy
from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ActiveEntry:
    """One running ServerBridge — task + ServerBridge handle + the
    row config that spawned it. Stored in the supervisor's `_active`
    map keyed by server_id.

    Phase 10f added `bridge` so the supervisor can call
    `trigger_refresh()` directly instead of restarting the task.
    `task` is still tracked for cancellation on config-change
    restart and shutdown teardown."""

    task: asyncio.Task
    bridge: ServerBridge
    row: ServerBridgeRow


class Supervisor:
    """Process-level orchestrator. One instance per process; reads
    the full `mcp_servers` table from one router."""

    def __init__(
        self,
        *,
        admin_client: AdminClient,
        router_url: str,
        state_dir: Path,
        poll_interval_s: float = 30.0,
        stdio_policy: StdioPolicy | None = None,
    ) -> None:
        self._admin_client = admin_client
        self._router_url = router_url
        self._state_dir = state_dir
        self._poll_interval_s = poll_interval_s
        self._stdio_policy = stdio_policy or StdioPolicy()
        self._active: dict[str, _ActiveEntry] = {}

    async def run(self) -> None:
        """Run forever — reconcile loop + ServerBridge tasks share
        the same asyncio context. Returns only on cancellation."""
        logger.info(
            "mcp_supervisor_starting",
            extra={
                "event": "mcp_supervisor_starting",
                "poll_interval_s": self._poll_interval_s,
            },
        )
        try:
            while True:
                try:
                    await self._reconcile_once()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "mcp_supervisor_reconcile_failed",
                        extra={"event": "mcp_supervisor_reconcile_failed"},
                    )
                await asyncio.sleep(self._poll_interval_s)
        finally:
            await self._tear_down_all()

    async def _reconcile_once(self) -> None:
        try:
            rows_raw = await self._admin_client.list_mcp_servers()
        except AdminApiError as exc:
            # NB: `message` is a reserved LogRecord attribute — use
            # a different key for the API error detail.
            logger.warning(
                "mcp_supervisor_list_failed",
                extra={
                    "event": "mcp_supervisor_list_failed",
                    "status_code": exc.status_code,
                    "detail": exc.message,
                },
            )
            return
        desired = {
            r["server_id"]: ServerBridgeRow.from_admin_dict(r)
            for r in rows_raw
        }

        # Tear down servers that disappeared from the table.
        for sid in list(self._active.keys()):
            if sid not in desired:
                logger.info(
                    "mcp_server_bridge_removed",
                    extra={
                        "event": "mcp_server_bridge_removed",
                        "bp.mcp_server_id": sid,
                    },
                )
                await self._stop(sid)

        # Spawn new + handle config / refresh changes.
        for sid, row in desired.items():
            existing = self._active.get(sid)
            if existing is None:
                self._start(row)
                continue
            if existing.row.config_signature() != row.config_signature():
                logger.info(
                    "mcp_server_bridge_config_changed",
                    extra={
                        "event": "mcp_server_bridge_config_changed",
                        "bp.mcp_server_id": sid,
                    },
                )
                await self._stop(sid)
                self._start(row)
                continue
            if self._refresh_advanced(existing.row, row):
                logger.info(
                    "mcp_server_bridge_refresh_requested",
                    extra={
                        "event": "mcp_server_bridge_refresh_requested",
                        "bp.mcp_server_id": sid,
                    },
                )
                # Phase 10f: signal incrementally instead of restart.
                # The bridge stays connected; only changed/added/
                # removed tools see disruption.
                existing.bridge.trigger_refresh()
                self._active[sid] = _ActiveEntry(
                    task=existing.task, bridge=existing.bridge, row=row,
                )
                continue
            # No-op: same config signature, no refresh pending. The
            # full row may carry non-acted-on drift (description,
            # last_connected_at). Only rebuild the entry if it
            # actually differs, so the steady-state reconcile pass
            # is alloc-free.
            if existing.row != row:
                self._active[sid] = _ActiveEntry(
                    task=existing.task, bridge=existing.bridge, row=row,
                )

        # Authoritative point to publish the running-bridge gauge —
        # runs every poll cycle, after every add/remove this pass
        # applied. A gauge that's at most one poll-interval stale is
        # fine for a "how many bridges are up" indicator. No
        # `.labels()` — `active_bridges` is unlabelled, and real
        # prometheus rejects `.labels()` on a no-label metric.
        metrics.active_bridges.set(len(self._active))

    @staticmethod
    def _refresh_advanced(
        prev: ServerBridgeRow, new: ServerBridgeRow
    ) -> bool:
        """True iff `refresh_requested_at` moved from null/old to
        a new non-null value. Equal timestamps mean we've already
        handled this refresh request (restart cleared it server-
        side; the row's prev value reflects what we'd already
        observed when we restarted)."""
        if new.refresh_requested_at is None:
            return False
        if prev.refresh_requested_at is None:
            return True
        return new.refresh_requested_at > prev.refresh_requested_at

    def _start(self, row: ServerBridgeRow) -> None:
        bridge = ServerBridge(
            row,
            admin_client=self._admin_client,
            router_url=self._router_url,
            state_dir=self._state_dir,
            stdio_policy=self._stdio_policy,
        )
        task = asyncio.create_task(
            bridge.run(),
            name=f"mcp_server_bridge:{row.server_id}",
        )
        # `_on_bridge_done` logs the exit and evicts the dead entry
        # from `_active` so the next reconcile pass respawns.
        task.add_done_callback(self._on_bridge_done)
        self._active[row.server_id] = _ActiveEntry(
            task=task, bridge=bridge, row=row,
        )
        logger.info(
            "mcp_server_bridge_started",
            extra={
                "event": "mcp_server_bridge_started",
                "bp.mcp_server_id": row.server_id,
                "url": row.url,
                "transport": row.transport,
            },
        )

    def _on_bridge_done(self, task: asyncio.Task) -> None:
        # Evict the dead entry so the next reconcile pass respawns.
        # `task.get_name()` is `mcp_server_bridge:{server_id}` —
        # parse the suffix to find the slot. A NoneType compare
        # (slot already replaced by an explicit `_stop`/restart) is
        # the no-op path.
        name = task.get_name()
        prefix = "mcp_server_bridge:"
        if name.startswith(prefix):
            server_id = name[len(prefix):]
            current = self._active.get(server_id)
            if current is not None and current.task is task:
                # Only evict if this is STILL the active entry for
                # this server_id. A restart in progress may have
                # already replaced the slot — leaving the new entry
                # alone is the correct behaviour.
                self._active.pop(server_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "mcp_server_bridge_exited",
                extra={
                    "event": "mcp_server_bridge_exited",
                    "task_name": task.get_name(),
                },
                exc_info=exc,
            )

    async def _stop(self, server_id: str) -> None:
        entry = self._active.pop(server_id, None)
        if entry is None:
            return
        entry.task.cancel()
        try:
            await entry.task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception(
                "mcp_server_bridge_stop_error",
                extra={
                    "event": "mcp_server_bridge_stop_error",
                    "bp.mcp_server_id": server_id,
                },
            )

    async def _tear_down_all(self) -> None:
        """Cancel every running ServerBridge. Called from the
        supervisor's finally block on shutdown."""
        for sid in list(self._active.keys()):
            await self._stop(sid)
