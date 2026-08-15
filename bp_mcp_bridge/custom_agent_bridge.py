"""One custom-agent row's runtime: ONE backplane `Agent` whose single
mode runs an LLM completion.

The lifecycle — onboard, stay connected, record the connect, tear down —
is `AgentBridgeBase`, shared with the code-agent kind. What is here is the
row shape, the config signature the supervisor diffs on, and the two hooks
that say which agent to build and which admin endpoint to call.

See `docs/design/mcp-bridge-custom-llm-agents.md` and
`docs/design/bridge-python-code-agents.md` §11.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bp_mcp_bridge.admin_client import AdminClient
from bp_mcp_bridge.agent_bridge import AgentBridgeBase
from bp_mcp_bridge.agent_common import KIND_CUSTOM
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


class CustomAgentBridge(AgentBridgeBase):
    """One custom LLM agent's runtime. Everything but the three per-kind
    hooks lives in `AgentBridgeBase`, shared with the code-agent kind."""

    KIND = KIND_CUSTOM

    def __init__(
        self,
        row: CustomAgentBridgeRow,
        *,
        admin_client: AdminClient,
        router_url: str,
        state_dir: Path,
    ) -> None:
        super().__init__(
            agent_id=row.agent_id,
            admin_client=admin_client,
            router_url=router_url,
            state_dir=state_dir,
            pending_invitation_token=row.pending_invitation_token,
        )
        self._row = row

    def build_agent(self, invitation: str) -> Agent:
        return build_custom_agent(self._to_spec(), invitation)

    async def record_connected(self) -> None:
        await self._admin_client.record_custom_agent_connected(
            self._row.agent_id
        )

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
