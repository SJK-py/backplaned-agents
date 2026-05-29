"""Tests for Phase 10d: SSE MCP transport.

Two layers:

  * Factory selection — `build_mcp_client` picks the right class
    per `transport`.
  * SSE client wire shape — JSON-RPC over the bidirectional
    SSE-GET + POST channels: endpoint event handling, response
    correlation by id, error propagation, notification drop,
    aclose semantics.

End-to-end live test against a real MCP SSE server is in the
PR's manual checklist.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

# ===========================================================================
# Factory
# ===========================================================================


def test_factory_returns_streamable_http_for_default_transport() -> None:
    from bp_mcp_bridge.mcp_client import (
        StreamableHttpMcpClient,
        build_mcp_client,
    )

    client = build_mcp_client("streamable_http", "https://x/")
    assert isinstance(client, StreamableHttpMcpClient)


def test_factory_returns_sse_for_sse_transport() -> None:
    from bp_mcp_bridge.mcp_client import SseMcpClient, build_mcp_client

    client = build_mcp_client("sse", "https://x/sse")
    assert isinstance(client, SseMcpClient)


def test_factory_rejects_unknown_transport() -> None:
    from bp_mcp_bridge.mcp_client import build_mcp_client

    with pytest.raises(ValueError, match="unknown MCP transport"):
        build_mcp_client("websocket", "https://x/")


def test_factory_passes_auth_through() -> None:
    """Auth kwargs reach the underlying client regardless of
    transport choice."""
    from bp_mcp_bridge.mcp_client import build_mcp_client

    sse = build_mcp_client(
        "sse", "https://x/", auth_kind="bearer", auth_value="tok",
    )
    assert sse._headers["Authorization"] == "Bearer tok"

    http = build_mcp_client(
        "streamable_http", "https://x/", auth_kind="bearer", auth_value="tok",
    )
    assert http._headers["Authorization"] == "Bearer tok"


# ===========================================================================
# SSE — header construction
# ===========================================================================


def test_sse_client_sets_event_stream_accept_header() -> None:
    """SSE GET requires `Accept: text/event-stream`. The POST
    channel uses `application/json` — both must be set
    independently."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")
    assert client._sse_headers["Accept"] == "text/event-stream"
    assert client._post_headers["Content-Type"] == "application/json"


def test_sse_client_propagates_auth_to_both_channels() -> None:
    """The same auth credential must appear on both the SSE GET
    and the outbound POST."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient(
        "https://x/", auth_kind="bearer", auth_value="tok",
    )
    assert client._sse_headers["Authorization"] == "Bearer tok"
    assert client._post_headers["Authorization"] == "Bearer tok"


# ===========================================================================
# SSE — endpoint event handling
# ===========================================================================


def test_sse_endpoint_event_resolves_relative_post_url() -> None:
    """MCP spec is ambiguous about whether the endpoint URL is
    absolute or relative. The client resolves it against the SSE
    URL via urljoin, so `/messages` becomes
    `https://server.com/messages`."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.com/sse")

    async def drive():
        await client._handle_event("endpoint", "/messages")
        assert client._post_url == "https://server.com/messages"
        assert client._endpoint_event.is_set()

    asyncio.run(drive())


def test_sse_endpoint_event_accepts_same_origin_absolute_url() -> None:
    """Some servers emit absolute URLs. The SSE endpoint resolver
    accepts an absolute URL as long as its origin (scheme + host +
    port) matches the configured SSE URL — preventing a hostile /
    compromised server from redirecting POST traffic (and its
    bearer credential) to an attacker-controlled origin."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.com/sse")

    async def drive():
        await client._handle_event(
            "endpoint", "https://server.com/post"
        )
        assert client._post_url == "https://server.com/post"

    asyncio.run(drive())


def test_sse_endpoint_event_refuses_cross_origin_absolute_url() -> None:
    """Cross-origin URL: refused. `_post_url` stays None so
    subsequent RPCs fail with a clear error. No silent acceptance,
    no bearer credential leak to attacker."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.com/sse")

    async def drive():
        await client._handle_event(
            "endpoint", "https://attacker.example/post"
        )
        assert client._post_url is None, (
            "Cross-origin endpoint URL must be refused — otherwise "
            "the bridge POSTs subsequent requests + bearer to the "
            "attacker host."
        )

    asyncio.run(drive())


