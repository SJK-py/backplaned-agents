"""Incremental MCP reconciliation + SSE tools/list_changed listener
+ Agent.update_info / Agent.set_modes wiring.

Per the one-Agent-per-server design, reconcile collapses to a single
`Agent.set_modes(...)` call. This file covers:

  * `ServerBridge.trigger_refresh` — set-the-event semantics.
  * `_reconcile_tools` — drives `set_modes` once per refresh.
  * SSE `tools/list_changed` callback wiring.
  * `Agent.update_info` — outside-of-task-context path used by
    `set_modes` for the wire-broadcast hop.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# ServerBridge.trigger_refresh
# ===========================================================================


def test_trigger_refresh_is_idempotent_and_sync() -> None:
    """trigger_refresh just sets an asyncio.Event. Multiple calls
    before the loop picks up coalesce. NOT async — must be safe to
    call from a sync callback (the SSE notification handler)."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    assert not inspect.iscoroutinefunction(ServerBridge.trigger_refresh)


def test_trigger_refresh_sets_internal_event() -> None:
    """Source pin: trigger_refresh just sets the event."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge.trigger_refresh)
    assert "self._refresh_event.set()" in src


# ===========================================================================
# Reconcile flow — one set_modes call per refresh
# ===========================================================================


def _stub_server_bridge(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Construct a ServerBridge with a stubbed MCP client +
    AdminClient. Sufficient for testing reconcile without touching
    real network."""
    from bp_mcp_bridge.admin_client import AdminClient
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow(
        server_id="fs", url="https://x/", transport="streamable_http",
        auth_kind="none", auth_value_ref=None, auth_header_name=None,
        groups=[], expose_to_llm=True, refresh_requested_at=None,
    )
    admin = MagicMock(spec=AdminClient)
    admin.record_tools_refreshed = AsyncMock(return_value=None)
    admin.issue_service_invitation = AsyncMock(return_value=None)
    bridge = ServerBridge(
        row,
        admin_client=admin,
        router_url="ws://router/v1/agent",
        state_dir=tmp_path,
    )
    return bridge, admin


def test_reconcile_calls_set_modes_with_all_tools() -> None:
    """Source pin: _apply_tools (called from _reconcile_tools) drives
    one `Agent.set_modes` call covering every current tool — that IS
    the diff loop. No per-tool spawn / evict / `_update_tool_schema`
    branches survive."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    apply_src = inspect.getsource(ServerBridge._apply_tools)
    assert "self._agent.set_modes(" in apply_src
    # The per-tool spawn/evict/update_tool_schema methods are gone.
    assert not hasattr(ServerBridge, "_spawn_tool")
    assert not hasattr(ServerBridge, "_evict_tool")
    assert not hasattr(ServerBridge, "_update_tool_schema")
    assert not hasattr(ServerBridge, "_initial_spawn")


def test_reconcile_records_tools_refreshed_after_apply() -> None:
    """The admin API tools_cache must reflect the post-reconcile
    state, not the pre. Source pin on the post-apply call."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._reconcile_tools)
    assert "await self._record_tools_refreshed(new_tools)" in src


