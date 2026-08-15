"""One code-agent row's runtime: ONE backplane `Agent` whose single mode
runs the operator's Python function in a hardened subprocess.

The lifecycle — onboard, stay connected, record the connect, tear down —
is `AgentBridgeBase`, shared with the custom LLM kind. What is here is the
row shape, the config signature the supervisor diffs on, the two per-kind
hooks, and one thing the LLM kind has no equivalent of: resolving the
row's `env://` secret references into the values the subprocess will see.

See `docs/design/bridge-python-code-agents.md`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bp_mcp_bridge.admin_client import AdminClient
from bp_mcp_bridge.agent_bridge import AgentBridgeBase
from bp_mcp_bridge.agent_common import KIND_CODE
from bp_mcp_bridge.auth_resolver import AuthResolveError, resolve_auth_value
from bp_mcp_bridge.code_agent import CodeAgentSpec, build_code_agent
from bp_mcp_bridge.config import StdioPolicy
from bp_sdk import Agent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodeAgentBridgeRow:
    """Subset of a `code_agents` row the bridge cares about, constructed
    from the JSON of `GET /v1/admin/code-agents`."""

    agent_id: str
    description: str
    code: str
    entrypoint: str
    parameters: list[dict[str, Any]]
    returns: dict[str, Any] | None
    secret_refs: dict[str, str]
    timeout_s: int
    memory_mb: int
    groups: list[str]
    capabilities: list[str]
    expose_to_llm: bool
    output_as_file: bool
    enabled: bool
    # Admin-minted short-TTL invitation for first onboard / reconnect. Consumed
    # by the bridge; NOT part of config_signature (a mint must not restart a
    # healthy bridge).
    pending_invitation_token: str | None = field(default=None)

    @classmethod
    def from_admin_dict(cls, row: dict[str, Any]) -> CodeAgentBridgeRow:
        return cls(
            agent_id=row["agent_id"],
            description=row.get("description") or "",
            code=row.get("code") or "",
            entrypoint=row.get("entrypoint") or "run",
            parameters=list(row.get("parameters") or []),
            returns=row.get("returns") or None,
            secret_refs=dict(row.get("secret_refs") or {}),
            timeout_s=int(row.get("timeout_s") or 30),
            memory_mb=int(row.get("memory_mb") or 512),
            groups=list(row.get("groups") or []),
            capabilities=list(row.get("capabilities") or []),
            expose_to_llm=bool(row.get("expose_to_llm", True)),
            output_as_file=bool(row.get("output_as_file", False)),
            enabled=bool(row.get("enabled", True)),
            pending_invitation_token=row.get("pending_invitation_token"),
        )

    def config_signature(self) -> tuple:
        """Fields whose change requires a full bridge restart. Excludes
        `enabled` (handled by the supervisor's desired-set membership) and
        the pending invitation.

        The CODE is in here by hash, not by value: an edit must restart the
        bridge, but a signature is compared on every poll and carrying a
        whole module body through that comparison is waste."""
        from hashlib import sha256  # noqa: PLC0415

        return (
            self.description,
            sha256(self.code.encode("utf-8")).hexdigest(),
            self.entrypoint,
            tuple(
                (
                    p.get("name"),
                    p.get("type", "string"),
                    p.get("description", ""),
                    p.get("required", True),
                )
                for p in self.parameters
            ),
            repr(self.returns),
            tuple(sorted(self.secret_refs.items())),
            self.timeout_s,
            self.memory_mb,
            tuple(self.groups),
            tuple(self.capabilities),
            self.expose_to_llm,
            self.output_as_file,
        )


class CodeAgentBridge(AgentBridgeBase):
    """One code agent's runtime."""

    KIND = KIND_CODE

    def __init__(
        self,
        row: CodeAgentBridgeRow,
        *,
        admin_client: AdminClient,
        router_url: str,
        state_dir: Path,
        policy: StdioPolicy | None = None,
    ) -> None:
        super().__init__(
            agent_id=row.agent_id,
            admin_client=admin_client,
            router_url=router_url,
            state_dir=state_dir,
            pending_invitation_token=row.pending_invitation_token,
        )
        self._row = row
        self._policy = policy or StdioPolicy()

    def build_agent(self, invitation: str) -> Agent:
        return build_code_agent(self._to_spec(), invitation)

    async def record_connected(self) -> None:
        await self._admin_client.record_code_agent_connected(self._row.agent_id)

    def _resolved_secrets(self) -> dict[str, str]:
        """`{ENV_NAME: env://VAR}` → `{ENV_NAME: value}`, resolved from the
        BRIDGE's own environment.

        An unresolvable ref is logged by NAME and skipped rather than failing
        the bridge: the operator's function then sees the variable missing and
        can say so, which is a better failure than an agent that never comes
        up for a typo in one of five refs. The ref name is safe to log; the
        value never is."""
        out: dict[str, str] = {}
        for name, ref in self._row.secret_refs.items():
            try:
                value = resolve_auth_value(ref)
            except (AuthResolveError, NotImplementedError) as exc:
                logger.warning(
                    "code_agent_secret_unresolved",
                    extra={
                        "event": "code_agent_secret_unresolved",
                        "bp.code_agent_id": self._row.agent_id,
                        "secret_name": name,
                        "error": str(exc),
                    },
                )
                continue
            if value is not None:
                out[name] = value
        return out

    def _to_spec(self) -> CodeAgentSpec:
        return CodeAgentSpec(
            agent_id=self._row.agent_id,
            description=self._row.description,
            code=self._row.code,
            entrypoint=self._row.entrypoint,
            parameters=self._row.parameters,
            returns=self._row.returns,
            secrets=self._resolved_secrets(),
            timeout_s=self._row.timeout_s,
            memory_mb=self._row.memory_mb,
            groups=self._row.groups,
            capabilities=self._row.capabilities,
            expose_to_llm=self._row.expose_to_llm,
            output_as_file=self._row.output_as_file,
            policy=self._policy,
            router_url=self._router_url,
            state_dir=self._state_dir,
        )
