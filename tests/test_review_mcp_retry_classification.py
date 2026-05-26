"""MCP tool calls retry transient errors with exponential backoff.

Pre-R2: any exception from `call_tool` (network blip, transient
MCP -32603) surfaced immediately as a failed Result. A single
flaky upstream connection caused user-visible task failures
where a tiny retry would have rescued the call.

Retry policy:
  - 3 attempts total (1 initial + 2 retries)
  - Backoff: 0.5s, 1.0s (capped at 4s)
  - Transient: httpx network errors + MCP code -32603
  - Permanent: all other MCP error codes (surface immediately)
  - CancelledError NEVER classified as transient (cancellation
    must propagate)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _bridge_imports_available() -> bool:
    try:
        import bp_mcp_bridge.tool_agent  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _bridge_imports_available(),
    reason="bp_mcp_bridge imports require fastapi / sdk install",
)


def test_is_transient_classifies_httpx_transport_errors() -> None:
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(httpx.ConnectError("refused")) is True
    assert _is_transient(httpx.ReadTimeout("slow")) is True
    assert _is_transient(httpx.RemoteProtocolError("eof")) is True


def test_is_transient_classifies_mcp_internal_error() -> None:
    from bp_mcp_bridge.mcp_client import McpError
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(McpError(-32603, "internal")) is True


def test_is_transient_rejects_mcp_client_errors() -> None:
    """-32600 / -32601 / -32602 + application codes are permanent."""
    from bp_mcp_bridge.mcp_client import McpError
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(McpError(-32600, "invalid request")) is False
    assert _is_transient(McpError(-32601, "method not found")) is False
    assert _is_transient(McpError(-32602, "invalid params")) is False
    # App-defined codes (positive or arbitrary) are also permanent.
    assert _is_transient(McpError(42, "app error")) is False


def test_is_transient_rejects_unrelated_exceptions() -> None:
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(ValueError("bug")) is False
    assert _is_transient(RuntimeError("bug")) is False


def test_retry_succeeds_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call raises a transient error, second succeeds → retry
    returns the second response."""
    from bp_mcp_bridge import tool_agent
    from bp_mcp_bridge.mcp_client import ToolResult

    # Speed up the test — no real backoff sleeps.
    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)
    monkeypatch.setattr(tool_agent, "_BACKOFF_MAX_S", 0.0)

    calls = []

    async def _call_tool(name, payload):
        calls.append((name, payload))
        if len(calls) == 1:
            raise httpx.ConnectError("flake")
        return ToolResult(content=[{"type": "text", "text": "ok"}])

    client = MagicMock()
    client.call_tool = _call_tool
    ctx = MagicMock()
    ctx.log = MagicMock()

    async def _run() -> ToolResult:
        return await tool_agent._call_tool_with_retry(
            client, "t", {"k": "v"}, ctx=ctx, server_id="srv_1"
        )

    result = asyncio.run(_run())
    assert len(calls) == 2
    assert result.content[0]["text"] == "ok"


def test_retry_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient failure: after _MAX_ATTEMPTS attempts the
    final exception propagates."""
    from bp_mcp_bridge import tool_agent

    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)
    monkeypatch.setattr(tool_agent, "_BACKOFF_MAX_S", 0.0)

    calls = []

    async def _call_tool(name, payload):
        calls.append((name, payload))
        raise httpx.ConnectError("always flaky")

    client = MagicMock()
    client.call_tool = _call_tool
    ctx = MagicMock()
    ctx.log = MagicMock()

    async def _run() -> None:
        await tool_agent._call_tool_with_retry(
            client, "t", {"k": "v"}, ctx=ctx, server_id="srv_1"
        )

    with pytest.raises(httpx.ConnectError):
        asyncio.run(_run())
    assert len(calls) == tool_agent._MAX_ATTEMPTS


def test_retry_surfaces_permanent_error_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanent MCP error (-32602 invalid_params) does NOT retry."""
    from bp_mcp_bridge import tool_agent
    from bp_mcp_bridge.mcp_client import McpError

    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)

    calls = []

    async def _call_tool(name, payload):
        calls.append((name, payload))
        raise McpError(-32602, "bad params")

    client = MagicMock()
    client.call_tool = _call_tool
    ctx = MagicMock()
    ctx.log = MagicMock()

    async def _run() -> None:
        await tool_agent._call_tool_with_retry(
            client, "t", {"k": "v"}, ctx=ctx, server_id="srv_1"
        )

    with pytest.raises(McpError):
        asyncio.run(_run())
    assert len(calls) == 1  # no retry


def test_retry_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError must propagate unchanged — even though it's
    technically subclass-not-of `Exception` in modern asyncio, the
    retry helper must NEVER swallow / retry a cancel."""
    from bp_mcp_bridge import tool_agent

    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)

    async def _call_tool(name, payload):
        raise asyncio.CancelledError

    client = MagicMock()
    client.call_tool = _call_tool
    ctx = MagicMock()
    ctx.log = MagicMock()

    async def _run() -> None:
        await tool_agent._call_tool_with_retry(
            client, "t", {"k": "v"}, ctx=ctx, server_id="srv_1"
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())


def test_retry_logs_each_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each retry emits an `mcp_tool_call_retry` log with the
    attempt number, wait duration, and the exception repr."""
    from bp_mcp_bridge import tool_agent
    from bp_mcp_bridge.mcp_client import ToolResult

    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)
    monkeypatch.setattr(tool_agent, "_BACKOFF_MAX_S", 0.0)

    calls = []

    async def _call_tool(name, payload):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow")
        return ToolResult(content=[])

    client = MagicMock()
    client.call_tool = _call_tool
    ctx = MagicMock()
    ctx.log = MagicMock()

    async def _run() -> None:
        await tool_agent._call_tool_with_retry(
            client, "t", {"k": "v"}, ctx=ctx, server_id="srv_1"
        )

    asyncio.run(_run())
    # Two retry log entries (after attempt 1 and attempt 2).
    assert ctx.log.warning.call_count == 2
    last_call = ctx.log.warning.call_args_list[-1]
    assert last_call.args[0] == "mcp_tool_call_retry"
    extra = last_call.kwargs["extra"]
    assert extra["bp.mcp_server_id"] == "srv_1"
    assert extra["bp.mcp_tool"] == "t"
    assert extra["attempt"] == 2


def test_tool_agent_handler_calls_retry_helper() -> None:
    """Source pin: the per-tool mode handler (factory: `make_tool_handler`)
    invokes `_call_tool_with_retry`, not the raw `call_tool`."""
    import inspect

    from bp_mcp_bridge import tool_agent

    src = inspect.getsource(tool_agent.make_tool_handler)
    assert "_call_tool_with_retry(" in src
