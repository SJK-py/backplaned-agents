"""R8 HIGH: SSE in-flight requests fail fast on stream reconnect.

`SseMcpClient` POSTs a JSON-RPC request, then awaits the response
as an SSE `message` event keyed by request `id`. When the SSE
stream drops and reconnects:

  - The reconnected stream is a NEW session (new `endpoint` event,
    new session id from the server's perspective).
  - SSE-transport MCP servers route a request's response to the
    SSE session the request was correlated to. A response
    generated for the dead session is almost never replayed onto
    the new one — `Last-Event-ID` resumes the *event log*, not
    per-request response routing.

Pre-R8 the in-flight `_pending` futures were deliberately left
intact across reconnect ("the response may still arrive on the
reconnected stream"). For the overwhelming majority of servers it
does NOT, so every in-flight call hung the full
`_RESPONSE_TIMEOUT_S` (60 s) before failing — a 60-second latency
cliff on every transient network blip.

Fix: `_fail_pending_for_reconnect()` fails the stranded futures
with a retryable `McpError(-32603)` (in
`tool_agent._MCP_TRANSIENT_CODES`) so `_call_tool_with_retry`
re-issues the call on the fresh stream in sub-second time. A late
duplicate response for an already-failed id is dropped harmlessly
by `_handle_event` (`fut is None or fut.done()`).
"""
from __future__ import annotations

import asyncio
import inspect

from bp_mcp_bridge.mcp_client import McpError, SseMcpClient


def test_fail_pending_for_reconnect_fails_inflight_with_retryable_error() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = SseMcpClient("https://x/")
        fut1: asyncio.Future = loop.create_future()
        fut2: asyncio.Future = loop.create_future()
        client._pending[1] = fut1
        client._pending[2] = fut2

        client._fail_pending_for_reconnect()

        assert fut1.done() and fut2.done()
        for fut in (fut1, fut2):
            exc = fut.exception()
            assert isinstance(exc, McpError)
            # -32603 is the transient code `_call_tool_with_retry`
            # retries on.
            assert exc.code == -32603
        # `_pending` is cleared so a stale entry can't shadow a
        # future request.
        assert client._pending == {}
    finally:
        loop.close()


def test_fail_pending_is_noop_when_no_inflight() -> None:
    """Idle-stream reconnect (no requests outstanding) must be a
    no-op — not log spam, not an error."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = SseMcpClient("https://x/")
        assert client._pending == {}
        client._fail_pending_for_reconnect()  # should not raise
        assert client._pending == {}
    finally:
        loop.close()


def test_fail_pending_skips_already_done_futures() -> None:
    """A future that already resolved (response arrived just before
    the drop) must not be clobbered with the reconnect error."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = SseMcpClient("https://x/")
        done_fut: asyncio.Future = loop.create_future()
        done_fut.set_result({"ok": True})
        pending_fut: asyncio.Future = loop.create_future()
        client._pending[1] = done_fut
        client._pending[2] = pending_fut

        client._fail_pending_for_reconnect()

        # The already-resolved future keeps its result.
        assert done_fut.result() == {"ok": True}
        # The genuinely-pending one is failed retryably.
        assert isinstance(pending_fut.exception(), McpError)
    finally:
        loop.close()


def test_retryable_code_matches_tool_agent_transient_set() -> None:
    """The error code the reconnect handler uses MUST be in
    `tool_agent._MCP_TRANSIENT_CODES`, otherwise the retry layer
    treats it as permanent and the fix achieves nothing — the
    call fails instead of retrying on the reconnected stream."""
    from bp_mcp_bridge.tool_agent import _MCP_TRANSIENT_CODES

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = SseMcpClient("https://x/")
        fut: asyncio.Future = loop.create_future()
        client._pending[99] = fut
        client._fail_pending_for_reconnect()
        exc = fut.exception()
        assert isinstance(exc, McpError)
        assert exc.code in _MCP_TRANSIENT_CODES
    finally:
        loop.close()


def test_late_duplicate_response_after_fail_is_dropped_harmlessly() -> None:
    """After a pending future is failed by the reconnect handler,
    a late duplicate response for the same id (the rare buffering
    server) must be dropped without raising — the future is already
    popped, so `_handle_event` sees `fut is None`."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = SseMcpClient("https://x/")
        fut: asyncio.Future = loop.create_future()
        client._pending[5] = fut
        client._fail_pending_for_reconnect()
        assert fut.done()

        # Server replays the response on the new stream. Must NOT
        # raise (the id is gone from `_pending`).
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":5,"result":{"tools":[]}}',
            )
        )
        # The failed future keeps its McpError; the late result is
        # silently discarded.
        assert isinstance(fut.exception(), McpError)
    finally:
        loop.close()


def test_stream_loop_calls_fail_pending_before_reconnect_sleep() -> None:
    """Source pin: the reconnect path in `_stream_loop` must call
    `_fail_pending_for_reconnect()` before the backoff sleep, so
    callers don't wait the full response timeout."""
    src = inspect.getsource(SseMcpClient._stream_loop)
    assert "_fail_pending_for_reconnect()" in src
    # And it must be reached on BOTH the exception path and the
    # clean-close path — i.e. placed after the `except` block but
    # before the reconnect `sleep`, not nested inside the `except`.
    fail_idx = src.index("_fail_pending_for_reconnect()")
    sleep_idx = src.index("asyncio.sleep(backoff)")
    assert fail_idx < sleep_idx, (
        "fail-pending must run before the reconnect sleep"
    )


def test_closed_client_skips_reconnect_and_fail_pending() -> None:
    """When the client is shutting down (`_closed=True`), the loop
    returns before failing pending — `aclose()` owns that path and
    fails them with its own 'client closed' message. Source pin the
    ordering so a refactor can't accidentally double-fail."""
    src = inspect.getsource(SseMcpClient._stream_loop)
    closed_return_idx = src.index("if self._closed:\n                return")
    fail_idx = src.index("_fail_pending_for_reconnect()")
    assert closed_return_idx < fail_idx, (
        "the `if self._closed: return` guard must precede "
        "_fail_pending_for_reconnect so a shutting-down client "
        "doesn't run the reconnect-fail path"
    )
