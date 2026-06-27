"""One custom-agent row's runtime: ONE backplane `Agent` whose single
mode runs an LLM completion.

Far simpler than `ServerBridge` — there is no upstream MCP client, no
`tools/list`, no `tools/list_changed` reconcile. The bridge builds the
agent, onboards it, and stays connected until cancelled. The supervisor
restarts it only when the row's `config_signature` changes (an admin
edit); on restart it resumes from persisted credentials, so no fresh
invitation is needed for edits.

See `docs/design/mcp-bridge-custom-llm-agents.md`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bp_mcp_bridge.admin_client import AdminClient
from bp_mcp_bridge.custom_agent import CustomAgentSpec, build_custom_agent
from bp_sdk import Agent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CustomAgentBridgeRow:
    """Subset of a `custom_agents` row the bridge cares about,
    constructed from the JSON of `GET /v1/admin/custom-agents`."""

    agent_id: str
    description: str
    preset_name: str
    system_prompt: str
    user_prompt: str
    parameters: list[dict[str, Any]]
    groups: list[str]
    capabilities: list[str]
    expose_to_llm: bool
    output_as_file: bool
    enabled: bool
    agent_loop_enabled: bool = False
    max_rounds: int = 4
    file_access: str = "none"
    peer_tools_enabled: bool = False
    # Admin-minted short-TTL invitation for first onboard / reconnect. Consumed
    # by the bridge; NOT part of config_signature (a mint must not restart a
    # healthy bridge).
    pending_invitation_token: str | None = field(default=None)

    @classmethod
    def from_admin_dict(cls, row: dict[str, Any]) -> CustomAgentBridgeRow:
        return cls(
            agent_id=row["agent_id"],
            description=row.get("description") or "",
            preset_name=row["preset_name"],
            system_prompt=row.get("system_prompt") or "",
            user_prompt=row.get("user_prompt") or "",
            parameters=list(row.get("parameters") or []),
            groups=list(row.get("groups") or []),
            capabilities=list(row.get("capabilities") or []),
            expose_to_llm=bool(row.get("expose_to_llm", True)),
            output_as_file=bool(row.get("output_as_file", False)),
            enabled=bool(row.get("enabled", True)),
            agent_loop_enabled=bool(row.get("agent_loop_enabled", False)),
            max_rounds=int(row.get("max_rounds", 4) or 4),
            file_access=row.get("file_access") or "none",
            peer_tools_enabled=bool(row.get("peer_tools_enabled", False)),
            pending_invitation_token=row.get("pending_invitation_token"),
        )

    def config_signature(self) -> tuple:
        """Fields whose change requires a full bridge restart (a rebuild of
        the agent's AgentInfo + handler). Excludes `enabled` (handled by the
        supervisor's desired-set membership) and the pending invitation."""
        return (
            self.description,
            self.preset_name,
            self.system_prompt,
            self.user_prompt,
            tuple(
                (
                    p.get("name"),
                    p.get("description", ""),
                    p.get("required", True),
                    p.get("file_ref", False),
                )
                for p in self.parameters
            ),
            tuple(self.groups),
            tuple(self.capabilities),
            self.expose_to_llm,
            self.output_as_file,
            self.agent_loop_enabled,
            self.max_rounds,
            self.file_access,
            self.peer_tools_enabled,
        )


class CustomAgentBridge:
    """One custom agent's runtime: build + onboard ONE backplane agent and
    keep it connected. The supervisor spawns `run()` as a task and cancels
    it to tear down."""

    def __init__(
        self,
        row: CustomAgentBridgeRow,
        *,
        admin_client: AdminClient,
        router_url: str,
        state_dir: Path,
    ) -> None:
        self._row = row
        self._admin_client = admin_client
        self._router_url = router_url
        self._state_dir = state_dir
        self._agent: Agent | None = None
        self._agent_task: asyncio.Task[None] | None = None
        self._connected_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Onboard the agent and run until cancelled. Returns early (without
        connecting) when there's neither persisted creds nor a pending
        invitation — the supervisor respawns next poll, by which point an
        admin reconnect may have stashed a token."""
        if not self._can_onboard():
            logger.info(
                "custom_agent_bridge_awaiting_invitation",
                extra={
                    "event": "custom_agent_bridge_awaiting_invitation",
                    "bp.custom_agent_id": self._row.agent_id,
                },
            )
            return
        self._spawn_agent()
        assert self._agent_task is not None
        try:
            await self._agent_task
        finally:
            await self._tear_down_agent()

    def _spawn_agent(self) -> None:
        invitation = self._onboarding_invitation()
        self._agent = build_custom_agent(self._to_spec(), invitation)
        # Fire the connected-writeback once the WS handshake is up (creds
        # persisted). Background task so the on_startup hook returns
        # immediately and the dispatch loop starts reading the socket.
        self._agent.on_startup(self._on_connect)
        self._agent_task = asyncio.create_task(
            self._agent.run_async(),
            name=f"custom_agent:{self._row.agent_id}",
        )

    async def _on_connect(self) -> None:
        self._connected_task = asyncio.create_task(
            self._record_connected(),
            name=f"custom_agent_connected:{self._row.agent_id}",
        )

    async def _record_connected(self) -> None:
        try:
            await self._admin_client.record_custom_agent_connected(
                self._row.agent_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — the agent is connected regardless. The admin UI may
            # show a stale "pending" until the next reconnect/connect.
            logger.warning(
                "custom_agent_connected_write_failed",
                extra={
                    "event": "custom_agent_connected_write_failed",
                    "bp.custom_agent_id": self._row.agent_id,
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
                    "custom_agent_teardown_error",
                    extra={
                        "event": "custom_agent_teardown_error",
                        "bp.custom_agent_id": self._row.agent_id,
                    },
                )
        self._connected_task = None
        self._agent_task = None
        self._agent = None

    def _to_spec(self) -> CustomAgentSpec:
        return CustomAgentSpec(
            agent_id=self._row.agent_id,
            description=self._row.description,
            preset_name=self._row.preset_name,
            system_prompt=self._row.system_prompt,
            user_prompt=self._row.user_prompt,
            parameters=self._row.parameters,
            groups=self._row.groups,
            capabilities=self._row.capabilities,
            expose_to_llm=self._row.expose_to_llm,
            output_as_file=self._row.output_as_file,
            agent_loop_enabled=self._row.agent_loop_enabled,
            max_rounds=self._row.max_rounds,
            file_access=self._row.file_access,
            peer_tools_enabled=self._row.peer_tools_enabled,
            router_url=self._router_url,
            state_dir=self._state_dir,
        )

    def _creds_path(self) -> Path:
        return self._state_dir / self._row.agent_id / "credentials.json"

    def _can_onboard(self) -> bool:
        """True if the agent can connect: persisted creds to resume, or an
        admin-minted invitation to onboard with. When neither holds, the
        bridge waits for an admin (re)connect rather than spinning."""
        return self._creds_path().exists() or bool(
            self._row.pending_invitation_token
        )

    def _onboarding_invitation(self) -> str:
        """The invitation used to onboard. Empty once persisted creds exist
        (the SDK resumes from them and ignores the invitation)."""
        if self._creds_path().exists():
            return ""
        return self._row.pending_invitation_token or ""