def test_reconcile_logs_tool_count() -> None:
    """Operator visibility: each reconcile pass logs the new tool
    count. The finer added/removed/schema_changed breakdown is
    metric-only via `tool_reconcile_changes_total`; the log holds
    the cardinality that matters at a glance."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._reconcile_tools)
    assert '"mcp_server_bridge_reconciled"' in src
    assert '"tool_count": len(new_tools)' in src


def test_reconcile_emits_add_remove_metrics(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Behavioral: a reconcile with mixed added/removed/schema-changed
    tools bumps the `tool_reconcile_changes_total` counter with the
    right labels (`added`, `removed`, `schema_changed`)."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge import metrics
    from bp_mcp_bridge.mcp_client import ToolDefinition

    bridge, _admin = _stub_server_bridge(tmp_path)

    tool_a = ToolDefinition(
        name="tool_a", description="A", input_schema={"type": "object"},
    )
    tool_b_v1 = ToolDefinition(
        name="tool_b", description="B",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    tool_b_v2 = ToolDefinition(
        name="tool_b", description="B",
        input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    tool_c = ToolDefinition(
        name="tool_c", description="C", input_schema={"type": "object"},
    )

    # Seed the bridge with round-1 state.
    bridge._known_tools = [tool_a, tool_b_v1]
    bridge._agent = MagicMock()
    bridge._agent.set_modes = AsyncMock(return_value=None)
    bridge._agent.update_info = AsyncMock(return_value=None)

    def _read(label: str) -> float:
        m = metrics.tool_reconcile_changes_total.labels(
            server_id="fs", change=label,
        )
        return m._value.get()

    before_added = _read("added")
    before_removed = _read("removed")
    before_changed = _read("schema_changed")

    # Round 2: tool_a unchanged, tool_b schema changed, tool_c added.
    asyncio.run(bridge._apply_tools([tool_a, tool_b_v2, tool_c]))
    assert _read("added") - before_added == 1
    assert _read("removed") - before_removed == 0
    assert _read("schema_changed") - before_changed == 1

    # Round 3: tool_b removed, nothing else changes.
    asyncio.run(bridge._apply_tools([tool_a, tool_c]))
    assert _read("removed") - before_removed == 1


def test_reconcile_updates_capabilities_only_when_names_change(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Capabilities follow tool *names*. A pure schema-only change
    leaves the capability list alone (no AgentInfoUpdate hop wasted
    on a no-op). An add/remove triggers the update_info call.

    Brief window where catalog has new tools + old capabilities
    (≤ one ack RTT) is the documented tradeoff — capability-PATTERN
    rules (`mcp.tool.*`) still match either way."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import ToolDefinition

    bridge, _admin = _stub_server_bridge(tmp_path)

    tool_a_v1 = ToolDefinition(
        name="tool_a", description="A",
        input_schema={"type": "object", "properties": {}},
    )
    tool_a_v2 = ToolDefinition(
        name="tool_a", description="A",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    tool_b = ToolDefinition(
        name="tool_b", description="B", input_schema={"type": "object"},
    )

    bridge._known_tools = [tool_a_v1]
    bridge._agent = MagicMock()
    bridge._agent.set_modes = AsyncMock(return_value=None)
    bridge._agent.update_info = AsyncMock(return_value=None)

    # Schema-only change: same names → no update_info call.
    asyncio.run(bridge._apply_tools([tool_a_v2]))
    bridge._agent.update_info.assert_not_called()

    # Add a tool: names changed → update_info called once.
    asyncio.run(bridge._apply_tools([tool_a_v2, tool_b]))
    bridge._agent.update_info.assert_called_once()
    kwargs = bridge._agent.update_info.call_args.kwargs
    assert "capabilities" in kwargs
    assert "description" in kwargs


def test_run_loop_tears_down_agent_on_exit() -> None:
    """`finally` block in run() must cancel the per-server agent
    task so it doesn't outlive the bridge."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge.run)
    assert "self._tear_down_agent()" in src


def test_run_wires_on_tools_changed_callback() -> None:
    """SSE bridge subscribes to tools/list_changed via the
    callback handed to build_mcp_client. Streamable HTTP gets
    the same kwarg but doesn't use it (no server-push channel)."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge.run)
    assert "on_tools_changed=on_tools_changed" in src
    assert "self.trigger_refresh" in src


def test_set_modes_handler_factory_uses_make_tool_handler() -> None:
    """Source pin: the bridge constructs each mode's handler via
    `make_tool_handler(mcp_client, tool.name, server_id)` — the
    closure-captured tool name is what keys the upstream
    `mcp_client.call_tool` invocation."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._apply_tools)
    assert "make_tool_handler(" in src
    # Pin the input_schema being threaded through as the mode's
    # accepts_schema entry.
    assert "t.input_schema" in src


# ===========================================================================
# SSE notification callback wiring
# ===========================================================================


def test_sse_client_accepts_on_tools_changed_kwarg() -> None:
    """Constructor signature must accept the callback."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    sig = inspect.signature(SseMcpClient.__init__)
    assert "on_tools_changed" in sig.parameters
    assert sig.parameters["on_tools_changed"].default is None


def test_sse_handle_event_dispatches_tools_list_changed() -> None:
    """When the server pushes
    notifications/tools/list_changed, the SSE client invokes
    the configured callback."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    fired = []
    client = SseMcpClient("https://x/", on_tools_changed=lambda: fired.append(1))

    async def drive():
        await client._handle_event(
            "message",
            '{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}',
        )

    asyncio.run(drive())
    assert fired == [1]


