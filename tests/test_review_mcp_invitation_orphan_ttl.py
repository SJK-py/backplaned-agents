"""MCP onboarding invitations use a short TTL.

The MCP bridge no longer self-mints invitations (that needed the
invitation-mint capability, i.e. full admin). Instead an admin action
(`create_mcp_server` / `refresh_mcp_server_tools`) mints a `level=service`
invitation and stashes it on the `mcp_servers` row via
`_mint_mcp_pending_invitation`; the bridge consumes it on its next poll.

The orphan concern is unchanged but moved router-side: if no bridge consumes
the stashed token, it should expire quickly so it doesn't (1) clutter the
invitation list or (2) widen the window in which a leaked token can onboard a
service-level agent. So the router mints with a short TTL constant, not the API
default 1-hour.
"""
from __future__ import annotations

import inspect

import pytest


def test_router_mints_mcp_invitation_with_short_ttl() -> None:
    """Source pin: the admin mint helper uses the short TTL constant + stashes
    the token on the row, rather than the API default."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin._mint_mcp_pending_invitation)
    assert "_MCP_INVITATION_TTL_S" in src
    assert 'level="service"' in src
    assert "set_mcp_pending_invitation" in src


def test_router_mcp_invitation_ttl_constant_is_short() -> None:
    """The TTL constant is bounded: long enough that the bridge's next poll
    consumes it, short enough that an unconsumed token doesn't linger."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    ttl = admin._MCP_INVITATION_TTL_S
    assert isinstance(ttl, int)
    # Well below the API default (3600s) so the orphan window is meaningfully
    # shorter, with headroom over a poll interval.
    assert 60 <= ttl <= 900


def test_bridge_consumes_pending_invitation_not_self_mint() -> None:
    """The bridge onboards from the admin-stashed token and has no
    self-mint surface."""
    from bp_mcp_bridge.admin_client import AdminClient
    from bp_mcp_bridge.server_bridge import ServerBridge

    assert not hasattr(AdminClient, "issue_service_invitation")
    src = inspect.getsource(ServerBridge._onboarding_invitation)
    assert "self._row.pending_invitation_token" in src


def test_bridge_resumes_from_creds_skips_invitation() -> None:
    """Pre-existing credentials.json → the SDK resumes; the onboarding
    invitation is unused (returned empty)."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._onboarding_invitation)
    assert "_creds_path().exists()" in src
    assert 'return ""' in src
