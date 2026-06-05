"""R8 MEDIUM: MCP bridge robustness (4 batched findings).

Fresh-eyes R8 review of the MCP bridge surfaced four MEDIUM
correctness/robustness gaps in the JSON-RPC client and the
bridge startup/reconcile lifecycle:

1. **SSE JSON-RPC id type mismatch** — we always send int ids, but
   JSON-RPC 2.0 permits string ids and a non-strict server (or a
   re-parsing proxy) can echo `1` as `"1"` / `1.0`. The
   type-mismatched key never matches → the future is never
   resolved → every call hangs the full 60 s timeout, looking
   like a dead upstream.

2. **`run()` published the catalog before onboarding** —
   `_record_tools_refreshed` stamped the row healthy + cleared
   `refresh_requested_at` BEFORE `_spawn_agent`. A spawn failure
   then left the row advertising N tools with a dead agent.

3. **Lost refresh on transient reconcile failure** —
   `_refresh_loop` cleared the event before `_reconcile_tools`; a
   transient `list_tools()` error dropped the refresh entirely
   until the next external trigger (which for streamable_http may
   never come).

4. **Non-JSON 2xx body → opaque permanent error** — `resp.json()`
   on an empty/HTML/text 200 raised a bare `JSONDecodeError`
   which `_is_transient` classifies as permanent, so a flaky
   proxy returning HTML 200 fails the call instead of retrying.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# (1) SSE JSON-RPC id-type normalization
# ===========================================================================


def _sse_client():  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.mcp_client import SseMcpClient

    return SseMcpClient("https://x/")


def test_sse_resolves_future_when_server_echoes_string_id() -> None:
    """Server replies with `"id": "42"` (string) for a request we
    sent as int 42. The int-coerced fallback must still resolve."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = _sse_client()
        fut: asyncio.Future = loop.create_future()
        client._pending[42] = fut
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":"42","result":{"ok":true}}',
            )
        )
        assert fut.done()
        assert fut.result() == {"ok": True}
        assert client._pending == {}
    finally:
        loop.close()


def test_sse_resolves_future_when_server_echoes_float_id() -> None:
    """A proxy that JSON-reparses can turn `42` into `42.0`."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = _sse_client()
        fut: asyncio.Future = loop.create_future()
        client._pending[7] = fut
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":7.0,"result":{"x":1}}',
            )
        )
        assert fut.result() == {"x": 1}
    finally:
        loop.close()


def test_sse_exact_int_id_still_matches_first() -> None:
    """Sanity: the common compliant case (int echoed as int) still
    resolves on the primary lookup, no coercion needed."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = _sse_client()
        fut: asyncio.Future = loop.create_future()
        client._pending[5] = fut
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":5,"result":{}}',
            )
        )
        assert fut.result() == {}
    finally:
        loop.close()


def test_sse_unknown_string_id_dropped_not_crashed() -> None:
    """A string id with no matching pending future (and not even
    int-coercible) is dropped silently — no KeyError, no crash."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = _sse_client()
        # Must not raise.
        loop.run_until_complete(
            client._handle_event(
                "message",
                '{"jsonrpc":"2.0","id":"not-a-number","result":{}}',
            )
        )
    finally:
        loop.close()


# ===========================================================================
# (4) Non-JSON 2xx body → retryable McpError(-32603)
# ===========================================================================


def test_streamable_http_non_json_body_raises_retryable_mcperror() -> None:
    from bp_mcp_bridge.mcp_client import McpError, StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import _MCP_TRANSIENT_CODES

    client = StreamableHttpMcpClient("https://x/")

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"<html>502 from proxy</html>"

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    async def _post(*a: Any, **k: Any) -> Any:
        return _Resp()

    client._client.post = _post  # type: ignore[assignment]

    async def _run() -> None:
        with pytest.raises(McpError) as exc:
            await client._call("tools/list", {})
        assert exc.value.code == -32603
        assert exc.value.code in _MCP_TRANSIENT_CODES

    asyncio.run(_run())


def test_sse_inline_200_non_json_raises_retryable_mcperror() -> None:
    from bp_mcp_bridge.mcp_client import McpError, SseMcpClient
    from bp_mcp_bridge.tool_agent import _MCP_TRANSIENT_CODES

    client = SseMcpClient("https://x/")
    client._post_url = "https://x/messages"

    class _Resp:
        status_code = 200
        content = b"not json"

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            raise ValueError("bad json")

    async def _post(*a: Any, **k: Any) -> Any:
        return _Resp()

    client._client.post = _post  # type: ignore[assignment]

    async def _run() -> None:
        with pytest.raises(McpError) as exc:
            await client._call("tools/list", {})
        assert exc.value.code == -32603
        assert exc.value.code in _MCP_TRANSIENT_CODES

    asyncio.run(_run())


def test_streamable_http_valid_json_still_works() -> None:
    """Sanity: the guard doesn't break the happy path."""
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    client = StreamableHttpMcpClient("https://x/")

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"result":{"tools":[]}}'

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return {"result": {"tools": []}}

    async def _post(*a: Any, **k: Any) -> Any:
        return _Resp()

    client._client.post = _post  # type: ignore[assignment]
    assert asyncio.run(client._call("tools/list", {})) == {"tools": []}


