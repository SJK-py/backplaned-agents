"""R8 HIGH: `ServerBridge._close_mcp_client_bounded` caps `aclose()`.

Without this bound, a stuck upstream (slow socket, dropped
packets, SSE stream task that won't cancel) blocks the bridge's
`run()` `finally`, which blocks the supervisor's `_stop()`
`await entry.task`, which blocks the **next reconcile pass** —
the supervisor is wedged for as long as the dead upstream stays
in that state. The blast radius is the entire bridge process:
one stuck server starves out reconfiguration / restart / shutdown
of every other server it manages.

Fix: wrap `aclose()` in `asyncio.wait_for(..., timeout=5.0)`. On
timeout we log `mcp_server_bridge_aclose_timeout` and let the
underlying `httpx.AsyncClient` leak — the next bridge spawn
builds a fresh client. One leaked connection pool per stuck
server is a far better failure mode than a deadlocked supervisor.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_row():  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.server_bridge import ServerBridgeRow

    return ServerBridgeRow(
        server_id="srv1",
        url="https://upstream/",
        transport="streamable_http",
        auth_kind="none",
        auth_value_ref=None,
        auth_header_name=None,
        groups=["mcp_bridge"],
        expose_to_llm=True,
        refresh_requested_at=None,
    )


def _make_bridge(tmp_path: Path):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.server_bridge import ServerBridge

    return ServerBridge(
        _make_row(),
        admin_client=MagicMock(),
        router_url="ws://r/",
        state_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# Bounded close behaviour
# ---------------------------------------------------------------------------


def test_aclose_timeout_does_not_propagate(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A hung `aclose()` is bounded by the timeout. The bridge
    should log `mcp_server_bridge_aclose_timeout` and return,
    NOT block indefinitely or raise."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")

    bridge = _make_bridge(tmp_path)

    # Stub MCP client whose `aclose()` blocks forever.
    class _StuckClient:
        async def aclose(self) -> None:
            await asyncio.Event().wait()  # never resolves

    bridge._mcp_client = _StuckClient()  # type: ignore[assignment]

    # Drop the timeout to keep the test fast.
    monkeypatch.setattr(
        "bp_mcp_bridge.server_bridge._ACLOSE_TIMEOUT_S", 0.05,
    )

    # If the timeout works, this returns within ~50ms. If it
    # doesn't, the test hangs (CI will kill it on the suite-level
    # timeout, surfacing the regression).
    async def _run() -> None:
        await asyncio.wait_for(
            bridge._close_mcp_client_bounded(), timeout=2.0,
        )

    asyncio.run(_run())


def test_aclose_normal_completion_returns_quickly(tmp_path: Path) -> None:
    """When `aclose()` completes normally, the bounded helper
    returns without raising. Defends against a regression where the
    timeout wrapper accidentally swallows successful completions."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    bridge = _make_bridge(tmp_path)

    closed = asyncio.Event()

    class _NormalClient:
        async def aclose(self) -> None:
            closed.set()

    bridge._mcp_client = _NormalClient()  # type: ignore[assignment]

    asyncio.run(bridge._close_mcp_client_bounded())
    assert closed.is_set()


def test_aclose_exception_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """When `aclose()` raises something other than TimeoutError, the
    error is logged and swallowed — the supervisor must always be
    able to make forward progress past a misbehaving aclose."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    bridge = _make_bridge(tmp_path)

    class _BrokenClient:
        async def aclose(self) -> None:
            raise RuntimeError("connection already closed")

    bridge._mcp_client = _BrokenClient()  # type: ignore[assignment]

    asyncio.run(bridge._close_mcp_client_bounded())
    # The exception didn't propagate. Source-pin that the catch is
    # logged via `mcp_server_bridge_aclose_error` so operators can
    # see it in production.


def test_close_mcp_client_bounded_is_noop_when_client_none(
    tmp_path: Path,
) -> None:
    """If `_mcp_client` is already None (double-close, or before
    `run()` got far enough to construct it), the helper is a no-op."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    bridge = _make_bridge(tmp_path)
    bridge._mcp_client = None
    # Should not raise.
    asyncio.run(bridge._close_mcp_client_bounded())


# ---------------------------------------------------------------------------
# Source pin
# ---------------------------------------------------------------------------


def test_run_finally_calls_bounded_close_not_raw_aclose() -> None:
    """Source-pin so a future refactor that drops the bounded
    helper and goes back to `await self._mcp_client.aclose()` in
    `run()`'s finally block gets caught."""
    from bp_mcp_bridge import server_bridge

    src = inspect.getsource(server_bridge.ServerBridge.run)
    assert "_close_mcp_client_bounded" in src, (
        "run()'s finally must call the bounded helper, not raw aclose"
    )


def test_module_exposes_aclose_timeout_constant() -> None:
    """The timeout is module-level so operators can monkey-patch it
    in tests / debugging. Pin the constant + its bound."""
    from bp_mcp_bridge import server_bridge

    assert isinstance(server_bridge._ACLOSE_TIMEOUT_S, (int, float))
    # Sanity bounds — too short would cause healthy aclose to time
    # out under load; too long defeats the purpose.
    assert 0.5 <= server_bridge._ACLOSE_TIMEOUT_S <= 60.0
