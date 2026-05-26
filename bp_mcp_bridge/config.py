"""Env-driven config for the Phase 10b bridge. Phase 10c will read
the same fields from `mcp_servers` rows via admin-API LISTEN/NOTIFY
or polling; the dataclass shape stays stable so the runtime code
doesn't change shape between phases."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRANSPORTS = ("streamable_http", "sse")
_AUTH_KINDS = ("none", "bearer", "header")


@dataclass(frozen=True)
class BridgeConfig:
    """One MCP server's configuration. Mirrors the columns of the
    `mcp_servers` table (PR #117) so the Phase 10c switch from
    env-config to DB-config is a swap of the loader, not the
    consumer.

    `admin_token` is the bridge's escape hatch — Phase 10b's C.1
    auth model has the bridge holding an admin JWT to self-issue
    invitations on first run. Subsequent runs use the per-agent
    credentials.json persisted under `state_dir`. Phase 10c
    switches to a narrower service-level identity."""

    # MCP server identity (mirrors mcp_servers row).
    server_id: str
    url: str
    transport: str
    auth_kind: str
    auth_value: str | None  # resolved secret, NOT a ref — env://VAR is resolved by the operator
    auth_header_name: str | None
    groups: list[str] = field(default_factory=list)
    expose_to_llm: bool = True

    # Bridge runtime config.
    router_url: str = "ws://localhost:8000/v1/agent"
    router_admin_url: str = "http://localhost:8000"  # base URL for /v1/admin/...
    admin_token: str | None = None
    state_dir: Path = field(default_factory=lambda: Path("/var/lib/bp_mcp_bridge"))

    def __post_init__(self) -> None:
        # Frozen dataclass — bypass setattr to validate.
        if self.transport not in _TRANSPORTS:
            raise ValueError(
                f"transport must be one of {_TRANSPORTS}; got {self.transport!r}"
            )
        if self.auth_kind not in _AUTH_KINDS:
            raise ValueError(
                f"auth_kind must be one of {_AUTH_KINDS}; got {self.auth_kind!r}"
            )
        if self.auth_kind != "none" and not self.auth_value:
            raise ValueError(
                f"auth_value required when auth_kind={self.auth_kind!r}"
            )
        if self.auth_kind == "header" and not self.auth_header_name:
            raise ValueError(
                "auth_header_name required when auth_kind='header'"
            )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BridgeConfig:
        """Build the config from process env (or a supplied mapping
        for testing). Required vars: SERVER_ID, URL, TRANSPORT."""
        e = env if env is not None else dict(os.environ)
        groups_raw = e.get("BP_MCP_BRIDGE_GROUPS", "").strip()
        groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
        expose = e.get("BP_MCP_BRIDGE_EXPOSE_TO_LLM", "true").lower()
        state_dir_raw = e.get("BP_MCP_BRIDGE_STATE_DIR", "/var/lib/bp_mcp_bridge")
        return cls(
            server_id=_require_env(e, "BP_MCP_BRIDGE_SERVER_ID"),
            url=_require_env(e, "BP_MCP_BRIDGE_URL"),
            transport=e.get("BP_MCP_BRIDGE_TRANSPORT", "streamable_http"),
            auth_kind=e.get("BP_MCP_BRIDGE_AUTH_KIND", "none"),
            auth_value=e.get("BP_MCP_BRIDGE_AUTH_VALUE") or None,
            auth_header_name=e.get("BP_MCP_BRIDGE_AUTH_HEADER_NAME") or None,
            groups=groups,
            expose_to_llm=expose not in ("false", "0", "no", "off"),
            router_url=e.get("BP_MCP_BRIDGE_ROUTER_URL", "ws://localhost:8000/v1/agent"),
            router_admin_url=e.get("BP_MCP_BRIDGE_ROUTER_ADMIN_URL", "http://localhost:8000"),
            admin_token=e.get("BP_MCP_BRIDGE_ADMIN_TOKEN") or None,
            state_dir=Path(state_dir_raw),
        )


def _require_env(env: dict[str, str], key: str) -> str:
    val = env.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"{key} environment variable is required for bp_mcp_bridge"
        )
    return val


@dataclass(frozen=True)
class SupervisorConfig:
    """Phase 10c top-level config — what one bridge PROCESS needs
    to know to connect to the router and start managing multiple
    MCP servers. The per-server config (URL, auth, etc.) comes
    from the `mcp_servers` table via the admin API; the
    supervisor itself doesn't need to know about specific
    servers."""

    router_url: str          # WebSocket URL for agent connections
    router_admin_url: str    # HTTP base URL for /v1/admin/...
    admin_token: str         # admin JWT (resolved env://… by operator)
    state_dir: Path
    poll_interval_s: float = 30.0
    metrics_port: int = 9464
    """Port for the Prometheus exposition endpoint. The default
    (9464) is the OpenTelemetry-Prometheus convention. Set to 0
    (or any value <= 0) to disable the metrics server entirely —
    the opt-out for deployments that scrape via a sidecar or don't
    scrape at all. A bind failure is logged and ignored; metrics
    are never load-bearing."""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SupervisorConfig:
        e = env if env is not None else dict(os.environ)
        interval_raw = e.get("BP_MCP_BRIDGE_POLL_INTERVAL_S", "30")
        try:
            interval = float(interval_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"BP_MCP_BRIDGE_POLL_INTERVAL_S must be a number, got {interval_raw!r}"
            ) from exc
        metrics_port_raw = e.get("BP_MCP_BRIDGE_METRICS_PORT", "9464")
        try:
            metrics_port = int(metrics_port_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"BP_MCP_BRIDGE_METRICS_PORT must be an integer, "
                f"got {metrics_port_raw!r}"
            ) from exc
        return cls(
            router_url=e.get(
                "BP_MCP_BRIDGE_ROUTER_URL", "ws://localhost:8000/v1/agent",
            ),
            router_admin_url=e.get(
                "BP_MCP_BRIDGE_ROUTER_ADMIN_URL", "http://localhost:8000",
            ),
            admin_token=_require_env(e, "BP_MCP_BRIDGE_ADMIN_TOKEN"),
            state_dir=Path(
                e.get("BP_MCP_BRIDGE_STATE_DIR", "/var/lib/bp_mcp_bridge")
            ),
            poll_interval_s=interval,
            metrics_port=metrics_port,
        )