def test_sse_handle_event_other_notifications_still_dropped() -> None:
    """Only tools/list_changed dispatches; other notification
    methods are silently ignored (no crash, no callback)."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    fired = []
    client = SseMcpClient("https://x/", on_tools_changed=lambda: fired.append(1))

    async def drive():
        await client._handle_event(
            "message",
            '{"jsonrpc":"2.0","method":"notifications/something_else"}',
        )

    asyncio.run(drive())
    assert fired == []


def test_sse_callback_exception_does_not_crash_stream() -> None:
    """If on_tools_changed raises, the SSE client logs and
    continues — the stream loop survives broken callbacks."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    def bad_callback():
        raise RuntimeError("oops")

    client = SseMcpClient("https://x/", on_tools_changed=bad_callback)

    async def drive():
        # Should NOT raise.
        await client._handle_event(
            "message",
            '{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}',
        )

    asyncio.run(drive())


def test_build_mcp_client_passes_callback_to_sse() -> None:
    from bp_mcp_bridge.mcp_client import SseMcpClient, build_mcp_client

    callback = lambda: None  # noqa: E731
    client = build_mcp_client(
        "sse", "https://x/", on_tools_changed=callback,
    )
    assert isinstance(client, SseMcpClient)
    assert client._on_tools_changed is callback


def test_build_mcp_client_silently_drops_callback_for_streamable_http() -> None:
    """Streamable HTTP has no server-push channel for
    notifications. The kwarg is accepted for API symmetry but
    silently dropped — callers don't have to branch on transport."""
    from bp_mcp_bridge.mcp_client import (
        StreamableHttpMcpClient,
        build_mcp_client,
    )

    callback = lambda: None  # noqa: E731
    client = build_mcp_client(
        "streamable_http", "https://x/", on_tools_changed=callback,
    )
    assert isinstance(client, StreamableHttpMcpClient)
    # Streamable HTTP doesn't carry the callback at all.
    assert not hasattr(client, "_on_tools_changed")


# ===========================================================================
# Agent.update_info — outside-of-task-context path used by set_modes
# ===========================================================================


def test_agent_update_info_exists() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.agent import Agent

    assert hasattr(Agent, "update_info")
    assert inspect.iscoroutinefunction(Agent.update_info)


def test_agent_update_info_raises_when_not_connected() -> None:
    """If the agent hasn't run yet (no dispatcher), the helper
    must surface a clear RuntimeError rather than AttributeError."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent

    agent = Agent(info=AgentInfo(agent_id="t_up_1", description="d"))
    # _dispatcher is None pre-run.
    async def drive():
        with pytest.raises(RuntimeError, match="not connected"):
            await agent.update_info(description="new")

    asyncio.run(drive())


def test_agent_update_info_rejects_empty_patch() -> None:
    """Same validation as the wire — agent SDK refuses empty
    patches client-side."""
    pytest.importorskip("fastapi")
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent

    agent = Agent(info=AgentInfo(agent_id="t_up_2", description="d"))
    # Stub the dispatcher so the empty-patch check fires before
    # the not-connected one.
    agent._dispatcher = MagicMock()

    async def drive():
        with pytest.raises(ValueError, match="at least one"):
            await agent.update_info()

    asyncio.run(drive())


def test_agent_update_info_uses_synthetic_trace_ids() -> None:
    """Outside-of-task-context path: trace_id and span_id are
    synthetic (all-zeros) since there's no parent task to
    inherit from. Pin so a future refactor doesn't accidentally
    require a TaskContext."""
    pytest.importorskip("fastapi")
    from bp_sdk.agent import Agent

    src = inspect.getsource(Agent.update_info)
    # 32 zeros / 16 zeros for trace_id / span_id.
    assert 'trace_id="0" * 32' in src
    assert 'span_id="0" * 16' in src


def test_agent_update_info_uses_register_with_none_task_id() -> None:
    """Pin on task_id=None — the agent-level call isn't tied to
    a handler task, so it falls back to the timeout-reaper path
    for ack delivery."""
    pytest.importorskip("fastapi")
    from bp_sdk.agent import Agent

    src = inspect.getsource(Agent.update_info)
    assert "task_id=None" in src


def test_peer_client_update_agent_info_now_delegates_to_agent() -> None:
    """The in-handler convenience method (kept for ergonomics)
    is now a thin wrapper over Agent.update_info."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.update_agent_info)
    assert "self._dispatcher.agent.update_info(" in src