def test_sse_endpoint_event_treats_default_ports_as_same_origin() -> None:
    """`https://server.com/sse` and `https://server.com:443/post`
    are the same origin. The check must canonicalise default ports."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.com/sse")

    async def drive():
        await client._handle_event(
            "endpoint", "https://server.com:443/post"
        )
        assert client._post_url == "https://server.com:443/post"

    asyncio.run(drive())


# ===========================================================================
# SSE — message event correlation
# ===========================================================================


def test_sse_message_event_resolves_pending_future_by_id() -> None:
    """When a JSON-RPC response arrives via SSE, the client looks
    up the matching pending future by `id` and resolves it."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        fut: asyncio.Future = loop.create_future()
        client._pending[42] = fut
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":42,"result":{"tools":[]}}',
            )
        )
        assert fut.done()
        assert fut.result() == {"tools": []}
    finally:
        loop.close()


def test_sse_message_event_propagates_jsonrpc_error() -> None:
    """JSON-RPC error objects become McpError exceptions on the
    pending future."""
    import asyncio

    from bp_mcp_bridge.mcp_client import McpError, SseMcpClient

    client = SseMcpClient("https://x/")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        fut: asyncio.Future = loop.create_future()
        client._pending[7] = fut
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":7,"error":{"code":-32602,"message":"bad params"}}',
            )
        )
        assert fut.done()
        with pytest.raises(McpError, match="bad params"):
            fut.result()
    finally:
        loop.close()


def test_sse_notification_drops_silently_in_phase_10d() -> None:
    """Notifications (no `id`) are ignored in Phase 10d.
    `tools/list_changed` handling would route through here in a
    future phase."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")

    async def drive():
        # Should not raise.
        await client._handle_event(
            "message",
            '{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}',
        )

    asyncio.run(drive())


def test_sse_invalid_json_in_message_ignored_with_warning() -> None:
    """Malformed JSON in a `message` event must not crash the
    stream loop — log and move on."""
    import asyncio
    import logging

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")

    async def drive():
        # Should not raise.
        await client._handle_event("message", "not-json{")

    with patch.object(logging.getLogger("bp_mcp_bridge.mcp_client"), "warning") as warn:
        asyncio.run(drive())
        # At least one warning logged.
        assert warn.called


def test_sse_unknown_event_type_ignored() -> None:
    """Future event types or spec extensions don't crash the
    parser — silently dropped."""
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")

    async def drive():
        await client._handle_event("future_event_type", '{"x": 1}')

    asyncio.run(drive())


# ===========================================================================
# SSE — RPC requires endpoint event first
# ===========================================================================


def test_sse_call_before_endpoint_received_raises() -> None:
    """Calling RPC before the SSE stream emitted the endpoint
    event raises a clear error — the POST URL isn't known yet."""
    import asyncio

    from bp_mcp_bridge.mcp_client import McpError, SseMcpClient

    client = SseMcpClient("https://x/")

    async def drive():
        with pytest.raises(McpError, match="endpoint URL not yet"):
            await client._call("tools/list", {})

    asyncio.run(drive())


def test_sse_list_tools_requires_initialize() -> None:
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")
    with pytest.raises(RuntimeError, match="initialize"):
        asyncio.run(client.list_tools())


def test_sse_call_tool_requires_initialize() -> None:
    import asyncio

    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/")
    with pytest.raises(RuntimeError, match="initialize"):
        asyncio.run(client.call_tool("read_file", {"path": "/x"}))


# ===========================================================================
# SSE — initialize handshake shape
# ===========================================================================


def test_sse_initialize_waits_for_endpoint_event() -> None:
    """Source pin: initialize() awaits self._endpoint_event before
    sending any RPC. Without this, the first POST would race the
    SSE handshake."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient.initialize)
    assert "self._endpoint_event.wait()" in src
    assert "asyncio.wait_for" in src


def test_sse_initialize_sends_notifications_initialized() -> None:
    """MCP spec: client MUST send `notifications/initialized` after
    receiving the initialize response. Same pattern as Streamable
    HTTP."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient.initialize)
    assert '"notifications/initialized"' in src


def test_sse_initialize_raises_on_endpoint_timeout() -> None:
    """If the server doesn't emit an endpoint event within 10s
    (typical: operator configured a Streamable HTTP server with
    transport=sse), raise a clear error pointing at the
    transport mismatch."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient.initialize)
    assert "did not emit an" in src
    assert "transport setting" in src


# ===========================================================================
# SSE — call() shape
# ===========================================================================


def test_sse_call_registers_pending_future() -> None:
    """Source pin: each _call adds a per-id Future to _pending and
    awaits it. The SSE stream handler resolves the future when the
    matching response arrives."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._call)
    assert "self._pending[rid] = fut" in src
    assert "asyncio.wait_for(fut" in src


