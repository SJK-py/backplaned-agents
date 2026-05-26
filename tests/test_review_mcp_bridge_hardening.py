"""MCP bridge hardening — 5 pre-ship findings.

1. CRITICAL: SSE `endpoint` event lets an attacker-controlled
   payload point the POST URL at a different origin, leaking the
   bearer credential.
2. HIGH: `_pending[rid]` Future leaks when the SSE response
   never arrives (`asyncio.wait_for` timeout path).
3. MEDIUM: bare `data:` events (no `event:` line) are dropped
   instead of treated as the default `"message"` type per the
   WHATWG SSE spec.
4. HIGH: stream loop runs exactly once; any transient drop
   permanently breaks the bridge until config changes or process
   restart.
5. MEDIUM: `Supervisor._on_bridge_done` doesn't evict the dead
   entry from `_active` — a bridge that errors out mid-`run()`
   leaves its slot occupied and never respawns.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

# ===========================================================================
# 1. SSE endpoint URL hijack
# ===========================================================================


def test_endpoint_event_uses_origin_check() -> None:
    """Source pin: `_handle_event` for `endpoint` events compares
    the resolved URL's origin (scheme + host + port) against the
    base SSE URL's. A regression that strips the check
    (`urljoin` alone) re-opens the bearer-credential leak."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._handle_event)
    assert "urlparse" in src, (
        "Endpoint resolver must parse both URLs to compare origins."
    )
    assert "scheme" in src and "hostname" in src, (
        "Origin check must compare scheme + hostname."
    )
    assert "mcp_sse_endpoint_origin_mismatch" in src, (
        "Mismatch path must log a clear event for the operator."
    )


def test_endpoint_event_rejects_attacker_url() -> None:
    """Functional: an SSE event with absolute attacker URL leaves
    `_post_url` unset. Any subsequent _call() would raise the
    'endpoint URL not yet received' McpError — surfacing a clear
    config / hostile-server signal instead of silently leaking."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.example/sse")

    async def drive() -> None:
        await client._handle_event(
            "endpoint", "https://attacker.example/post"
        )

    asyncio.run(drive())
    assert client._post_url is None


def test_endpoint_event_accepts_same_origin_path() -> None:
    """Companion sanity: relative path resolves and gets accepted."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.example/sse")

    async def drive() -> None:
        await client._handle_event("endpoint", "/post")

    asyncio.run(drive())
    assert client._post_url == "https://server.example/post"


def test_endpoint_event_rejects_scheme_downgrade() -> None:
    """Even with same hostname, `https → http` is a TLS downgrade
    that strips bearer protection. Refuse."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    client = SseMcpClient("https://server.example/sse")

    async def drive() -> None:
        await client._handle_event(
            "endpoint", "http://server.example/post"
        )

    asyncio.run(drive())
    assert client._post_url is None


# ===========================================================================
# 2. _pending leak on response timeout
# ===========================================================================


def test_call_cleans_up_pending_on_timeout() -> None:
    """Source pin: `_call` wraps the `wait_for(fut, ...)` await in
    try/finally so the `_pending.pop(rid, None)` runs on EVERY
    exit path — including the TimeoutError path that previously
    left the Future in `_pending` permanently."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._call)
    # try/finally rather than try/except. There must be a finally
    # clause AND a `self._pending.pop(rid, None)` inside it.
    assert "finally:" in src, (
        "_call must use try/finally so cleanup runs on timeout, "
        "not just on POST exceptions."
    )
    lines = src.splitlines()
    finally_idx = next(
        (i for i, line in enumerate(lines)
         if line.strip() == "finally:"),
        -1,
    )
    assert finally_idx >= 0
    body_after_finally = "\n".join(lines[finally_idx:])
    assert "self._pending.pop(rid, None)" in body_after_finally, (
        "The finally block must include the cleanup pop."
    )


