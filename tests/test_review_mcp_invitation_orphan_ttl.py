"""bridge issues service invitations with short TTL.

`ServerBridge._issue_invitation_if_needed` self-issues an invitation
just before constructing the per-server agent's `run_async()` task.
The agent consumes the invitation during its first onboard handshake
— typically within a second. If the bridge process crashes
between `issue` and `consume` (network failure, OOM, panic), the
invitation row sits in the `invitations` table marked
`consumed_at=null`. That orphan is harmless functionally (the
agent re-issues a fresh one on the next bridge run) but it:

1. **Clutters the admin invitation list** — operators see dozens
   of unused tokens with no obvious explanation.
2. **Widens the attack surface** — anyone who reads the orphan
   token from a log file or backup can onboard a service-level
   agent until the TTL elapses.

The bridge passes `expires_in_s=300` (5 minutes) — generous for
the worst-case slow onboard, narrow enough that the reaper sweep
clears orphans well before they pile up or pose meaningful risk.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_row():  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.server_bridge import ServerBridgeRow

    return ServerBridgeRow(
        server_id="srv1",
        url="https://upstream/",
        transport="streamable_http",
        auth_kind="none",
        auth_value_ref=None,
        auth_header_name=None,
        groups=["mcp_bridge"],
        expose_to_llm=True,
        refresh_requested_at=None,
    )


def _make_tool(name: str = "read_file"):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.mcp_client import ToolDefinition

    return ToolDefinition(
        name=name,
        description="t",
        input_schema={"type": "object"},
    )


def _make_bridge(tmp_path: Path, admin_client: Any):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.server_bridge import ServerBridge

    return ServerBridge(
        _make_row(),
        admin_client=admin_client,
        router_url="ws://r/",
        state_dir=tmp_path,
    )


def test_bridge_invitation_uses_short_ttl(tmp_path: Path) -> None:
    """End-to-end: the bridge passes `expires_in_s=300` to the
    admin client. Pin via call_args inspection.

    One invitation per server — the method takes no tool argument."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")

    admin = MagicMock()
    admin.issue_service_invitation = AsyncMock(return_value=None)
    bridge = _make_bridge(tmp_path, admin)

    asyncio.run(bridge._issue_invitation_if_needed())

    admin.issue_service_invitation.assert_awaited_once()
    args, kwargs = admin.issue_service_invitation.await_args
    # `token` is the positional arg.
    assert isinstance(args[0], str) and len(args[0]) >= 32
    # `expires_in_s` is the kwarg we care about.
    assert kwargs.get("expires_in_s") == 300


def test_bridge_invitation_skipped_when_credentials_exist(
    tmp_path: Path,
) -> None:
    """Pre-existing credentials.json → no invitation issued → no
    orphan possible. Pin the fast-path."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge.tool_agent import agent_id_for_server

    admin = MagicMock()
    admin.issue_service_invitation = AsyncMock(return_value=None)
    bridge = _make_bridge(tmp_path, admin)

    # Materialise a fake credentials.json so the early-return fires.
    # One creds dir per server (`mcp_<server>/`); the bridge looks
    # up that per-server path.
    agent_dir = tmp_path / agent_id_for_server("srv1")
    agent_dir.mkdir(parents=True)
    (agent_dir / "credentials.json").write_text("{}")

    token = asyncio.run(bridge._issue_invitation_if_needed())

    assert token == ""
    admin.issue_service_invitation.assert_not_awaited()


def test_bridge_invitation_ttl_constant_is_short() -> None:
    """The TTL constant lives at module level so operators can
    monkey-patch it in dev / testing if needed. Pin both its
    presence and that the value is bounded sensibly."""
    from bp_mcp_bridge import server_bridge

    assert hasattr(server_bridge, "_BRIDGE_INVITATION_TTL_S")
    ttl = server_bridge._BRIDGE_INVITATION_TTL_S
    assert isinstance(ttl, int)
    # Sanity bounds:
    #   - Lower: at least 60s so a slow onboard under load doesn't
    #     race the expiry mid-handshake.
    #   - Upper: well below the API default 3600s so the orphan
    #     window is meaningfully shorter.
    assert 60 <= ttl <= 900


def test_bridge_invitation_source_pin_uses_ttl_constant() -> None:
    """Source pin so a future refactor that drops the kwarg and
    falls back to the API default 1-hour TTL gets caught."""
    from bp_mcp_bridge import server_bridge

    src = inspect.getsource(server_bridge.ServerBridge._issue_invitation_if_needed)
    assert "_BRIDGE_INVITATION_TTL_S" in src
    assert "expires_in_s=" in src


def test_bridge_invitation_failure_propagates(tmp_path: Path) -> None:
    """If `issue_service_invitation` raises, the bridge surfaces a
    typed RuntimeError — not silently swallowed. Pin that the
    error-handling shape didn't regress along with the TTL change."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")

    admin = MagicMock()
    admin.issue_service_invitation = AsyncMock(
        side_effect=RuntimeError("network blip")
    )
    bridge = _make_bridge(tmp_path, admin)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(bridge._issue_invitation_if_needed())
    assert "failed to self-issue invitation" in str(exc_info.value)


def test_admin_client_supports_custom_expires_in_s() -> None:
    """Source pin: the admin client's `issue_service_invitation`
    surface accepts the `expires_in_s` kwarg. If a future refactor
    drops the parameter, the bridge's fix silently regresses to the
    API default."""
    import inspect as _inspect

    from bp_mcp_bridge.admin_client import AdminClient

    sig = _inspect.signature(AdminClient.issue_service_invitation)
    assert "expires_in_s" in sig.parameters
