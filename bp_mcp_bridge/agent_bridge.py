"""The shared runtime for a bridge-hosted non-MCP agent.

`CustomAgentBridge` and `CodeAgentBridge` are the same object with a
different `Agent` inside: build it, onboard it, keep it connected until
cancelled, and tell the router once it is up. Only three things differ per
kind — how the `Agent` is built, which admin endpoint records the connect,
and the metric label — so those are the three hooks a subclass fills in.

Far simpler than `ServerBridge`: there is no upstream client, no
`tools/list`, no `tools/list_changed` reconcile. The supervisor restarts one
of these only when the row's `config_signature` changes (an admin edit); on
restart it resumes from persisted credentials, so no fresh invitation is
needed for edits.

See `docs/design/bridge-python-code-agents.md` §11.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bp_mcp_bridge import metrics
from bp_mcp_bridge.admin_client import AdminClient
from bp_sdk import Agent

logger = logging.getLogger(__name__)


class AgentBridgeBase:
    """One hosted agent's runtime. The supervisor spawns `run()` as a task
    and cancels it to tear down."""

    #: `kind` label for the shared non-MCP metrics, and the log-field prefix.
    KIND = "agent"

    def __init__(
        self,
        *,
        agent_id: str,
        admin_client: AdminClient,
        router_url: str,
        state_dir: Path,
        pending_invitation_token: str | None,
    ) -> None:
        self._agent_id = agent_id
        self._admin_client = admin_client
        self._router_url = router_url
        self._state_dir = state_dir
        self._pending_invitation_token = pending_invitation_token
        self._agent: Agent | None = None
        self._agent_task: asyncio.Task[None] | None = None
        self._connected_task: asyncio.Task[None] | None = None

    # -- per-kind hooks -----------------------------------------------------

    def build_agent(self, invitation: str) -> Agent:
        """Construct the backplane `Agent` this bridge hosts."""
        raise NotImplementedError

    async def record_connected(self) -> None:
        """Tell the router the agent has onboarded (clears the consumed
        invitation so the admin UI stops showing 'pending')."""
        raise NotImplementedError

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Onboard the agent and run until cancelled. Returns early (without
        connecting) when there's neither persisted creds nor a pending
        invitation — the supervisor respawns next poll, by which point an
        admin reconnect may have stashed a token."""
        if not self._can_onboard():
            logger.info(
                "agent_bridge_awaiting_invitation",
                extra={
                    "event": "agent_bridge_awaiting_invitation",
                    "kind": self.KIND,
                    "bp.bridged_agent_id": self._agent_id,
                },
            )
            return
        self._spawn_agent()
        assert self._agent_task is not None
        metrics.agent_bridge_starts_total.labels(
            kind=self.KIND, agent_id=self._agent_id,
        ).inc()
        exit_reason = "returned"
        try:
            await self._agent_task
        except asyncio.CancelledError:
            exit_reason = "cancelled"
            raise
        except Exception:
            exit_reason = "error"
            raise
        finally:
            metrics.agent_bridge_exits_total.labels(
                kind=self.KIND, agent_id=self._agent_id, reason=exit_reason,
            ).inc()
            await self._tear_down_agent()

    def _spawn_agent(self) -> None:
        self._agent = self.build_agent(self._onboarding_invitation())
        # Fire the connected-writeback once the WS handshake is up (creds
        # persisted). Background task so the on_startup hook returns
        # immediately and the dispatch loop starts reading the socket.
        self._agent.on_startup(self._on_connect)
        self._agent_task = asyncio.create_task(
            self._agent.run_async(),
            name=f"{self.KIND}_agent:{self._agent_id}",
        )

    async def _on_connect(self) -> None:
        self._connected_task = asyncio.create_task(
            self._record_connected_guarded(),
            name=f"{self.KIND}_agent_connected:{self._agent_id}",
        )

    async def _record_connected_guarded(self) -> None:
        try:
            await self.record_connected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — the agent is connected regardless. The admin UI may
            # show a stale "pending" until the next reconnect/connect.
            logger.warning(
                "agent_bridge_connected_write_failed",
                extra={
                    "event": "agent_bridge_connected_write_failed",
                    "kind": self.KIND,
                    "bp.bridged_agent_id": self._agent_id,
                    "error": repr(exc),
                },
            )

    async def _tear_down_agent(self) -> None:
        for task in (self._connected_task, self._agent_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception(
                    "agent_bridge_teardown_error",
                    extra={
                        "event": "agent_bridge_teardown_error",
                        "kind": self.KIND,
                        "bp.bridged_agent_id": self._agent_id,
                    },
                )
        self._connected_task = None
        self._agent_task = None
        self._agent = None

    # -- onboarding ---------------------------------------------------------

    def _creds_path(self) -> Path:
        return self._state_dir / self._agent_id / "credentials.json"

    def _can_onboard(self) -> bool:
        """True if the agent can connect: persisted creds to resume, or an
        admin-minted invitation to onboard with. When neither holds, the
        bridge waits for an admin (re)connect rather than spinning."""
        return self._creds_path().exists() or bool(
            self._pending_invitation_token
        )

    def _onboarding_invitation(self) -> str:
        """The invitation used to onboard. Empty once persisted creds exist
        (the SDK resumes from them and ignores the invitation)."""
        if self._creds_path().exists():
            return ""
        return self._pending_invitation_token or ""
