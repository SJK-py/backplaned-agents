"""One MCP server projected as ONE backplane agent (N modes).

One Agent per server, one mode per MCP tool — see
`docs/design/mcp-bridge-per-server-mode-per-tool.md`.

  * Per-row config (`ServerBridgeRow` — the relevant subset of an
    `mcp_servers` row, decoded for use here).
  * Shared `AdminClient` (multi-server bridge process has one,
    owned by the `Supervisor` and passed in).
  * Pluggable `auth_resolver.resolve_auth_value` for env://VAR
    indirection.
  * Callback to `admin_client.record_tools_refreshed(...)` after a
    successful upstream `tools/list`, so admin UI sees the tool
    count + `last_connected_at` update.

Lifecycle: build the per-server `Agent` once, spawn its `run_async()`
once, then watch for `tools/list_changed` (SSE push or admin-clicked
refresh) and reconcile via `Agent.set_modes(...)`. The same socket
serves every tool; mode add / remove / schema-update is one atomic
in-memory swap + one `AgentInfoUpdate` round-trip.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bp_mcp_bridge import metrics
from bp_mcp_bridge.admin_client import AdminClient
from bp_mcp_bridge.auth_resolver import resolve_auth_value
from bp_mcp_bridge.mcp_client import (
    SseMcpClient,
    StreamableHttpMcpClient,
    ToolDefinition,
    build_mcp_client,
)
from bp_mcp_bridge.tool_agent import (
    _build_capabilities,
    _server_description,
    agent_id_for_server,
    build_server_agent,
    make_tool_handler,
)
from bp_sdk import Agent

logger = logging.getLogger(__name__)


# Bound for the `aclose()` call in `run()`'s finally block. Without
# a timeout, a stuck HTTP request (slow upstream, dropped packets)
# OR a stuck SSE stream task can block `aclose()` forever — which
# in turn blocks the supervisor's `_stop()` from completing, which
# blocks the next reconcile pass from running. Five seconds is far
# longer than any healthy aclose needs (typically <100ms); if we
# hit it, we log a warning and let the underlying connection pool
# leak rather than wedge the supervisor.
_ACLOSE_TIMEOUT_S = 5.0

# Backoff before re-arming the refresh event after a failed
# `_reconcile_tools`. Prevents a hot spin when the upstream is
# persistently down (a set event makes `_refresh_event.wait()`
# return immediately, so without the sleep a hard-down server
# would busy-loop list_tools()). Short enough that a transient
# blip recovers within one cycle.
_RECONCILE_RETRY_BACKOFF_S = 5.0


@dataclass(frozen=True)
class ServerBridgeRow:
    """Subset of the `mcp_servers` row that the bridge cares about.

    Constructed from the JSON response of `GET /v1/admin/mcp-servers`;
    the supervisor adapts the row dict into this typed shape before
    handing it off."""

    server_id: str
    url: str
    transport: str
    auth_kind: str
    auth_value_ref: str | None
    auth_header_name: str | None
    groups: list[str]
    expose_to_llm: bool
    refresh_requested_at: str | None  # ISO timestamp or null
    # Admin-minted short-TTL invitation for first onboard / reconnect. Consumed
    # by the bridge; NOT part of config_signature (a mint must not restart a
    # healthy bridge).
    pending_invitation_token: str | None = None

    @classmethod
    def from_admin_dict(cls, row: dict[str, Any]) -> ServerBridgeRow:
        return cls(
            server_id=row["server_id"],
            url=row["url"],
            transport=row["transport"],
            auth_kind=row["auth_kind"],
            auth_value_ref=row.get("auth_value_ref"),
            auth_header_name=row.get("auth_header_name"),
            groups=list(row.get("groups") or []),
            expose_to_llm=bool(row.get("expose_to_llm", True)),
            refresh_requested_at=row.get("refresh_requested_at"),
            pending_invitation_token=row.get("pending_invitation_token"),
        )

    def config_signature(self) -> tuple:
        """Fields whose change requires a full bridge restart.
        Excludes `refresh_requested_at` (handled via the same
        restart flow, but signalled separately by the supervisor)."""
        return (
            self.url,
            self.transport,
            self.auth_kind,
            self.auth_value_ref,
            self.auth_header_name,
            tuple(self.groups),
            self.expose_to_llm,
        )


class ServerBridge:
    """One MCP server's runtime: MCP transport client + ONE backplane
    `Agent` (with one mode per tool).

    Construction is cheap; `run()` does the work — initialise the
    MCP client, list tools, build + onboard the per-server agent,
    then loop watching for refresh signals and reconciling the
    mode set.

    The supervisor spawns `run()` as an asyncio task and cancels it
    to tear down.

    Reconciliation on `refresh_requested_at` / SSE
    `tools/list_changed`:

      * Added / removed / changed tools → one `agent.set_modes(...)`
        call (full mode dict + accepts_schema), atomic in-process
        swap, single `AgentInfoUpdate` round-trip to the router.
      * Capability list / description rebuild → one
        `agent.update_info(capabilities=..., description=...)` call
        if the capability set changed. Brief window where the
        catalog has new tools + old capabilities (≤ one ack RTT) is
        accepted: capability-pattern ACL rules (`mcp.tool.*`) match
        either way; exact-capability rules (`mcp.tool.search_users`)
        see at-most-one-RTT staleness.
    """

    def __init__(
        self,
        row: ServerBridgeRow,
        *,
        admin_client: AdminClient,
        router_url: str,
        state_dir: Path,
    ) -> None:
        self._row = row
        self._admin_client = admin_client
        self._router_url = router_url
        self._state_dir = state_dir
        # Either transport client; both expose the same surface
        # (initialize / list_tools / call_tool / aclose).
        self._mcp_client: StreamableHttpMcpClient | SseMcpClient | None = None
        # The single per-server backplane agent + its run_async task.
        # Both are populated by `run()` after the initial tools/list
        # so the Agent is built with the operator-pinned schemas.
        self._agent: Agent | None = None
        self._agent_task: asyncio.Task[None] | None = None
        # The currently-published tool list. Kept so `_reconcile`
        # can skip the broadcast when nothing changed (a refresh
        # signal that finds no diff) and can rebuild capabilities
        # only when the set of tool *names* moves.
        self._known_tools: list[ToolDefinition] = []
        self._refresh_event = asyncio.Event()

    def trigger_refresh(self) -> None:
        """Signal the bridge to re-fetch tools/list and reconcile.

        Idempotent — multiple calls before the bridge picks up
        the signal coalesce into one reconcile pass. Safe to call
        from any context (no async; just sets an asyncio.Event)."""
        self._refresh_event.set()

    async def run(self) -> None:
        """Initialise MCP, list tools, onboard the per-server agent,
        then run forever — watching for refresh signals and
        reconciling the mode set. Returns only on cancellation."""
        # Don't touch the upstream MCP server until we can actually onboard the
        # agent. With no persisted creds AND no admin-minted invitation, wait
        # for an admin to (re)connect this server — the supervisor respawns us
        # next poll, by which point a reconnect may have stashed a token.
        if not self._can_onboard():
            logger.info(
                "mcp_server_bridge_awaiting_invitation",
                extra={
                    "event": "mcp_server_bridge_awaiting_invitation",
                    "bp.mcp_server_id": self._row.server_id,
                },
            )
            return
        auth_value = resolve_auth_value(self._row.auth_value_ref)
        # SSE clients gain a tools/list_changed callback so server-
        # pushed tool changes trigger the same reconcile path as
        # admin-driven refreshes. Streamable HTTP has no equivalent
        # push channel; only polling/admin work.
        on_tools_changed = self.trigger_refresh
        self._mcp_client = build_mcp_client(
            self._row.transport,
            self._row.url,
            auth_kind=self._row.auth_kind,
            auth_value=auth_value,
            auth_header_name=self._row.auth_header_name,
            on_tools_changed=on_tools_changed,
            server_id=self._row.server_id,
        )
        metrics.bridge_starts_total.labels(
            server_id=self._row.server_id,
        ).inc()
        exit_reason = "returned"
        try:
            await self._mcp_client.initialize()
            tools = await self._mcp_client.list_tools()
            logger.info(
                "mcp_server_bridge_tools_listed",
                extra={
                    "event": "mcp_server_bridge_tools_listed",
                    "bp.mcp_server_id": self._row.server_id,
                    "count": len(tools),
                },
            )
            # Onboard the agent BEFORE publishing the catalog /
            # stamping the row healthy. `_record_tools_refreshed`
            # writes `tools_cache`, stamps `last_connected_at=now()`,
            # and clears `refresh_requested_at`. If it ran first and
            # `_spawn_agent` then failed (a self-issued invitation
            # raising, the router rejecting onboarding), the bridge
            # task would die while the `mcp_servers` row advertises
            # a healthy server with N tools and ZERO running agents
            # — every tool call routes to nothing and the admin UI
            # shows no problem.
            await self._spawn_agent(tools)
            await self._record_tools_refreshed(tools)
            await self._race_refresh_against_agent()
        except asyncio.CancelledError:
            exit_reason = "cancelled"
            raise
        except Exception:
            exit_reason = "error"
            raise
        finally:
            metrics.bridge_exits_total.labels(
                server_id=self._row.server_id, reason=exit_reason,
            ).inc()
            await self._tear_down_agent()
            await self._close_mcp_client_bounded()
            self._mcp_client = None

    async def _close_mcp_client_bounded(self) -> None:
        """Call `aclose()` on the MCP client with a hard timeout.

        Without this bound, a stuck upstream (slow socket, dropped
        packets, hung SSE stream task) blocks the bridge's `finally`,
        which blocks the supervisor's `_stop()` `await task`, which
        blocks the next reconcile pass from running. The supervisor
        ends up wedged for as long as the dead upstream stays in
        that state — possibly indefinitely.

        On timeout we log loudly and let the underlying `httpx.AsyncClient`
        leak. That leaks one connection pool per stuck server; the
        next reconcile cycle creates a fresh bridge with a fresh
        client. Operators see `mcp_server_bridge_aclose_timeout` in
        logs and know the upstream is unhealthy.

        Note: `asyncio.shield()` would NOT help here — the supervisor
        is *waiting* on us, so shielding our cancellation just defers
        the same wedge. The timeout *abandons* the aclose entirely.
        """
        if self._mcp_client is None:
            return
        try:
            await asyncio.wait_for(
                self._mcp_client.aclose(), timeout=_ACLOSE_TIMEOUT_S,
            )
        except TimeoutError:
            metrics.aclose_timeouts_total.labels(
                server_id=self._row.server_id,
            ).inc()
            logger.warning(
                "mcp_server_bridge_aclose_timeout",
                extra={
                    "event": "mcp_server_bridge_aclose_timeout",
                    "bp.mcp_server_id": self._row.server_id,
                    "timeout_s": _ACLOSE_TIMEOUT_S,
                },
            )
        except Exception:  # noqa: BLE001
            # aclose() itself raised something other than timeout
            # (broken pipe, double-close, etc.). Log and move on —
            # the supervisor cares about progress, not perfection.
            logger.exception(
                "mcp_server_bridge_aclose_error",
                extra={
                    "event": "mcp_server_bridge_aclose_error",
                    "bp.mcp_server_id": self._row.server_id,
                },
            )

    async def _race_refresh_against_agent(self) -> None:
        """Run `_refresh_loop` and the per-server agent task
        concurrently; exit (with the agent's exception, if any) as
        soon as either finishes.

        The refresh loop normally runs forever — it returns only on
        cancellation. The agent task is supposed to do the same, but
        `Agent.run_async` can exit abnormally (transport permanently
        failed, unhandled handler exception bubbling out). Without
        this race the bridge would happily keep awaiting refresh
        signals against a corpse — every reconcile would mutate
        in-memory state and then fail on `update_info` because the
        dispatcher's transport is closed. The supervisor watches the
        bridge task, not the inner agent task, so it can't see the
        agent died.

        Surfacing the agent's exit cause as the bridge's exit cause
        makes the supervisor's restart logic apply correctly: the
        bridge task ends with whatever killed the agent, the
        supervisor catches it, restarts the bridge, fresh agent."""
        assert self._agent_task is not None
        refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name=f"mcp_refresh_loop:{self._row.server_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {refresh_task, self._agent_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Surface the first completed task's exception (typically
            # the agent dying). If both completed (rare race on
            # supervisor cancellation), the agent's takes precedence
            # because it carries the diagnostic operators care about.
            if self._agent_task in done:
                self._agent_task.result()
            for task in done:
                task.result()
        finally:
            # Cancel whichever task is still running. `_tear_down_agent`
            # in the outer `run()` finally also cancels `_agent_task`,
            # which is idempotent.
            if not refresh_task.done():
                refresh_task.cancel()
                try:
                    await refresh_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _refresh_loop(self) -> None:
        """Wait for refresh signals, reconcile, repeat. The signal
        comes from the supervisor (admin "Refresh tools" click on
        the UI) OR from an SSE `tools/list_changed` notification."""
        while True:
            await self._refresh_event.wait()
            self._refresh_event.clear()
            try:
                await self._reconcile_tools()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mcp_server_bridge_reconcile_failed",
                    extra={
                        "event": "mcp_server_bridge_reconcile_failed",
                        "bp.mcp_server_id": self._row.server_id,
                    },
                )
                # The event was cleared BEFORE the reconcile ran. If
                # `_reconcile_tools` raised (a transient
                # `list_tools()` failure — network blip, -32603, an
                # SSE reconnect window), the refresh request is now
                # LOST: added/removed tools never reconcile until the
                # next admin click or SSE push, which for a
                # streamable_http server may never come. Re-arm the
                # event so the reconcile retries. The short sleep
                # before re-arming prevents a hot spin when the
                # upstream is persistently down (the wait() returns
                # immediately on a set event).
                await asyncio.sleep(_RECONCILE_RETRY_BACKOFF_S)
                self._refresh_event.set()

    async def _reconcile_tools(self) -> None:
        """Re-fetch tools/list and replace the agent's mode set in
        one `set_modes` call. Writes the new tools_cache to the
        router on success.

        Adds / removes / schema-only changes all flow through this
        single path. A schema-only change is just a different
        schema for the same mode name; an add is a new mode entry;
        a remove is an absent entry. The SDK's `set_modes` is the
        diff loop — the bridge doesn't carry one of its own.
        """
        assert self._mcp_client is not None
        assert self._agent is not None
        new_tools = await self._mcp_client.list_tools()
        await self._apply_tools(new_tools)
        await self._record_tools_refreshed(new_tools)
        logger.info(
            "mcp_server_bridge_reconciled",
            extra={
                "event": "mcp_server_bridge_reconciled",
                "bp.mcp_server_id": self._row.server_id,
                "tool_count": len(new_tools),
            },
        )

    async def _apply_tools(self, new_tools: list[ToolDefinition]) -> None:
        """Push `new_tools` to the per-server agent as a mode set
        replacement + (if needed) a capability/description refresh.

        Two writes: `set_modes` for the mode dict + accepts_schema
        (always); `update_info` for capabilities + description (only
        if the tool-name set changed — capabilities are derived from
        names, so a pure schema change leaves them alone). Skipping
        the second call avoids a spurious AgentInfoUpdate round-trip
        when the upstream returns the same N tools with the same
        names.
        """
        assert self._agent is not None
        old_names = {t.name for t in self._known_tools}
        new_names = {t.name for t in new_tools}
        added = new_names - old_names
        removed = old_names - new_names

        await self._agent.set_modes({
            t.name: (
                make_tool_handler(
                    self._mcp_client, t.name, self._row.server_id,  # type: ignore[arg-type]
                ),
                t.input_schema,
            )
            for t in new_tools
        })

        # Capability set + description follow the tool *name* set.
        # A schema-only change (same name, new shape) leaves them
        # alone; only an add/remove triggers the update_info call.
        if added or removed:
            await self._update_capabilities_and_description(new_tools)

        if added:
            metrics.tool_reconcile_changes_total.labels(
                server_id=self._row.server_id, change="added",
            ).inc(len(added))
        if removed:
            metrics.tool_reconcile_changes_total.labels(
                server_id=self._row.server_id, change="removed",
            ).inc(len(removed))
        # Schema-only-change count. Tools present in both sets whose
        # input_schema differs are the "schema_changed" bucket; an
        # operator watching the metric distinguishes "shape moved"
        # from "tool added/removed".
        present = old_names & new_names
        old_by_name = {t.name: t for t in self._known_tools}
        new_by_name = {t.name: t for t in new_tools}
        schema_changed = sum(
            1 for n in present
            if old_by_name[n].input_schema != new_by_name[n].input_schema
        )
        if schema_changed:
            metrics.tool_reconcile_changes_total.labels(
                server_id=self._row.server_id, change="schema_changed",
            ).inc(schema_changed)

        self._known_tools = list(new_tools)

    async def _update_capabilities_and_description(
        self, tools: list[ToolDefinition]
    ) -> None:
        """Push capability + description refresh as a separate
        `AgentInfoUpdate`. Description follows the server_id +
        transport (rarely changes); capabilities follow the tool
        names (changes whenever a tool is added/removed)."""
        assert self._agent is not None
        try:
            await self._agent.update_info(
                capabilities=_build_capabilities(tools),
                description=_server_description(
                    self._row.server_id, self._row.transport,
                ),
            )
        except Exception:  # noqa: BLE001
            # Best-effort: the modes were already replaced. A failed
            # capability refresh leaves the catalog with the old
            # cap list — capability-PATTERN ACL rules (mcp.tool.*)
            # still match. Operators see the warning in logs and
            # the next reconcile retries.
            logger.warning(
                "mcp_server_bridge_capability_refresh_failed",
                extra={
                    "event": "mcp_server_bridge_capability_refresh_failed",
                    "bp.mcp_server_id": self._row.server_id,
                },
                exc_info=True,
            )

    async def _spawn_agent(self, tools: list[ToolDefinition]) -> None:
        """Build the per-server agent from the initial tool list
        and start its `run_async` task. Onboarding (invitation
        issuance + WS handshake) happens inside `run_async` before
        the agent accepts NewTaskFrames.

        A zero-tool server still gets an agent — it onboards and
        sits idle until the first `tools/list_changed` populates
        modes. This keeps the bridge healthy through a temporarily-
        empty upstream rather than crashing the supervisor.
        """
        assert self._mcp_client is not None
        invitation = self._onboarding_invitation()
        self._agent = build_server_agent(
            self._to_bridge_config(),
            self._mcp_client,
            tools,
            invitation,
        )
        # Race-recovery hook (see `_resync_info_on_connect` docstring).
        # The agent runs its on_startup hooks after the dispatcher is
        # built — the earliest point at which `update_info` actually
        # broadcasts. Anything `set_modes` had to silently swallow
        # during onboarding gets republished here.
        self._agent.on_startup(self._resync_info_on_connect)
        self._known_tools = list(tools)
        self._agent_task = asyncio.create_task(
            self._agent.run_async(),
            name=f"mcp_server_agent:{self._agent.info.agent_id}",
        )

    async def _resync_info_on_connect(self) -> None:
        """Re-broadcast the agent's current `accepts_schema` /
        `non_tool_modes` / `capabilities` once the WS is up.

        Why: `set_modes` calls that fire while the agent is still
        onboarding (between `onboard_or_resume` and
        `build_dispatcher`) mutate `self.info` in-memory but return
        without broadcasting (the `_dispatcher is None` guard). The
        agent's onboard POST registered the snapshot at that moment;
        the router's WS handshake handler IGNORES `HelloFrame.agent_info`
        on connect — so any drift the bridge accumulated during
        onboarding never reaches the catalog without an explicit
        AgentInfoUpdate.

        The window is narrow (the duration of the WS handshake +
        first tools/list_changed) but real on SSE servers that emit
        notifications during the bridge's cold start. This hook
        closes it idempotently: if no drift, the router applies the
        same values it already has."""
        if self._agent is None:
            return
        try:
            await self._agent.update_info(
                accepts_schema=dict(self._agent.info.accepts_schema or {}),
                non_tool_modes=list(self._agent.info.non_tool_modes or []),
                capabilities=list(self._agent.info.capabilities or []),
            )
        except Exception:  # noqa: BLE001
            # Non-fatal — the agent is still connected. Operators
            # see the warning; the next reconcile will re-broadcast
            # if anything actually drifted.
            logger.warning(
                "mcp_server_bridge_resync_failed",
                extra={
                    "event": "mcp_server_bridge_resync_failed",
                    "bp.mcp_server_id": self._row.server_id,
                },
                exc_info=True,
            )

    async def _tear_down_agent(self) -> None:
        """Cancel the per-server agent task. Called from `run()`'s
        finally block on shutdown / supervisor cancellation. Router's
        WS-disconnect path handles the catalog eviction."""
        if self._agent_task is None:
            return
        self._agent_task.cancel()
        try:
            await self._agent_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception(
                "mcp_server_agent_teardown_error",
                extra={
                    "event": "mcp_server_agent_teardown_error",
                    "bp.mcp_server_id": self._row.server_id,
                },
            )
        finally:
            self._agent_task = None
            self._agent = None

    async def _record_tools_refreshed(self, tools: list[ToolDefinition]) -> None:
        """Write the latest tool list back to the `mcp_servers`
        row. Atomically updates `tools_cache`, stamps
        `last_connected_at = now()`, and clears
        `refresh_requested_at`."""
        tools_cache = {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in tools
            ],
        }
        try:
            await self._admin_client.record_tools_refreshed(
                self._row.server_id, tools_cache=tools_cache,
            )
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — the agent is still onboarded. Operators
            # see a stale tools_cache in the admin UI until the next
            # successful write.
            logger.warning(
                "mcp_tools_refreshed_write_failed",
                extra={
                    "event": "mcp_tools_refreshed_write_failed",
                    "bp.mcp_server_id": self._row.server_id,
                    "error": repr(exc),
                },
            )

    def _to_bridge_config(self):  # type: ignore[no-untyped-def]
        """Adapter for `build_server_agent`, which still expects a
        BridgeConfig-shaped object. Keep this seam thin: only the
        fields the factory consumes need to be passed through."""
        from bp_mcp_bridge.config import BridgeConfig  # noqa: PLC0415
        return BridgeConfig(
            server_id=self._row.server_id,
            url=self._row.url,
            transport=self._row.transport,
            auth_kind=self._row.auth_kind,
            auth_value=resolve_auth_value(self._row.auth_value_ref),
            auth_header_name=self._row.auth_header_name,
            groups=self._row.groups,
            expose_to_llm=self._row.expose_to_llm,
            router_url=self._router_url,
            router_admin_url="",  # not used by build_server_agent
            admin_token=None,
            state_dir=self._state_dir,
        )

    def _creds_path(self) -> Path:
        return self._state_dir / agent_id_for_server(self._row.server_id) / "credentials.json"

    def _can_onboard(self) -> bool:
        """True if the agent can connect: it has persisted creds to resume, or
        an admin-minted invitation to onboard with. When neither holds, the
        bridge waits (an admin must connect/reconnect the server) rather than
        connecting to the upstream MCP server for nothing."""
        return self._creds_path().exists() or bool(self._row.pending_invitation_token)

    def _onboarding_invitation(self) -> str:
        """The invitation token used to onboard the per-server agent.

        The bridge no longer self-mints (that needed the invitation-mint
        capability, i.e. admin). Instead an admin action (create / reconnect)
        stashes a short-TTL invitation on the `mcp_servers` row, which arrives
        here via `ServerBridgeRow.pending_invitation_token`. Subsequent runs
        resume from the persisted `credentials.json`; the SDK ignores the
        invitation when `auth_token` is loaded, so "" is returned then."""
        if self._creds_path().exists():
            return ""
        return self._row.pending_invitation_token or ""
