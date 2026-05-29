"""R8 HIGH: MCP bridge Prometheus metrics.

The bridge ran as a separate process with ZERO metrics — operators
had only stderr logs to reason about tool-call health, SSE
stability, reconcile churn, and bridge lifecycle. This adds a
bridge-local metric registry + a `/metrics` HTTP endpoint, wired
into the existing hot paths.

Design invariants pinned here:

  - **Never load-bearing.** When `prometheus_client` is absent the
    handles degrade to no-op stubs and call sites stay
    unconditional; the bridge must not crash.
  - **Bridge-local registry.** A dedicated `CollectorRegistry`
    distinct from the router's.
  - **Bounded labels.** `server_id` / `tool` are operator-defined
    and finite — safe as labels (unlike caller-supplied agent_id).
  - **Wiring.** tool calls (success/tool_error/failed + latency +
    retries), SSE reconnects, reconcile changes, bridge lifecycle,
    invitations, active-bridge gauge.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module shape / no-op resilience
# ---------------------------------------------------------------------------


def test_metrics_module_imports_without_crash() -> None:
    from bp_mcp_bridge import metrics

    # All the documented handles exist.
    for name in (
        "tool_calls_total",
        "tool_call_retries_total",
        "tool_call_duration_seconds",
        "sse_reconnects_total",
        "sse_pending_stranded_total",
        "bridge_starts_total",
        "bridge_exits_total",
        "active_bridges",
        "tool_reconcile_changes_total",
        "aclose_timeouts_total",
        "invitations_issued_total",
    ):
        assert hasattr(metrics, name), name


def test_noop_metric_supports_full_surface() -> None:
    """The no-op stub must support the exact handle surface the
    call sites use so a prometheus-less bridge never AttributeErrors."""
    from bp_mcp_bridge.metrics import _NoopMetric

    m = _NoopMetric()
    # Chained labels().inc()/observe()/set() must all be no-ops.
    m.labels(a="b").inc()
    m.labels(a="b").inc(5)
    m.labels(x="y").observe(0.3)
    m.set(2)
    m.labels().inc()  # zero-arg labels too


def test_start_metrics_server_disabled_on_nonpositive_port() -> None:
    from bp_mcp_bridge import metrics

    assert metrics.start_metrics_server(0) is False
    assert metrics.start_metrics_server(-1) is False


def test_start_metrics_server_binds_when_prometheus_available() -> None:
    """When prometheus is installed (it is in CI), the server starts
    on an ephemeral port and the registry is scrapeable."""
    pytest.importorskip("prometheus_client")
    import socket

    from bp_mcp_bridge import metrics

    # Grab a free port.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    started = metrics.start_metrics_server(port)
    assert started is True

    # Scrape it.
    import urllib.request

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/metrics", timeout=5
    ) as resp:
        body = resp.read().decode()
    # Our metric names are present in the exposition.
    assert "bp_mcp_bridge_tool_calls_total" in body


# ---------------------------------------------------------------------------
# Tool-call wiring
# ---------------------------------------------------------------------------


def _stub_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.config import BridgeConfig

    return BridgeConfig(
        server_id="srv1",
        url="https://x/",
        transport="streamable_http",
        auth_kind="none",
        auth_value=None,
        auth_header_name=None,
        groups=["mcp_bridge"],
        expose_to_llm=True,
        state_dir=tmp_path,
    )


def _stub_tool(name: str = "read_file"):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.mcp_client import ToolDefinition

    return ToolDefinition(
        name=name, description="t",
        input_schema={"type": "object"},
    )


def test_tool_call_success_increments_success_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient, ToolResult
    from bp_mcp_bridge.tool_agent import build_server_agent

    calls: list[tuple[str, dict, str]] = []

    class _Rec:
        def __init__(self, label: str) -> None:
            self.label = label

        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            calls.append((self.label, self._kw, "inc"))

        def observe(self, amount: float) -> None:
            calls.append((self.label, self._kw, "observe"))

    monkeypatch.setattr(m, "tool_calls_total", _Rec("tool_calls_total"))
    monkeypatch.setattr(
        m, "tool_call_duration_seconds", _Rec("tool_call_duration_seconds")
    )
    monkeypatch.setattr(
        m, "tool_call_retries_total", _Rec("tool_call_retries_total")
    )

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")
    agent = build_server_agent(config, mcp, [tool], "tok")
    handler = agent._handlers_by_mode[tool.name]

    async def fake_call_tool(name: str, payload: dict) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": "ok"}], is_error=False)

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    class _Ctx:
        log = MagicMock()

    asyncio.run(handler.fn(_Ctx(), {"path": "/x"}))

    outcomes = [
        kw.get("outcome")
        for label, kw, op in calls
        if label == "tool_calls_total" and op == "inc"
    ]
    assert outcomes == ["success"]
    # Latency observed exactly once.
    assert sum(
        1 for label, _, op in calls
        if label == "tool_call_duration_seconds" and op == "observe"
    ) == 1


def test_tool_call_is_error_increments_tool_error_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient, ToolResult
    from bp_mcp_bridge.tool_agent import build_server_agent

    seen: list[dict] = []

    class _Rec:
        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            seen.append(self._kw)

        def observe(self, amount: float) -> None:
            pass

    monkeypatch.setattr(m, "tool_calls_total", _Rec())
    monkeypatch.setattr(m, "tool_call_duration_seconds", _Rec())
    monkeypatch.setattr(m, "tool_call_retries_total", _Rec())

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")
    agent = build_server_agent(config, mcp, [tool], "tok")
    handler = agent._handlers_by_mode[tool.name]

    async def fake_call_tool(name: str, payload: dict) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": "nope"}], is_error=True)

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    class _Ctx:
        log = MagicMock()

    asyncio.run(handler.fn(_Ctx(), {"path": "/x"}))

    assert any(kw.get("outcome") == "tool_error" for kw in seen)


def test_tool_call_exception_increments_failed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge.mcp_client import McpError, StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    seen: list[dict] = []

    class _Rec:
        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            seen.append(self._kw)

        def observe(self, amount: float) -> None:
            pass

    monkeypatch.setattr(m, "tool_calls_total", _Rec())
    monkeypatch.setattr(m, "tool_call_duration_seconds", _Rec())
    monkeypatch.setattr(m, "tool_call_retries_total", _Rec())

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")
    agent = build_server_agent(config, mcp, [tool], "tok")
    handler = agent._handlers_by_mode[tool.name]

    async def fake_call_tool(name: str, payload: dict):  # type: ignore[no-untyped-def]
        # -32601 is permanent (not in transient set) → raised immediately.
        raise McpError(-32601, "method not found")

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    class _Ctx:
        log = MagicMock()

    with pytest.raises(McpError):
        asyncio.run(handler.fn(_Ctx(), {"path": "/x"}))

    assert any(kw.get("outcome") == "failed" for kw in seen)


# ---------------------------------------------------------------------------
# SSE reconnect wiring
# ---------------------------------------------------------------------------


def test_sse_client_carries_server_id_for_metrics() -> None:
    from bp_mcp_bridge.mcp_client import SseMcpClient, build_mcp_client

    c = build_mcp_client("sse", "https://x/", server_id="srv-xyz")
    assert isinstance(c, SseMcpClient)
    assert c._server_id == "srv-xyz"

    # Default when not supplied.
    c2 = SseMcpClient("https://x/")
    assert c2._server_id == "unknown"


def test_sse_reconnect_increments_counter_with_server_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive `_stream_loop` through exactly one failed-connect →
    reconnect cycle and assert the counter fired with the server
    label. The loop is stopped by flipping `_closed` after the
    first backoff sleep."""
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge.mcp_client import SseMcpClient

    seen: list[dict] = []

    class _Rec:
        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            seen.append(self._kw)

    monkeypatch.setattr(m, "sse_reconnects_total", _Rec())

    client = SseMcpClient("https://x/", server_id="srv-7")

    # `_client.stream` raises so we hit the except → reconnect path.
    class _BoomStream:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("connect failed")

        async def __aexit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(
        client._client, "stream", lambda *a, **k: _BoomStream()
    )

    real_sleep = asyncio.sleep

    async def _sleep(_s: float) -> None:
        # After the first reconnect backoff, stop the loop.
        client._closed = True
        await real_sleep(0)

    monkeypatch.setattr("bp_mcp_bridge.mcp_client.asyncio.sleep", _sleep)

    asyncio.run(client._stream_loop())

    assert seen == [{"server_id": "srv-7"}]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_supervisor_config_metrics_port_default_and_override() -> None:
    from bp_mcp_bridge.config import SupervisorConfig

    base_env = {
        "BP_MCP_BRIDGE_ADMIN_TOKEN": "t",
    }
    cfg = SupervisorConfig.from_env(base_env)
    assert cfg.metrics_port == 9464

    cfg2 = SupervisorConfig.from_env(
        {**base_env, "BP_MCP_BRIDGE_METRICS_PORT": "0"}
    )
    assert cfg2.metrics_port == 0

    cfg3 = SupervisorConfig.from_env(
        {**base_env, "BP_MCP_BRIDGE_METRICS_PORT": "9999"}
    )
    assert cfg3.metrics_port == 9999


