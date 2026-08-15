"""The bridge gives up, and stops fighting flaky HTTPS.

Two reported problems, one theme — the bridge retried forever and retried
badly:

  1. **Endless retry.** Three loops never stopped: the supervisor respawned a
     dead bridge every poll interval for the life of the process, the SSE
     stream task reconnected forever, and neither escalated its waits. A row
     with a typo'd URL meant a connect attempt against someone else's server
     every 30 seconds, and a stack trace in the log to match, indefinitely.
  2. **Flaky HTTPS with some providers.** A pooled connection the far end had
     already closed produced `RemoteProtocolError` mid-handshake — and the
     CONNECT path (`initialize` + `tools/list`) had no retry, so one dropped
     socket killed the whole bridge. Note what the fix is and is not: pool
     tuning does NOT solve this (httpx already expires idle sockets faster
     than any common LB), the retry does. Separately, a scalar `timeout=`
     gave the connect phase the read's budget, and the SSE client bounded
     nothing at all.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

# ===========================================================================
# HTTP client hardening
# ===========================================================================


def test_client_bounds_every_timeout_phase() -> None:
    """A scalar `timeout=` gives the CONNECT the same budget as the read, so
    a black-holed host used to hang the bridge for a minute an attempt."""
    from bp_mcp_bridge.mcp_client import build_http_client

    client = build_http_client(read_timeout_s=60.0)
    try:
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 60.0
        assert client.timeout.write is not None
        assert client.timeout.pool is not None
    finally:
        asyncio.run(client.aclose())


def test_sse_client_bounds_connect_but_not_read() -> None:
    """An SSE stream is idle for long stretches by design, so a read deadline
    would tear down a healthy stream — but the old `timeout=None` left the
    TLS handshake unbounded too, which could hang the stream task forever."""
    from bp_mcp_bridge.mcp_client import build_http_client

    client = build_http_client(read_timeout_s=None)
    try:
        assert client.timeout.read is None
        assert client.timeout.connect == 10.0
    finally:
        asyncio.run(client.aclose())


def test_pool_reuse_is_never_looser_than_the_httpx_default() -> None:
    """Pool tuning is hygiene here, NOT the fix for stale sockets — the retry
    layers are (see `mcp_client`'s header). What this pins is the direction:
    every extra second of reuse window is a second in which the far end may
    close the socket under us, so the explicit value must never be more
    permissive than the default it replaced."""
    import httpx as _httpx

    from bp_mcp_bridge import mcp_client

    assert 0 < mcp_client._KEEPALIVE_EXPIRY_S <= _httpx.Limits().keepalive_expiry


def test_the_real_stale_socket_fix_is_retry_on_both_paths() -> None:
    """A request that was already sent and then died on a dropped connection
    never reaches httpx's connect-retry — only an explicit retry recovers it.
    Both paths that talk to an upstream must have one."""
    from bp_mcp_bridge import server_bridge, tool_agent

    assert "_is_transient" in inspect.getsource(tool_agent._call_tool_with_retry)
    assert "is_transient_error" in inspect.getsource(
        server_bridge.ServerBridge._connect_with_retry
    )


def test_transport_retries_connections() -> None:
    """`AsyncHTTPTransport(retries=...)` retries connection ESTABLISHMENT
    only — never a request already sent, which would not be safe."""
    from bp_mcp_bridge import mcp_client

    assert mcp_client._TRANSPORT_CONNECT_RETRIES >= 1
    src = inspect.getsource(mcp_client.build_http_client)
    assert "AsyncHTTPTransport" in src and "retries=" in src


def test_both_transports_use_the_tuned_client() -> None:
    """A future transport that hand-rolls `httpx.AsyncClient()` gets the
    default pool behaviour back and the flakiness with it."""
    from bp_mcp_bridge import mcp_client

    src = inspect.getsource(mcp_client)
    body = src[src.index("class StreamableHttpMcpClient"):]
    assert "httpx.AsyncClient(" not in body, (
        "construct clients through build_http_client()"
    )


# ===========================================================================
# Transient classification, now shared with the connect path
# ===========================================================================


def test_stale_pooled_connection_is_transient() -> None:
    """`RemoteProtocolError: Server disconnected` is exactly what a dead
    keep-alive socket produces, and it must be retryable."""
    from bp_mcp_bridge.mcp_client import is_transient_error

    assert is_transient_error(
        httpx.RemoteProtocolError("Server disconnected without sending a response")
    )
    assert is_transient_error(httpx.ConnectError("connection refused"))
    assert is_transient_error(httpx.ReadTimeout("timed out"))


def test_permanent_errors_are_not_retried() -> None:
    from bp_mcp_bridge.mcp_client import McpError, is_transient_error

    request = httpx.Request("POST", "https://x/y")
    for status in (400, 401, 403, 404):
        resp = httpx.Response(status, request=request)
        assert not is_transient_error(
            httpx.HTTPStatusError("nope", request=request, response=resp)
        )
    # -32601 method not found: the same on every attempt.
    assert not is_transient_error(McpError(-32601, "method not found"))
    assert not is_transient_error(ValueError("unrelated"))


def test_tool_agent_delegates_to_the_shared_classifier() -> None:
    from bp_mcp_bridge import tool_agent
    from bp_mcp_bridge.mcp_client import http_transient_status, mcp_transient_codes

    assert "is_transient_error" in inspect.getsource(tool_agent._is_transient)
    # The names the retry loop's own tests import still resolve.
    assert tool_agent._MCP_TRANSIENT_CODES == mcp_transient_codes()
    assert tool_agent._HTTP_TRANSIENT_STATUS == http_transient_status()


# ===========================================================================
# Connect-path retry (was: one blip killed the whole bridge)
# ===========================================================================


def _bridge(tmp_path, client):  # noqa: ANN001, ANN202
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow.from_admin_dict({
        "server_id": "srv_1", "url": "https://x/mcp",
        "transport": "streamable_http", "auth_kind": "none",
        "groups": [], "capabilities": [], "expose_to_llm": True,
        "disabled_tools": [], "command": None, "args": [], "env_refs": {},
    })
    bridge = ServerBridge(
        row, admin_client=SimpleNamespace(), router_url="ws://r",  # type: ignore[arg-type]
        state_dir=tmp_path,
    )
    bridge._mcp_client = client
    return bridge


def test_connect_retries_a_transient_failure(tmp_path) -> None:
    """A hosted provider dropping one connection during `initialize()` used
    to cost a full bridge restart cycle."""
    attempts = {"n": 0}

    class _Flaky:
        async def initialize(self):  # noqa: ANN202
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.RemoteProtocolError("Server disconnected")
            return {}

        async def list_tools(self):  # noqa: ANN202
            return ["tool"]

    bridge = _bridge(tmp_path, _Flaky())
    out = asyncio.run(bridge._connect_with_retry())
    assert out == ["tool"]
    assert attempts["n"] == 3


def test_connect_does_not_retry_a_permanent_failure(tmp_path) -> None:
    """Bad credentials should reach the operator on the first attempt, not
    after three rounds of hammering someone else's 401."""
    attempts = {"n": 0}
    request = httpx.Request("POST", "https://x/mcp")

    class _Unauthorised:
        async def initialize(self):  # noqa: ANN202
            attempts["n"] += 1
            raise httpx.HTTPStatusError(
                "unauthorised", request=request,
                response=httpx.Response(401, request=request),
            )

        async def list_tools(self):  # noqa: ANN202  # pragma: no cover
            return []

    bridge = _bridge(tmp_path, _Unauthorised())
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(bridge._connect_with_retry())
    assert attempts["n"] == 1


def test_connect_gives_up_after_its_attempt_budget(tmp_path) -> None:
    """Short on purpose: past this the supervisor's backoff is the better
    place to wait, because it escalates and it eventually stops."""
    from bp_mcp_bridge import server_bridge

    attempts = {"n": 0}

    class _Down:
        async def initialize(self):  # noqa: ANN202
            attempts["n"] += 1
            raise httpx.ConnectError("refused")

        async def list_tools(self):  # noqa: ANN202  # pragma: no cover
            return []

    bridge = _bridge(tmp_path, _Down())
    with pytest.raises(httpx.ConnectError):
        asyncio.run(bridge._connect_with_retry())
    assert attempts["n"] == server_bridge._CONNECT_ATTEMPTS


# ===========================================================================
# SSE stream: bounded reconnects
# ===========================================================================


def test_sse_stream_gives_up_after_consecutive_failures() -> None:
    """It used to reconnect forever, inside a bridge that still reported
    itself healthy. Giving up ends the stream task, which ends the bridge,
    which hands the decision to the one loop that escalates and stops."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://x/sse")
    client._MAX_CONSECUTIVE_RECONNECTS = 3
    client._RECONNECT_BACKOFF_INITIAL_S = 0.001
    client._RECONNECT_BACKOFF_MAX_S = 0.001
    attempts = {"n": 0}

    class _AlwaysFails:
        def stream(self, *a, **kw):  # noqa: ANN001, ANN202
            attempts["n"] += 1
            raise httpx.ConnectError("refused")

    client._client = _AlwaysFails()  # type: ignore[assignment]
    asyncio.run(client._stream_loop())
    assert attempts["n"] == 3, "should stop at the ceiling, not loop forever"


def test_sse_reconnect_counter_resets_on_a_good_connect() -> None:
    """A stream that works for a while then blips gets the full allowance
    again — the ceiling is on CONSECUTIVE failures, not lifetime ones."""
    from bp_mcp_bridge import mcp_client

    src = inspect.getsource(mcp_client.SseMcpClient._stream_loop)
    reset = src.index("consecutive_failures = 0")
    bump = src.index("consecutive_failures += 1")
    assert reset < bump, "the reset must be on the success path"


# ===========================================================================
# The supervisor's health gate — backoff, give-up, and operator recovery
# ===========================================================================


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _gate(**over):  # noqa: ANN202
    from bp_mcp_bridge.health import HealthGate

    clock = _Clock()
    base = dict(
        backoff_base_s=10.0, backoff_max_s=40.0, max_failures=4,
        healthy_run_s=60.0, clock=clock,
    )
    base.update(over)
    return HealthGate(**base), clock


def _fail(gate, clock, key="mcp:srv", *, error="ConnectError: refused"):  # noqa: ANN001, ANN202
    gate.record_start(key)
    clock.advance(1.0)  # died fast — a failed start
    gate.record_exit(key, error=error)


def test_a_fresh_agent_may_start() -> None:
    gate, _ = _gate()
    assert gate.may_start("mcp:srv")


def test_failures_defer_the_next_start_with_growing_backoff() -> None:
    gate, clock = _gate()

    _fail(gate, clock)
    assert not gate.may_start("mcp:srv"), "must wait after a failure"
    clock.advance(10.0)
    assert gate.may_start("mcp:srv"), "10s backoff elapsed"

    _fail(gate, clock)
    clock.advance(10.0)
    assert not gate.may_start("mcp:srv"), "second failure waits longer"
    clock.advance(10.0)
    assert gate.may_start("mcp:srv")


def test_backoff_is_capped() -> None:
    gate, clock = _gate()
    for _ in range(3):
        _fail(gate, clock)
    clock.advance(40.0)
    assert gate.may_start("mcp:srv"), "must not exceed backoff_max_s"


def test_it_gives_up_after_the_failure_ceiling() -> None:
    """The reported bug: without this the loop never ends."""
    gate, clock = _gate()
    for _ in range(4):
        _fail(gate, clock)
    assert gate.status("mcp:srv")["given_up"] is True
    clock.advance(10_000.0)
    assert not gate.may_start("mcp:srv"), "no amount of waiting reopens it"


def test_the_last_error_is_kept_for_the_operator() -> None:
    gate, clock = _gate()
    _fail(gate, clock, error="ConnectError: name resolution failed")
    assert "name resolution" in gate.status("mcp:srv")["last_error"]


def test_a_clean_but_immediate_exit_still_counts_as_a_failure() -> None:
    """A bridge with neither credentials nor an invitation returns cleanly
    and instantly. Counting only exceptions would let that spin at the poll
    interval forever — which is the same bug in a quieter costume."""
    gate, clock = _gate()
    for _ in range(4):
        gate.record_start("mcp:srv")
        clock.advance(0.1)
        gate.record_exit("mcp:srv", error=None)
    assert gate.status("mcp:srv")["given_up"] is True


def test_a_healthy_run_resets_the_counter() -> None:
    """It connected and served for a while; whatever killed it later is a
    fresh problem, not evidence of a persistent one."""
    gate, clock = _gate()
    for _ in range(3):
        _fail(gate, clock)
    assert gate.status("mcp:srv")["failures"] == 3

    gate.record_start("mcp:srv")
    clock.advance(120.0)  # ran well past healthy_run_s
    gate.record_exit("mcp:srv", error="ConnectError: later blip")
    assert gate.status("mcp:srv")["failures"] == 0
    assert gate.may_start("mcp:srv")


def test_editing_the_row_reopens_a_given_up_agent() -> None:
    """Recovery path 1: the config signature changed, so the operator fixed
    something and wants another go. No process restart."""
    gate, clock = _gate()
    gate.observe("mcp:srv", signature=("old",), invitation=None)
    for _ in range(4):
        _fail(gate, clock)
    assert not gate.may_start("mcp:srv")

    gate.observe("mcp:srv", signature=("fixed-url",), invitation=None)
    assert gate.may_start("mcp:srv")
    assert gate.status("mcp:srv")["failures"] == 0


def test_clicking_reconnect_reopens_a_given_up_agent() -> None:
    """Recovery path 2: a fresh invitation token was minted onto the row,
    which is precisely what the admin UI's Reconnect button does."""
    gate, clock = _gate()
    gate.observe("mcp:srv", signature=("same",), invitation=None)
    for _ in range(4):
        _fail(gate, clock)
    assert not gate.may_start("mcp:srv")

    gate.observe("mcp:srv", signature=("same",), invitation="fresh-token")
    assert gate.may_start("mcp:srv")


def test_an_unchanged_row_does_not_reopen_the_gate() -> None:
    """`observe` runs on every poll; only a CHANGE may reset."""
    gate, clock = _gate()
    gate.observe("mcp:srv", signature=("same",), invitation="tok")
    for _ in range(4):
        _fail(gate, clock)
    for _ in range(5):
        gate.observe("mcp:srv", signature=("same",), invitation="tok")
    assert not gate.may_start("mcp:srv")


def test_forgetting_an_agent_clears_its_state() -> None:
    """A deleted-then-recreated id starts from a clean slate."""
    gate, clock = _gate()
    for _ in range(4):
        _fail(gate, clock)
    gate.forget("mcp:srv")
    assert gate.may_start("mcp:srv")
    assert gate.status("mcp:srv")["failures"] == 0


def test_keys_are_namespaced_per_kind() -> None:
    gate, clock = _gate()
    for _ in range(4):
        _fail(gate, clock, key="mcp:srv")
    _fail(gate, clock, key="code:code_x")
    assert gate.keys_for("mcp") == ["mcp:srv"]
    assert gate.keys_for("code") == ["code:code_x"]
    # Giving up on one must not touch the other.
    assert not gate.may_start("mcp:srv")
    assert gate.status("code:code_x")["given_up"] is False


# ===========================================================================
# Supervisor wiring — the gate is actually consulted
# ===========================================================================


def _supervisor(**over):  # noqa: ANN202
    from bp_mcp_bridge.supervisor import Supervisor

    base = dict(
        admin_client=SimpleNamespace(
            list_custom_agents=None, list_code_agents=None,
        ),
        router_url="ws://r/v1/agent",
        state_dir=Path("/tmp/state"),  # noqa: S108
    )
    base.update(over)
    return Supervisor(**base)  # type: ignore[arg-type]


def test_supervisor_gates_every_start() -> None:
    """All three spawn sites must ask. One that forgets reintroduces the
    endless respawn for its kind only — which is worse than not having the
    gate, because it looks fixed."""
    from bp_mcp_bridge.supervisor import Supervisor

    for fn in (Supervisor._start_kind, Supervisor._reconcile_once,
               Supervisor._reconcile_kind):
        src = inspect.getsource(fn)
        if fn is Supervisor._start_kind:
            assert "record_start" in src
        else:
            assert "may_start" in src, fn.__name__


def test_supervisor_records_exits_for_both_paths() -> None:
    from bp_mcp_bridge.supervisor import Supervisor

    for fn in (Supervisor._on_bridge_done, Supervisor._on_agent_bridge_done):
        assert "record_exit" in inspect.getsource(fn), fn.__name__


def test_a_given_up_row_is_not_respawned_by_the_reconcile() -> None:
    """End to end through the real reconcile: a code agent whose bridge dies
    instantly is retried, deferred, then abandoned — rather than started
    afresh on every single poll."""
    from bp_mcp_bridge.health import HealthGate

    clock = _Clock()
    sup = _supervisor(
        health=HealthGate(
            backoff_base_s=10.0, backoff_max_s=10.0, max_failures=3,
            healthy_run_s=60.0, clock=clock,
        ),
    )
    starts = {"n": 0}

    row = {
        "agent_id": "code_x", "description": "", "code": "def run(p,c): return 1\n",
        "entrypoint": "run", "parameters": [], "returns": None,
        "secret_refs": {}, "timeout_s": 30, "memory_mb": 512,
        "groups": [], "capabilities": [], "expose_to_llm": True,
        "output_as_file": False, "enabled": True,
    }

    async def _list_code_agents():  # noqa: ANN202
        return [row]

    sup._admin_client.list_code_agents = _list_code_agents  # type: ignore[attr-defined]

    real_start = sup._start_kind

    def _counting_start(kind, r):  # noqa: ANN001, ANN202
        starts["n"] += 1
        # Simulate a bridge that dies immediately, the way a broken row does.
        sup._health.record_start(f"{kind.name}:{r.agent_id}")
        clock.advance(0.1)
        sup._health.record_exit(
            f"{kind.name}:{r.agent_id}", error="ConnectError: refused"
        )

    sup._start_kind = _counting_start  # type: ignore[assignment]

    async def _poll(times: int) -> None:
        for _ in range(times):
            for kind in sup._kinds():
                if kind.name == "code":
                    await sup._reconcile_kind(kind)
            clock.advance(10.0)  # one poll interval, and the backoff window

    asyncio.run(_poll(10))
    assert real_start is not None
    # 3 attempts, then the gate is closed for good — NOT 10.
    assert starts["n"] == 3, f"expected 3 attempts before giving up, got {starts['n']}"


# ===========================================================================
# The fourth loop: in-bridge tool refresh
# ===========================================================================
#
# `HealthGate` cannot see this one. It runs inside a bridge that is otherwise
# healthy and never exits, so no task ever completes for the supervisor to
# account for. Before the fix it re-armed itself on a FIXED 5s, forever — a
# slow hot spin against an upstream that had already said no.

# Captured before any monkeypatching. `server_bridge` calls `asyncio.sleep`
# through the module object, so patching it patches the real one for everyone
# — including this file. A stub that does not yield would deadlock the event
# loop, so every stub below yields through this reference instead.
_real_sleep = asyncio.sleep


def _refresh_bridge(tmp_path: Path):  # noqa: ANN202
    from bp_mcp_bridge import server_bridge as sb

    row = sb.ServerBridgeRow(
        server_id="srv1", url="https://x/", transport="streamable_http",
        auth_kind="none", auth_value_ref=None, auth_header_name=None,
        groups=["mcp_bridge"], expose_to_llm=True, refresh_requested_at=None,
        pending_invitation_token="inv-tok",
    )
    return sb.ServerBridge(
        row, admin_client=SimpleNamespace(), router_url="ws://r/",
        state_dir=tmp_path,
    )


def _drive_refresh_loop(  # noqa: ANN202
    bridge, monkeypatch, *, fail_times: int, budget: int
):
    """Run `_refresh_loop` against a reconcile that fails `fail_times` times.

    Returns `(attempts, waits)`. `budget` bounds the run by cancelling out of
    the reconcile, so a loop that never gives up fails the assertion instead
    of hanging the suite.
    """
    from bp_mcp_bridge import server_bridge as sb

    waits: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        waits.append(delay)
        await _real_sleep(0)

    monkeypatch.setattr(sb.asyncio, "sleep", _fake_sleep)
    calls = {"n": 0}

    async def _reconcile() -> None:
        calls["n"] += 1
        if calls["n"] > budget:
            raise asyncio.CancelledError
        if calls["n"] <= fail_times:
            raise RuntimeError("transient list_tools failure")

    bridge._reconcile_tools = _reconcile  # type: ignore[method-assign]

    async def _run() -> None:
        bridge._refresh_event.set()
        try:
            # Parking is the SUCCESS condition here: once the loop gives up
            # it blocks on `_refresh_event.wait()` and never returns on its
            # own. `wait_for` is what ends the test — it schedules on the
            # loop's timer rather than through the patched `asyncio.sleep`,
            # so the stub above cannot swallow it.
            await asyncio.wait_for(bridge._refresh_loop(), 2.0)
        except (asyncio.CancelledError, TimeoutError):
            pass

    asyncio.run(_run())
    return calls["n"], waits


def test_refresh_loop_stops_re_arming_after_the_ceiling(tmp_path, monkeypatch) -> None:
    """A permanently broken upstream must not be re-asked forever. Past the
    ceiling the loop parks on `wait()` instead of re-setting its own event."""
    from bp_mcp_bridge import server_bridge as sb

    attempts, _ = _drive_refresh_loop(
        _refresh_bridge(tmp_path), monkeypatch, fail_times=10_000, budget=200
    )
    assert attempts == sb._RECONCILE_MAX_CONSECUTIVE_FAILURES, (
        f"expected the loop to park after "
        f"{sb._RECONCILE_MAX_CONSECUTIVE_FAILURES} failures, got {attempts}"
    )


def test_refresh_backoff_escalates_to_a_ceiling(tmp_path, monkeypatch) -> None:
    """A fixed interval is a slow hot spin. The waits must grow, and stop
    growing somewhere bounded."""
    from bp_mcp_bridge import server_bridge as sb

    _, waits = _drive_refresh_loop(
        _refresh_bridge(tmp_path), monkeypatch, fail_times=10_000, budget=200
    )
    assert waits, "a failed reconcile must back off before re-arming"
    assert waits == sorted(waits), f"waits must not shrink: {waits}"
    assert len(set(waits)) > 1, "one repeated value is the bug being fixed"
    assert max(waits) <= sb._RECONCILE_RETRY_BACKOFF_MAX_S


def test_a_successful_refresh_resets_the_failure_count(tmp_path, monkeypatch) -> None:
    """Giving up must mean *consecutive* failures. A run that fails, then
    succeeds, must not carry the old count into the next bad patch."""
    from bp_mcp_bridge import server_bridge as sb

    ceiling = sb._RECONCILE_MAX_CONSECUTIVE_FAILURES
    attempts, _ = _drive_refresh_loop(
        _refresh_bridge(tmp_path), monkeypatch, fail_times=ceiling - 1, budget=500
    )
    # ceiling-1 failures, then a success — which neither gives up nor
    # re-arms, so the loop parks on wait() having spent one more attempt.
    assert attempts == ceiling


def test_giving_up_on_refresh_is_recoverable_by_a_new_signal(
    tmp_path, monkeypatch
) -> None:
    """Parking is not death: the loop returns to `wait()`, so an admin
    'Refresh tools' click or an SSE `tools/list_changed` starts it over."""
    from bp_mcp_bridge import server_bridge as sb

    src = inspect.getsource(sb.ServerBridge._refresh_loop)
    # The give-up branch must `continue` back to the wait. A `return` or
    # `break` would leave the bridge permanently deaf to refresh signals.
    assert "continue" in src
    after_giveup = src.split("gave_up")[-1]
    assert "return" not in after_giveup.split("continue")[0]

    async def _fake_sleep(_delay: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr(sb.asyncio, "sleep", _fake_sleep)
    bridge = _refresh_bridge(tmp_path)
    calls = {"n": 0}

    async def _reconcile() -> None:
        calls["n"] += 1
        raise RuntimeError("down")

    bridge._reconcile_tools = _reconcile  # type: ignore[method-assign]

    async def _run() -> None:
        bridge._refresh_event.set()
        task = asyncio.create_task(bridge._refresh_loop())
        for _ in range(500):  # let it burn its budget and park
            await _real_sleep(0)
        parked = calls["n"]
        assert parked == sb._RECONCILE_MAX_CONSECUTIVE_FAILURES, (
            f"expected it to park at the ceiling, got {parked}"
        )
        bridge._refresh_event.set()  # a genuine new signal
        for _ in range(500):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert calls["n"] > parked, "a new signal must restart the retries"

    asyncio.run(_run())
