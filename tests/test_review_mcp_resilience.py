"""MCP bridge resilience — pre-ship Tier-2 fixes.

`SseMcpClient.aclose` previously caught the catch-all
`except (asyncio.CancelledError, Exception): pass` on the
stream-task await. The intent was "absorb the cancel we just
sent", but the wider Exception branch silently swallowed real
errors (bugs in the stream loop, OS errors, etc.) on teardown.
Tearing down anyway is the right call, but logging the
non-cancel path keeps real bugs surface-visible.
"""

from __future__ import annotations

import inspect

import pytest


def test_sse_aclose_separates_cancel_from_exception() -> None:
    """`SseMcpClient.aclose` must NOT use the
    `except (CancelledError, Exception)` catch-all — that silently
    drops genuine stream-task errors. Source pin: separate
    `except asyncio.CancelledError:` (pass) from
    `except Exception:` (log via logger.exception)."""
    pytest.importorskip("httpx")
    from bp_mcp_bridge.mcp_client import SseMcpClient

    src = inspect.getsource(SseMcpClient.aclose)
    # The combined catch must be gone.
    assert "except (asyncio.CancelledError, Exception)" not in src, (
        "aclose must split CancelledError from broad Exception so "
        "real stream-task bugs aren't silently dropped at teardown."
    )
    # And the broad Exception branch logs.
    assert "logger.exception" in src or "_logger.exception" in src, (
        "Non-cancel teardown exceptions must be logged."
    )


def test_sse_aclose_still_proceeds_on_stream_task_error() -> None:
    """Functional pin: if the stream task raises during aclose's
    await, aclose still completes the teardown (rejects pending
    futures, closes httpx). A regression that re-raises would
    leave the client half-closed."""
    pytest.importorskip("httpx")
    import asyncio

    from bp_mcp_bridge.mcp_client import McpError, SseMcpClient

    async def drive() -> asyncio.Future:
        client = SseMcpClient("https://x/")

        # Plant a stream task that raises after one scheduling tick
        # — close enough to "running when aclose fires" for the
        # path we want to test, and aclose's `await` retrieves
        # the exception so asyncio doesn't warn about it.
        async def _stream() -> None:
            await asyncio.sleep(0)
            raise RuntimeError("simulated stream crash")

        client._stream_task = asyncio.create_task(_stream())

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        client._pending[1] = fut

        await client.aclose()
        return fut

    fut = asyncio.run(drive())

    # aclose still rejected the pending future.
    assert fut.done()
    with pytest.raises(McpError, match="closed before response"):
        fut.result()