def test_call_cleans_up_pending_on_timeout_functional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Functional pin: drive `_call` such that the inline POST
    returns 202 (which the existing code treats as 'response will
    come via stream') and the wait_for times out. Assert `_pending`
    is empty after the call raises."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    async def drive() -> None:
        client = SseMcpClient("https://x/")
        client._post_url = "https://x/post"
        # Force a very short timeout for the wait_for path.
        monkeypatch.setattr(
            SseMcpClient, "_RESPONSE_TIMEOUT_S", 0.01
        )

        async def fake_post(*a, **kw):
            r = MagicMock()
            r.status_code = 202
            r.content = b""
            r.raise_for_status = lambda: None
            return r

        client._client.post = fake_post  # type: ignore[assignment]
        try:
            await client._call("ping", {})
        except (TimeoutError, Exception):
            pass
        # Whatever the exception, the entry must be gone.
        assert client._pending == {}, (
            f"_pending should be empty after wait_for timeout; "
            f"leaked entries: {list(client._pending.keys())!r}"
        )

    asyncio.run(drive())


# ===========================================================================
# 3. SSE bare-data event-type default
# ===========================================================================


def test_stream_loop_defaults_event_type_to_message() -> None:
    """Per WHATWG SSE spec §9.2.6, an event with no `event:` line
    has type `"message"`. The previous shape required `event_type`
    to be non-empty and silently dropped bare `data:` events — the
    most common shape for spec-compliant servers. Source pin on
    the new default."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    # The dispatch line passes `event_type or "message"`.
    assert 'event_type or "message"' in src, (
        "Stream loop must default missing event_type to "
        "'message' per WHATWG SSE spec."
    )
    # And the flush guard depends only on data_lines being non-
    # empty (not on event_type also being set).
    assert "if data_lines:" in src


# ===========================================================================
# 4. SSE reconnect with Last-Event-ID
# ===========================================================================


def test_stream_loop_wraps_in_reconnect_loop() -> None:
    """`_stream_loop` runs in a `while not self._closed:` loop so
    a transient drop reconnects. Source pin."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    assert "while not self._closed:" in src
    # And the loop uses exponential backoff.
    assert "_RECONNECT_BACKOFF_INITIAL_S" in src
    assert "_RECONNECT_BACKOFF_MAX_S" in src
    # On each reconnect, sleeps for `backoff` seconds.
    assert "await asyncio.sleep(backoff)" in src


def test_stream_loop_tracks_last_event_id_for_reconnect() -> None:
    """When the server emits `id:` lines, the client captures the
    value and sends `Last-Event-ID` on the next reconnect so the
    server can resume after the last-seen event."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    assert 'line.startswith("id:")' in src
    assert "last_event_id" in src
    assert '"Last-Event-ID"' in src


def test_stream_loop_honors_server_retry_hint() -> None:
    """Per WHATWG SSE, the server can send `retry: <ms>` to
    suggest a reconnect interval. The client respects it (capped
    by `_RECONNECT_BACKOFF_MAX_S`)."""
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient._stream_loop)
    assert 'line.startswith("retry:")' in src


# ===========================================================================
# 5. Supervisor dead-entry leak on bridge error
# ===========================================================================


def test_on_bridge_done_evicts_dead_entry_from_active() -> None:
    """If `ServerBridge.run()` raises (e.g. MCP `initialize` fails
    on a hostile server), the task exits. Previously
    `_on_bridge_done` only logged the exception; the slot in
    `_active` stayed occupied with the dead task, the next
    reconcile saw `existing is not None` with unchanged config,
    and the bridge never respawned. Fix: evict the entry on done."""
    from bp_mcp_bridge.supervisor import Supervisor

    src = inspect.getsource(Supervisor._on_bridge_done)
    assert "self._active" in src, (
        "_on_bridge_done must touch `self._active` to evict the "
        "dead entry."
    )
    assert "pop" in src, (
        "_on_bridge_done must remove the dead entry from the map."
    )
    # And the eviction is keyed on `task.get_name()` so it finds
    # the right slot.
    assert "task.get_name()" in src


def test_on_bridge_done_respects_restart_in_progress() -> None:
    """A config-change restart explicitly cancels the OLD task
    (via `_stop`) and spawns a NEW one. If the eviction check
    were naive (`del self._active[sid]`), it would delete the
    NEW entry on the OLD task's done-callback firing.

    Defence: only evict if the current entry's task IS still
    this task. Pinned via the `current.task is task` check."""
    from bp_mcp_bridge.supervisor import Supervisor

    src = inspect.getsource(Supervisor._on_bridge_done)
    assert "current.task is task" in src, (
        "Eviction must guard against a restart-in-progress race "
        "(current.task is task)."
    )