def test_supervisor_config_metrics_port_invalid_raises() -> None:
    from bp_mcp_bridge.config import SupervisorConfig

    with pytest.raises(RuntimeError) as exc:
        SupervisorConfig.from_env({
            "BP_MCP_BRIDGE_ADMIN_TOKEN": "t",
            "BP_MCP_BRIDGE_METRICS_PORT": "not-a-number",
        })
    assert "BP_MCP_BRIDGE_METRICS_PORT" in str(exc.value)


# ---------------------------------------------------------------------------
# Source pins for the remaining wirings
# ---------------------------------------------------------------------------


def test_server_bridge_wires_lifecycle_and_reconcile_metrics() -> None:
    from bp_mcp_bridge import server_bridge

    run_src = inspect.getsource(server_bridge.ServerBridge.run)
    assert "bridge_starts_total" in run_src
    assert "bridge_exits_total" in run_src
    # Exit reason is classified, not hardcoded.
    assert 'exit_reason = "cancelled"' in run_src
    assert 'exit_reason = "error"' in run_src

    # `_apply_tools` carries the per-change metric increments;
    # `_reconcile_tools` calls into it after the upstream tools/list.
    apply_src = inspect.getsource(server_bridge.ServerBridge._apply_tools)
    assert "tool_reconcile_changes_total" in apply_src

    inv_src = inspect.getsource(
        server_bridge.ServerBridge._issue_invitation_if_needed
    )
    assert "invitations_issued_total" in inv_src


