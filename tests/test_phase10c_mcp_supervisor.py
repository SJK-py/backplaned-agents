"""Tests for Phase 10c: multi-server bridge supervisor + admin
write-back endpoint + auth resolver + ServerBridgeRow.

Covers:
  * `tools-refreshed` admin endpoint (router-side): query helper,
    Pydantic request shape, atomic write semantics, audit.
  * `auth_resolver`: env:// resolution, secret:// rejection.
  * `AdminClient`: payload shapes for the three RPCs it issues.
  * `ServerBridgeRow.from_admin_dict` / `config_signature` —
    diff key for reconciliation.
  * `Supervisor` reconciliation: spawn / stop / restart-on-config-
    change / restart-on-refresh-advanced / no-op.
  * `SupervisorConfig` env loading.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Router-side: tools-refreshed endpoint
# ===========================================================================


def test_query_helper_atomic_write_clears_refresh_requested_at() -> None:
    """The query writes all three columns in one UPDATE:
    tools_cache, last_connected_at=now(), refresh_requested_at=NULL.
    Atomic so an admin-click → bridge-respond loop has a clean
    completion state."""
    from bp_router.db import queries

    src = inspect.getsource(queries.record_mcp_server_tools_refreshed)
    # Three SET targets in one UPDATE.
    assert "SET tools_cache" in src
    assert "last_connected_at    = now()" in src
    assert "refresh_requested_at = NULL" in src
    # Single UPDATE statement — no separate writes that would race.
    assert src.count("UPDATE mcp_servers") == 1


def test_query_helper_returns_truthy_on_hit() -> None:
    from bp_router.db import queries

    src = inspect.getsource(queries.record_mcp_server_tools_refreshed)
    assert "result.endswith(\" 1\")" in src


def test_request_model_defaults_tools_cache_to_empty_dict() -> None:
    """Empty body is acceptable — a bridge that connected but the
    upstream tools/list returned nothing still wants to mark the
    row as 'connected'."""
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpToolsRefreshedRequest

    req = McpToolsRefreshedRequest()
    assert req.tools_cache == {}


def test_endpoint_audits_tool_count() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.record_mcp_tools_refreshed)
    assert 'event="mcp_server.tools_refreshed"' in src
    assert '"tool_count"' in src


def test_endpoint_returns_404_on_unknown_server() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.record_mcp_tools_refreshed)
    assert "if not ok:" in src
    assert 'HTTPException(404' in src


def test_endpoint_mounted_on_admin_router() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    paths = {route.path for route in admin.router.routes if hasattr(route, "path")}
    assert "/mcp-servers/{server_id}/tools-refreshed" in paths


# ===========================================================================
# Auth resolver
# ===========================================================================


def test_auth_resolver_returns_none_for_none_input() -> None:
    from bp_mcp_bridge.auth_resolver import resolve_auth_value

    assert resolve_auth_value(None) is None
    assert resolve_auth_value("") is None


def test_auth_resolver_env_scheme_reads_from_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.auth_resolver import resolve_auth_value

    monkeypatch.setenv("MCP_TEST_TOKEN", "tok-12345")
    assert resolve_auth_value("env://MCP_TEST_TOKEN") == "tok-12345"


def test_auth_resolver_env_scheme_raises_on_missing_var(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.auth_resolver import AuthResolveError, resolve_auth_value

    monkeypatch.delenv("MCP_MISSING_VAR_X", raising=False)
    with pytest.raises(AuthResolveError, match="not set"):
        resolve_auth_value("env://MCP_MISSING_VAR_X")


def test_auth_resolver_env_scheme_raises_on_empty_var_name() -> None:
    from bp_mcp_bridge.auth_resolver import AuthResolveError, resolve_auth_value

    with pytest.raises(AuthResolveError, match="empty env var name"):
        resolve_auth_value("env://")


def test_auth_resolver_secret_scheme_not_implemented() -> None:
    """secret:// is reserved for a future phase. NotImplementedError
    with a clear workaround pointer (use env://VAR + sidecar)."""
    from bp_mcp_bridge.auth_resolver import resolve_auth_value

    with pytest.raises(NotImplementedError, match="secret-store sidecar"):
        resolve_auth_value("secret://kv/mcp/filesystem")


def test_auth_resolver_unknown_scheme_rejected() -> None:
    from bp_mcp_bridge.auth_resolver import AuthResolveError, resolve_auth_value

    with pytest.raises(AuthResolveError, match="env:// scheme"):
        resolve_auth_value("https://vault.example.com/secret")


# ===========================================================================
# AdminClient — payload shapes
# ===========================================================================


def test_admin_client_list_calls_correct_path() -> None:
    """Source pin: list_mcp_servers hits /v1/admin/mcp-servers."""
    from bp_mcp_bridge.admin_client import AdminClient

    src = inspect.getsource(AdminClient.list_mcp_servers)
    assert "/v1/admin/mcp-servers" in src


def test_admin_client_issue_invitation_uses_phase1_token_field() -> None:
    """Source pin: POST body uses the Phase-1 F10 `token` field
    (caller-supplied invitation token), level=service, short TTL."""
    from bp_mcp_bridge.admin_client import AdminClient

    src = inspect.getsource(AdminClient.issue_service_invitation)
    assert '"level": "service"' in src
    assert '"token": token' in src
    assert "expires_in_s" in src


def test_admin_client_record_refreshed_payload_shape() -> None:
    """Source pin: POST body carries tools_cache verbatim under
    that key — matches McpToolsRefreshedRequest on the router."""
    from bp_mcp_bridge.admin_client import AdminClient

    src = inspect.getsource(AdminClient.record_tools_refreshed)
    assert '/v1/admin/mcp-servers/' in src
    assert "/tools-refreshed" in src
    assert '"tools_cache": tools_cache' in src


def test_admin_client_raise_on_non_2xx() -> None:
    """Non-2xx surfaces as AdminApiError carrying status_code +
    detail message extracted from the JSON body (or text fallback)."""
    from bp_mcp_bridge.admin_client import AdminClient

    src = inspect.getsource(AdminClient._raise_or_json)
    assert "raise AdminApiError" in src
    assert "resp.json()" in src


# ===========================================================================
# ServerBridgeRow
# ===========================================================================


def test_server_bridge_row_from_admin_dict_decodes_minimal() -> None:
    from bp_mcp_bridge.server_bridge import ServerBridgeRow

    row = ServerBridgeRow.from_admin_dict({
        "server_id": "fs",
        "url": "https://x/",
        "transport": "streamable_http",
        "auth_kind": "none",
    })
    assert row.server_id == "fs"
    assert row.groups == []
    assert row.expose_to_llm is True
    assert row.refresh_requested_at is None


def test_server_bridge_row_config_signature_excludes_refresh_field() -> None:
    """`refresh_requested_at` MUST NOT be part of the config
    signature — otherwise every refresh request would also count
    as a config change and trigger a restart twice (once for
    "config changed", once for the supervisor's separate refresh
    detection). The supervisor relies on these signals being
    disjoint."""
    from bp_mcp_bridge.server_bridge import ServerBridgeRow

    base_args = {
        "server_id": "fs", "url": "https://x/", "transport": "streamable_http",
        "auth_kind": "none", "auth_value_ref": None, "auth_header_name": None,
        "groups": [], "expose_to_llm": True,
    }
    a = ServerBridgeRow(refresh_requested_at=None, **base_args)
    b = ServerBridgeRow(refresh_requested_at="2026-05-15T00:00:00Z", **base_args)
    assert a.config_signature() == b.config_signature()


def test_server_bridge_row_config_signature_changes_on_url() -> None:
    from bp_mcp_bridge.server_bridge import ServerBridgeRow

    base_args = {
        "server_id": "fs", "transport": "streamable_http",
        "auth_kind": "none", "auth_value_ref": None, "auth_header_name": None,
        "groups": [], "expose_to_llm": True, "refresh_requested_at": None,
    }
    a = ServerBridgeRow(url="https://a/", **base_args)
    b = ServerBridgeRow(url="https://b/", **base_args)
    assert a.config_signature() != b.config_signature()


def test_server_bridge_row_config_signature_changes_on_auth_value_ref() -> None:
    """Same URL, different secret reference → MUST restart so the
    bridge picks up the new credential."""
    from bp_mcp_bridge.server_bridge import ServerBridgeRow

    base_args = {
        "server_id": "fs", "url": "https://x/", "transport": "streamable_http",
        "auth_kind": "bearer", "auth_header_name": None,
        "groups": [], "expose_to_llm": True, "refresh_requested_at": None,
    }
    a = ServerBridgeRow(auth_value_ref="env://OLD", **base_args)
    b = ServerBridgeRow(auth_value_ref="env://NEW", **base_args)
    assert a.config_signature() != b.config_signature()


# ===========================================================================
# Supervisor reconciliation
# ===========================================================================


def _stub_admin_client(rows: list[dict]) -> MagicMock:
    """AdminClient stub: returns the supplied rows from list_,
    no-ops on writes. Suitable for testing the supervisor's
    reconcile logic without HTTP."""
    client = MagicMock()
    client.list_mcp_servers = AsyncMock(return_value=rows)
    client.issue_service_invitation = AsyncMock(return_value=None)
    client.record_tools_refreshed = AsyncMock(return_value=None)
    client.aclose = AsyncMock(return_value=None)
    return client


def _row(server_id: str, **overrides) -> dict:  # type: ignore[no-untyped-def]
    """Build a row dict matching what GET /v1/admin/mcp-servers
    returns, with sensible defaults."""
    base = {
        "server_id": server_id,
        "description": "",
        "url": f"https://{server_id}/",
        "transport": "streamable_http",
        "auth_kind": "none",
        "auth_value_ref": None,
        "auth_header_name": None,
        "groups": [],
        "expose_to_llm": True,
        "tools_cache": None,
        "refresh_requested_at": None,
        "created_at": "2026-05-15T00:00:00Z",
        "last_connected_at": None,
        "created_by": None,
    }
    base.update(overrides)
    return base


def test_supervisor_spawns_bridge_for_new_row(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Initial reconcile: empty active map + one row → one
    spawned bridge task."""
    from bp_mcp_bridge.supervisor import Supervisor

    admin = _stub_admin_client([_row("fs")])
    sup = Supervisor(
        admin_client=admin, router_url="ws://x/", state_dir=tmp_path,
    )

    # Patch ServerBridge.run to a no-op that doesn't exit (so the
    # supervisor sees the task as alive).
    async def hang(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    from bp_mcp_bridge import server_bridge as sb_mod
    monkeypatch.setattr(sb_mod.ServerBridge, "run", hang)

    async def drive():
        await sup._reconcile_once()
        assert "fs" in sup._active
        await sup._tear_down_all()

    asyncio.run(drive())


def test_supervisor_tears_down_bridge_for_removed_row(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.supervisor import Supervisor

    admin = _stub_admin_client([_row("fs")])
    sup = Supervisor(
        admin_client=admin, router_url="ws://x/", state_dir=tmp_path,
    )

    async def hang(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    from bp_mcp_bridge import server_bridge as sb_mod
    monkeypatch.setattr(sb_mod.ServerBridge, "run", hang)

    async def drive():
        # First reconcile: spawn fs.
        await sup._reconcile_once()
        assert "fs" in sup._active

        # Second reconcile: row gone → torn down.
        admin.list_mcp_servers = AsyncMock(return_value=[])
        await sup._reconcile_once()
        assert "fs" not in sup._active

    asyncio.run(drive())


def test_supervisor_restarts_on_config_change(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """URL change → cancel old task, spawn fresh ServerBridge."""
    from bp_mcp_bridge.supervisor import Supervisor

    admin = _stub_admin_client([_row("fs", url="https://a/")])
    sup = Supervisor(
        admin_client=admin, router_url="ws://x/", state_dir=tmp_path,
    )

    async def hang(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    from bp_mcp_bridge import server_bridge as sb_mod
    monkeypatch.setattr(sb_mod.ServerBridge, "run", hang)

    async def drive():
        await sup._reconcile_once()
        first_task = sup._active["fs"].task

        # Same server_id, different URL.
        admin.list_mcp_servers = AsyncMock(
            return_value=[_row("fs", url="https://b/")]
        )
        await sup._reconcile_once()

        second_task = sup._active["fs"].task
        assert first_task is not second_task
        assert sup._active["fs"].row.url == "https://b/"
        await sup._tear_down_all()

    asyncio.run(drive())


def test_supervisor_triggers_refresh_on_refresh_advanced(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Phase 10f: refresh_requested_at moves null → set →
    supervisor calls ServerBridge.trigger_refresh() instead of
    cancelling + respawning the task. The bridge stays connected;
    only changed/added/removed tools see disruption."""
    from bp_mcp_bridge.supervisor import Supervisor

    admin = _stub_admin_client([_row("fs")])
    sup = Supervisor(
        admin_client=admin, router_url="ws://x/", state_dir=tmp_path,
    )

    async def hang(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    from bp_mcp_bridge import server_bridge as sb_mod
    monkeypatch.setattr(sb_mod.ServerBridge, "run", hang)

    async def drive():
        await sup._reconcile_once()
        entry = sup._active["fs"]
        first_task = entry.task
        first_bridge = entry.bridge
        # Spy on trigger_refresh so we can verify it was called.
        calls: list[int] = []
        original = first_bridge.trigger_refresh

        def spy() -> None:
            calls.append(1)
            original()

        first_bridge.trigger_refresh = spy  # type: ignore[method-assign]

        admin.list_mcp_servers = AsyncMock(
            return_value=[_row("fs", refresh_requested_at="2026-05-15T12:00:00Z")]
        )
        await sup._reconcile_once()

        entry_after = sup._active["fs"]
        # Same task — no restart.
        assert entry_after.task is first_task
        assert entry_after.bridge is first_bridge
        # trigger_refresh was called.
        assert calls == [1]
        await sup._tear_down_all()

    asyncio.run(drive())


def test_supervisor_noop_when_config_and_refresh_unchanged(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Two reconciles with the same row → same task, no respawn."""
    from bp_mcp_bridge.supervisor import Supervisor

    admin = _stub_admin_client([_row("fs")])
    sup = Supervisor(
        admin_client=admin, router_url="ws://x/", state_dir=tmp_path,
    )

    async def hang(*args, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    from bp_mcp_bridge import server_bridge as sb_mod
    monkeypatch.setattr(sb_mod.ServerBridge, "run", hang)

    async def drive():
        await sup._reconcile_once()
        first_task = sup._active["fs"].task

        await sup._reconcile_once()
        second_task = sup._active["fs"].task

        assert first_task is second_task
        await sup._tear_down_all()

    asyncio.run(drive())


def test_supervisor_refresh_advanced_detects_first_setting() -> None:
    """Helper: null → non-null counts as 'advanced'."""
    from bp_mcp_bridge.server_bridge import ServerBridgeRow
    from bp_mcp_bridge.supervisor import Supervisor

    common = {
        "server_id": "fs", "url": "https://x/", "transport": "streamable_http",
        "auth_kind": "none", "auth_value_ref": None, "auth_header_name": None,
        "groups": [], "expose_to_llm": True,
    }
    prev = ServerBridgeRow(refresh_requested_at=None, **common)
    new = ServerBridgeRow(refresh_requested_at="2026-05-15T12:00:00Z", **common)
    assert Supervisor._refresh_advanced(prev, new) is True


def test_supervisor_refresh_advanced_detects_newer_timestamp() -> None:
    from bp_mcp_bridge.server_bridge import ServerBridgeRow
    from bp_mcp_bridge.supervisor import Supervisor

    common = {
        "server_id": "fs", "url": "https://x/", "transport": "streamable_http",
        "auth_kind": "none", "auth_value_ref": None, "auth_header_name": None,
        "groups": [], "expose_to_llm": True,
    }
    prev = ServerBridgeRow(refresh_requested_at="2026-05-15T10:00:00Z", **common)
    new = ServerBridgeRow(refresh_requested_at="2026-05-15T12:00:00Z", **common)
    assert Supervisor._refresh_advanced(prev, new) is True


def test_supervisor_refresh_advanced_rejects_equal_timestamps() -> None:
    """Same timestamp → already handled, don't restart."""
    from bp_mcp_bridge.server_bridge import ServerBridgeRow
    from bp_mcp_bridge.supervisor import Supervisor

    common = {
        "server_id": "fs", "url": "https://x/", "transport": "streamable_http",
        "auth_kind": "none", "auth_value_ref": None, "auth_header_name": None,
        "groups": [], "expose_to_llm": True,
    }
    ts = "2026-05-15T10:00:00Z"
    prev = ServerBridgeRow(refresh_requested_at=ts, **common)
    new = ServerBridgeRow(refresh_requested_at=ts, **common)
    assert Supervisor._refresh_advanced(prev, new) is False


def test_supervisor_handles_admin_api_failure_without_crashing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Transient admin API errors must be logged + retried on next
    poll, not propagated to crash the supervisor."""
    from bp_mcp_bridge.admin_client import AdminApiError
    from bp_mcp_bridge.supervisor import Supervisor

    admin = MagicMock()
    admin.list_mcp_servers = AsyncMock(side_effect=AdminApiError(503, "down"))
    sup = Supervisor(
        admin_client=admin, router_url="ws://x/", state_dir=tmp_path,
    )

    async def drive():
        # Doesn't raise — returns silently after logging.
        await sup._reconcile_once()
        assert sup._active == {}

    asyncio.run(drive())


# ===========================================================================
# SupervisorConfig env loading
# ===========================================================================


def test_supervisor_config_from_env_minimal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.config import SupervisorConfig

    cfg = SupervisorConfig.from_env({
        "BP_MCP_BRIDGE_ADMIN_TOKEN": "tok",
    })
    assert cfg.admin_token == "tok"
    assert cfg.router_url == "ws://localhost:8000/v1/agent"
    assert cfg.poll_interval_s == 30.0


def test_supervisor_config_requires_admin_token() -> None:
    from bp_mcp_bridge.config import SupervisorConfig

    with pytest.raises(RuntimeError, match="BP_MCP_BRIDGE_ADMIN_TOKEN"):
        SupervisorConfig.from_env({})


def test_supervisor_config_parses_poll_interval() -> None:
    from bp_mcp_bridge.config import SupervisorConfig

    cfg = SupervisorConfig.from_env({
        "BP_MCP_BRIDGE_ADMIN_TOKEN": "tok",
        "BP_MCP_BRIDGE_POLL_INTERVAL_S": "5",
    })
    assert cfg.poll_interval_s == 5.0


def test_supervisor_config_rejects_non_numeric_poll_interval() -> None:
    from bp_mcp_bridge.config import SupervisorConfig

    with pytest.raises(RuntimeError, match="must be a number"):
        SupervisorConfig.from_env({
            "BP_MCP_BRIDGE_ADMIN_TOKEN": "tok",
            "BP_MCP_BRIDGE_POLL_INTERVAL_S": "thirty",
        })