def test_sse_call_cleans_up_pending_on_post_failure() -> None:
    """If the POST request itself fails (network error, 5xx), the
    pending future must be removed so memory doesn't leak across
    retries."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._call)
    assert "self._pending.pop(rid, None)" in src


def test_sse_call_handles_inline_200_response_too() -> None:
    """Some MCP servers return 200 with inline JSON on the POST
    instead of 202+stream. The client handles both shapes."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._call)
    assert "resp.status_code == 200" in src


# ===========================================================================
# SSE — aclose semantics
# ===========================================================================


def test_sse_aclose_cancels_stream_task() -> None:
    """Source pin: aclose() cancels the background stream task
    and awaits it (with cancellation absorbed) so the asyncio
    runtime doesn't warn about unawaited tasks on shutdown."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient.aclose)
    assert "self._stream_task.cancel()" in src


def test_sse_aclose_rejects_pending_futures() -> None:
    """In-flight requests waiting for SSE responses must wake up
    when the client closes — otherwise callers hang on
    `wait_for` until the configured timeout."""
    import asyncio

    from bp_mcp_bridge.mcp_client import McpError, SseMcpClient

    client = SseMcpClient("https://x/")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        fut: asyncio.Future = loop.create_future()
        client._pending[1] = fut

        async def drive():
            await client.aclose()

        loop.run_until_complete(drive())
        assert fut.done()
        with pytest.raises(McpError, match="closed before response"):
            fut.result()
    finally:
        loop.close()


# ===========================================================================
# SSE — stream loop parsing
# ===========================================================================


def test_sse_stream_loop_parses_event_and_data_lines() -> None:
    """Source pin: the parser accumulates `event:` and `data:`
    lines, flushes on blank line, and dispatches via
    `_handle_event`. SSE comment lines (starting with `:`) are
    ignored."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    assert 'line.startswith("event:")' in src
    assert 'line.startswith("data:")' in src
    assert 'line.startswith(":")' in src
    # Blank-line boundary.
    assert "if not line:" in src
    # Dispatch.
    assert "self._handle_event(" in src


def test_sse_stream_loop_reads_via_httpx_stream() -> None:
    """The SSE GET uses `client.stream(...)` (long-lived) rather
    than `client.get(...)` (single response). Pin the call shape
    so a future refactor can't accidentally swap them."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    assert 'self._client.stream(' in src
    assert '"GET"' in src


def test_sse_stream_loop_reconnects_on_transient_failure() -> None:
    """The stream loop now reconnects on transient failures
    (network blip, server restart) rather than failing every
    pending future and dying — the previous shape required an
    admin "Refresh tools" click to recover from a 30s network
    blip. Source pin on the reconnect-loop shape."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    assert "while not self._closed:" in src, (
        "Stream loop must be wrapped in a reconnect-while-loop."
    )
    assert "backoff" in src, (
        "Reconnects must use backoff so we don't burn CPU on a "
        "permanently-down server."
    )
    assert "Last-Event-ID" in src, (
        "On reconnect, send the `Last-Event-ID` header (WHATWG SSE) "
        "so the server can resume after the last seen event."
    )


def test_sse_stream_loop_does_not_fail_pending_on_drop() -> None:
    """On stream drop, in-flight `_pending` futures must NOT be
    failed — the corresponding POST may have already been accepted
    by the server and the response can arrive on the reconnected
    stream. Pending futures fall back to their own
    `_RESPONSE_TIMEOUT_S` if no response ever shows up."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    # Old shape called `fut.set_exception` from inside the
    # exception handler. New shape doesn't.
    assert "fut.set_exception" not in src, (
        "Stream loop must not fail in-flight futures on drop — "
        "they may still be served on the reconnected stream."
    )


# ===========================================================================
# Integration: ServerBridge selects SSE transport
# ===========================================================================


def test_server_bridge_uses_factory_for_transport_selection() -> None:
    """Source pin: ServerBridge.run() calls `build_mcp_client(...)`
    rather than instantiating a specific client class. New
    transports get added at the factory; ServerBridge stays
    transport-agnostic."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge.run)
    assert "build_mcp_client(" in src
    assert "self._row.transport" in src