# ===========================================================================
# (2) run() onboards before publishing the catalog
# ===========================================================================


def test_run_records_tools_after_spawn_agent() -> None:
    """Source pin: `_spawn_agent` (per-server agent build + onboard
    task creation) must be awaited BEFORE `_record_tools_refreshed`
    in `run()`, so a spawn failure can't leave the row advertising a
    healthy server with no agent."""
    from bp_mcp_bridge import server_bridge

    src = inspect.getsource(server_bridge.ServerBridge.run)
    spawn_idx = src.index("self._spawn_agent(self._enabled_tools(tools))")
    record_idx = src.index("self._record_tools_refreshed(tools)")
    assert spawn_idx < record_idx, (
        "run() must onboard the per-server agent (_spawn_agent) "
        "before publishing the catalog (_record_tools_refreshed)"
    )


def test_run_spawn_failure_does_not_record_tools(tmp_path: Path) -> None:
    """Behavioural: when `_spawn_agent` raises, the bridge must
    NOT have called `_record_tools_refreshed` (the row stays
    un-stamped so the operator sees the server as unhealthy)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import server_bridge as sb

    row = sb.ServerBridgeRow(
        server_id="srv1", url="https://x/", transport="streamable_http",
        auth_kind="none", auth_value_ref=None, auth_header_name=None,
        groups=["mcp_bridge"], expose_to_llm=True, refresh_requested_at=None,
        pending_invitation_token="inv-tok",  # past the onboarding gate
    )
    bridge = sb.ServerBridge(
        row, admin_client=MagicMock(), router_url="ws://r/",
        state_dir=tmp_path,
    )

    recorded: list[Any] = []

    class _FakeClient:
        async def initialize(self) -> dict:
            return {}

        async def list_tools(self) -> list:
            from bp_mcp_bridge.mcp_client import ToolDefinition
            return [ToolDefinition(name="t", description="d",
                                   input_schema={"type": "object"})]

        async def aclose(self) -> None:
            pass

    # Patch build_mcp_client to return our fake.
    import bp_mcp_bridge.server_bridge as sbmod

    def _fake_build(*a: Any, **k: Any) -> Any:
        return _FakeClient()

    bridge._record_tools_refreshed = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda *a, **k: recorded.append(1)
    )

    async def _boom(_tools: Any) -> None:
        raise RuntimeError("onboard failed")

    bridge._spawn_agent = _boom  # type: ignore[method-assign]
    bridge._tear_down_agent = AsyncMock()  # type: ignore[method-assign]

    import unittest.mock as um
    with um.patch.object(sbmod, "build_mcp_client", _fake_build), \
         um.patch.object(sbmod, "resolve_auth_value", lambda _r: None):
        with pytest.raises(RuntimeError, match="onboard failed"):
            asyncio.run(bridge.run())

    assert recorded == [], (
        "_record_tools_refreshed must NOT run when _spawn_agent fails"
    )


# ===========================================================================
# (3) Re-arm refresh event on transient reconcile failure
# ===========================================================================


def test_refresh_loop_rearms_event_on_reconcile_failure(tmp_path: Path) -> None:
    """When `_reconcile_tools` raises, `_refresh_loop` must re-set
    the event (after a backoff) so the refresh isn't lost."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import server_bridge as sb

    row = sb.ServerBridgeRow(
        server_id="srv1", url="https://x/", transport="streamable_http",
        auth_kind="none", auth_value_ref=None, auth_header_name=None,
        groups=["mcp_bridge"], expose_to_llm=True, refresh_requested_at=None,
        pending_invitation_token="inv-tok",  # past the onboarding gate
    )
    bridge = sb.ServerBridge(
        row, admin_client=MagicMock(), router_url="ws://r/",
        state_dir=tmp_path,
    )

    calls = {"n": 0}

    async def _reconcile() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient list_tools failure")
        # Second call succeeds → stop the loop.
        raise asyncio.CancelledError

    bridge._reconcile_tools = _reconcile  # type: ignore[method-assign]

    # Make the backoff instant.
    import unittest.mock as um

    async def _run() -> None:
        bridge._refresh_event.set()
        with um.patch.object(sb, "_RECONCILE_RETRY_BACKOFF_S", 0.0):
            with pytest.raises(asyncio.CancelledError):
                await bridge._refresh_loop()

    asyncio.run(_run())
    # First call failed and re-armed; second call ran (proving the
    # event was re-set) and cancelled out.
    assert calls["n"] == 2


def test_refresh_loop_source_pins_rearm_and_backoff() -> None:
    from bp_mcp_bridge import server_bridge

    src = inspect.getsource(server_bridge.ServerBridge._refresh_loop)
    # Must re-arm after a failure...
    assert "self._refresh_event.set()" in src
    # ...and back off first so a hard-down upstream doesn't hot-spin.
    assert "_RECONCILE_RETRY_BACKOFF_S" in src
    assert isinstance(server_bridge._RECONCILE_RETRY_BACKOFF_S, (int, float))
    assert 0.5 <= server_bridge._RECONCILE_RETRY_BACKOFF_S <= 60.0