def test_main_starts_metrics_server() -> None:
    from bp_mcp_bridge import __main__ as m

    src = inspect.getsource(m._main)
    assert "metrics.start_metrics_server(config.metrics_port)" in src


def test_supervisor_publishes_active_bridges_gauge() -> None:
    from bp_mcp_bridge import supervisor

    src = inspect.getsource(supervisor.Supervisor._reconcile_once)
    assert "metrics.active_bridges.set(len(self._active))" in src
    # MUST be direct .set(), not .labels().set() — real prometheus
    # rejects .labels() on an unlabelled metric.
    assert "active_bridges.labels()" not in src


# ---------------------------------------------------------------------------
# Cross-PR integration: metrics that only became wireable once the
# SSE-reconnect (#189) and aclose-timeout (#187) fixes merged. The
# metrics PR declared these handles up front; this pins that the
# rebase actually connected them rather than leaving dead handles.
# ---------------------------------------------------------------------------


def test_sse_stranded_metric_fires_with_count_on_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `_fail_pending_for_reconnect` strands N in-flight
    requests, `sse_pending_stranded_total` increments by exactly N
    (and the reconnect counter by 1) — in the same loop turn,
    captured BEFORE `_pending` is cleared."""
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge.mcp_client import SseMcpClient

    reconnects: list[dict] = []
    stranded: list[tuple[dict, float]] = []

    class _Rec:
        def __init__(self, sink: list, count: bool = False) -> None:
            self._sink = sink
            self._count = count

        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            if self._count:
                self._sink.append((self._kw, amount))
            else:
                self._sink.append(self._kw)

    monkeypatch.setattr(m, "sse_reconnects_total", _Rec(reconnects))
    monkeypatch.setattr(
        m, "sse_pending_stranded_total", _Rec(stranded, count=True)
    )

    client = SseMcpClient("https://x/", server_id="srv-9")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Stage two in-flight requests.
        client._pending[1] = loop.create_future()
        client._pending[2] = loop.create_future()

        class _BoomStream:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("connect failed")

            async def __aexit__(self, *a: Any) -> None:
                return None

        monkeypatch.setattr(
            client._client, "stream", lambda *a, **k: _BoomStream()
        )

        real_sleep = asyncio.sleep

        async def _sleep(_s: float) -> None:
            client._closed = True
            await real_sleep(0)

        monkeypatch.setattr(
            "bp_mcp_bridge.mcp_client.asyncio.sleep", _sleep
        )

        loop.run_until_complete(client._stream_loop())
    finally:
        loop.close()

    assert reconnects == [{"server_id": "srv-9"}]
    # Stranded count == number of in-flight requests, server-labelled.
    assert stranded == [({"server_id": "srv-9"}, 2)]


def test_sse_stranded_metric_not_fired_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle-stream reconnect (no in-flight requests) bumps the
    reconnect counter but NOT the stranded counter — the common
    steady-state case must not emit a zero-count increment."""
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge.mcp_client import SseMcpClient

    reconnects: list[dict] = []
    stranded: list[Any] = []

    class _Rec:
        def __init__(self, sink: list) -> None:
            self._sink = sink

        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            self._sink.append(self._kw)

    monkeypatch.setattr(m, "sse_reconnects_total", _Rec(reconnects))
    monkeypatch.setattr(m, "sse_pending_stranded_total", _Rec(stranded))

    client = SseMcpClient("https://x/", server_id="srv-idle")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # No _pending entries — idle stream.
        class _BoomStream:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("blip")

            async def __aexit__(self, *a: Any) -> None:
                return None

        monkeypatch.setattr(
            client._client, "stream", lambda *a, **k: _BoomStream()
        )

        real_sleep = asyncio.sleep

        async def _sleep(_s: float) -> None:
            client._closed = True
            await real_sleep(0)

        monkeypatch.setattr(
            "bp_mcp_bridge.mcp_client.asyncio.sleep", _sleep
        )

        loop.run_until_complete(client._stream_loop())
    finally:
        loop.close()

    assert reconnects == [{"server_id": "srv-idle"}]
    assert stranded == []  # no stranded increment when idle


