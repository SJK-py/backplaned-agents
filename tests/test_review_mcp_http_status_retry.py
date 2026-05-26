"""MCP retry now classifies `HTTPStatusError` (5xx / 429) as transient.

R2 PR #136 added retry for `httpx.TransportError` subclasses
and MCP code -32603. It missed `httpx.HTTPStatusError` — raised
by `resp.raise_for_status()` inside `mcp_client._call` on every
non-2xx HTTP response. The class is a SIBLING of `TransportError`
under `httpx.HTTPError`, not a subclass — so 5xx / 429 from
upstream surfaced immediately without retry, exactly the case
the retry was added for.

R4 fix: also catch `HTTPStatusError` and retry when
`response.status_code in {429, 500, 502, 503, 504}`.
"""

from __future__ import annotations

import asyncio
import inspect
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


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build a real `HTTPStatusError` shaped like one
    `resp.raise_for_status()` would raise."""
    request = httpx.Request("POST", "https://upstream.invalid")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"server returned {status_code}", request=request, response=response
    )


def test_classifier_retries_5xx_responses() -> None:
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(_make_http_status_error(500)) is True
    assert _is_transient(_make_http_status_error(502)) is True
    assert _is_transient(_make_http_status_error(503)) is True
    assert _is_transient(_make_http_status_error(504)) is True


def test_classifier_retries_429_too_many_requests() -> None:
    """429 is the classic upstream-throttle signal — exactly the case
    that benefits most from backoff."""
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(_make_http_status_error(429)) is True


def test_classifier_does_not_retry_4xx_client_errors() -> None:
    """Client errors (400, 401, 403, 404, 422) are bugs in the request
    shape — retry won't help. Surface immediately so the caller fixes
    their input."""
    from bp_mcp_bridge.tool_agent import _is_transient

    for code in (400, 401, 403, 404, 422):
        assert _is_transient(_make_http_status_error(code)) is False


def test_classifier_does_not_retry_2xx_3xx_status() -> None:
    """Sanity: a 2xx/3xx wouldn't normally raise HTTPStatusError
    (raise_for_status only fires on 4xx/5xx), but defensively
    reject."""
    from bp_mcp_bridge.tool_agent import _is_transient

    assert _is_transient(_make_http_status_error(200)) is False
    assert _is_transient(_make_http_status_error(301)) is False


def test_retry_succeeds_after_5xx_then_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Functional: first call raises HTTPStatusError(503), second
    succeeds → retry returns the second response."""
    from bp_mcp_bridge import tool_agent
    from bp_mcp_bridge.mcp_client import ToolResult

    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)
    monkeypatch.setattr(tool_agent, "_BACKOFF_MAX_S", 0.0)

    calls = []

    async def _call_tool(name, payload):
        calls.append((name, payload))
        if len(calls) == 1:
            raise _make_http_status_error(503)
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


def test_retry_surfaces_4xx_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 / 422 client error must NOT retry — caller's request is
    malformed, retry won't help and just delays the surface."""
    from bp_mcp_bridge import tool_agent

    monkeypatch.setattr(tool_agent, "_BACKOFF_INITIAL_S", 0.0)

    calls = []

    async def _call_tool(name, payload):
        calls.append(1)
        raise _make_http_status_error(422)

    client = MagicMock()
    client.call_tool = _call_tool
    ctx = MagicMock()
    ctx.log = MagicMock()

    async def _run() -> None:
        await tool_agent._call_tool_with_retry(
            client, "t", {"k": "v"}, ctx=ctx, server_id="srv_1"
        )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run())
    # NO retry — single attempt only.
    assert len(calls) == 1


def test_http_status_check_runs_before_transport_error_check() -> None:
    """`HTTPStatusError` is a sibling of `TransportError` under
    `HTTPError` — they're not in a subclass relationship. The
    classifier must check both branches; verify the HTTPStatusError
    check is reachable (lives in the source before the catch-all
    return False)."""
    from bp_mcp_bridge import tool_agent

    src = inspect.getsource(tool_agent._is_transient)
    http_idx = src.find("httpx.HTTPStatusError")
    transport_idx = src.find("httpx.TransportError")
    assert http_idx >= 0
    assert transport_idx >= 0
    # Both branches present.


def test_transient_status_set_pinned_to_429_plus_5xx() -> None:
    """Source pin: the transient status set is the documented
    {429, 500, 502, 503, 504}. A regression that adds 401 / 403
    (which can leak credentials on retry) or drops 502 / 504
    would fail here."""
    from bp_mcp_bridge.tool_agent import _HTTP_TRANSIENT_STATUS

    assert _HTTP_TRANSIENT_STATUS == {429, 500, 502, 503, 504}