def test_aclose_timeout_increments_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded-aclose timeout branch (from #187) now increments
    `aclose_timeouts_total` — pin the integration so a refactor of
    `_close_mcp_client_bounded` can't silently drop it."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge import server_bridge as sb

    seen: list[dict] = []

    class _Rec:
        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            seen.append(self._kw)

    monkeypatch.setattr(m, "aclose_timeouts_total", _Rec())
    # Drop the timeout so the test is fast.
    monkeypatch.setattr(sb, "_ACLOSE_TIMEOUT_S", 0.05)

    row = sb.ServerBridgeRow(
        server_id="srv-stuck",
        url="https://x/",
        transport="streamable_http",
        auth_kind="none",
        auth_value_ref=None,
        auth_header_name=None,
        groups=["mcp_bridge"],
        expose_to_llm=True,
        refresh_requested_at=None,
    )
    bridge = sb.ServerBridge(
        row, admin_client=MagicMock(), router_url="ws://r/",
        state_dir=tmp_path,
    )

    class _StuckClient:
        async def aclose(self) -> None:
            await asyncio.Event().wait()

    bridge._mcp_client = _StuckClient()  # type: ignore[assignment]

    asyncio.run(bridge._close_mcp_client_bounded())

    assert seen == [{"server_id": "srv-stuck"}]


def test_aclose_timeout_metric_not_fired_on_clean_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal aclose must NOT bump the timeout counter."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge import metrics as m
    from bp_mcp_bridge import server_bridge as sb

    seen: list[dict] = []

    class _Rec:
        def labels(self, **kw: str) -> _Rec:
            self._kw = kw
            return self

        def inc(self, amount: float = 1.0) -> None:
            seen.append(self._kw)

    monkeypatch.setattr(m, "aclose_timeouts_total", _Rec())

    row = sb.ServerBridgeRow(
        server_id="srv-ok",
        url="https://x/",
        transport="streamable_http",
        auth_kind="none",
        auth_value_ref=None,
        auth_header_name=None,
        groups=["mcp_bridge"],
        expose_to_llm=True,
        refresh_requested_at=None,
    )
    bridge = sb.ServerBridge(
        row, admin_client=MagicMock(), router_url="ws://r/",
        state_dir=tmp_path,
    )

    class _NormalClient:
        async def aclose(self) -> None:
            return None

    bridge._mcp_client = _NormalClient()  # type: ignore[assignment]

    asyncio.run(bridge._close_mcp_client_bounded())

    assert seen == []
