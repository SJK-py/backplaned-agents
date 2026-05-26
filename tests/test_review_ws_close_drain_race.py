"""Audit MED-2: WebSocketTransport.close() shutdown-drain race.

`_send_pump` does `frame = await _outbox.get()` THEN
`await ws.send(...)`. The old `close()` drained with
`while not _outbox.empty()` — which is True the instant the frame
is popped but BEFORE it is on the wire. close() then `_closed.set()`
+ cancels the pump mid-`ws.send`; the pump re-queues the frame and
dies, so a shutdown-emitted terminal CANCELLED Result is never sent
and the calling parent hangs to correlation_timeout.

Fix: drain via `_outbox.join()` + `task_done()` so the drain waits
for the frame currently IN `ws.send`, not merely popped. These
tests pin the join-vs-empty divergence and the task_done() balance.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest


def _transport():  # type: ignore[no-untyped-def]
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import WebSocketTransport

    config = MagicMock()
    config.progress_buffer_size = 8
    info = MagicMock()
    info.agent_id = "agt_test"
    return WebSocketTransport(config, info=info)


def _frame(kind: str = "Result"):  # type: ignore[no-untyped-def]
    f = MagicMock()
    f.type = kind
    return f


def test_join_waits_for_in_flight_send_but_empty_would_not() -> None:
    """The decisive pin. While `ws.send` is mid-flight: the queue is
    already empty (old drain would exit + cancel the pump, losing the
    frame), but `_outbox.join()` is NOT satisfied (new drain waits).
    Once the send completes, `join()` returns."""

    async def _run() -> None:
        t = _transport()
        sending = asyncio.Event()
        release = asyncio.Event()

        async def _slow_send(_payload: Any) -> None:
            sending.set()
            await release.wait()

        ws = MagicMock()
        ws.send = _slow_send

        await t._outbox.put(_frame())
        pump = asyncio.create_task(t._send_pump(ws))

        await asyncio.wait_for(sending.wait(), 1.0)  # pump is inside ws.send

        # OLD drain basis: queue is already empty here.
        assert t._outbox.empty() is True
        # NEW drain basis: join() must NOT be satisfied — the frame
        # is popped but not yet on the wire.
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(t._outbox.join(), 0.1)

        # Send completes → task_done() → join() resolves promptly.
        release.set()
        await asyncio.wait_for(t._outbox.join(), 1.0)

        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    asyncio.run(_run())


def test_task_done_balanced_on_send_failure_so_join_can_progress() -> None:
    """On `ws.send` failure the frame is re-queued AND `task_done()`
    is called for the original pop. Net unfinished count must equal
    the single re-queued item (not 2) — otherwise `join()` could
    never reach zero even after the re-queued frame is later sent."""

    async def _run() -> None:
        t = _transport()

        ws = MagicMock()

        async def _boom(_payload: Any) -> None:
            raise ConnectionResetError("peer gone")

        ws.send = _boom

        await t._outbox.put(_frame())
        with pytest.raises(ConnectionResetError):
            await t._send_pump(ws)

        # Re-queued (preserved for reconnect)...
        assert t._outbox.qsize() == 1
        # ...and the unfinished count is exactly 1, not 2 — i.e. the
        # original get() was balanced by a task_done(). A fresh pump
        # draining the one re-queued item then lets join() reach 0.
        assert t._outbox._unfinished_tasks == 1

        recovered = await t._outbox.get()
        t._outbox.task_done()
        assert recovered.type == "Result"
        await asyncio.wait_for(t._outbox.join(), 1.0)

    asyncio.run(_run())


def test_task_done_balanced_on_queuefull_drop() -> None:
    """If the re-queue hits QueueFull (frame dropped + logged),
    `task_done()` must STILL fire so join() isn't wedged by a
    phantom unfinished item."""

    async def _run() -> None:
        t = _transport()
        # maxsize 1: pop one, then a second live item keeps the queue
        # full so the failed frame's re-queue raises QueueFull.
        t._outbox = asyncio.Queue(maxsize=1)

        ws = MagicMock()

        async def _boom(_payload: Any) -> None:
            # Fill the queue while THIS frame is in-flight so the
            # except-path put_nowait sees a full queue.
            t._outbox.put_nowait(_frame("Progress"))
            raise ConnectionResetError("peer gone")

        ws.send = _boom

        await t._outbox.put(_frame("Result"))
        with pytest.raises(ConnectionResetError):
            await t._send_pump(ws)

        # The dropped Result's get() was balanced; only the live
        # Progress frame remains unfinished.
        assert t._outbox.qsize() == 1
        assert t._outbox._unfinished_tasks == 1

    asyncio.run(_run())


def test_close_drain_uses_join_not_empty_poll() -> None:
    """Source pin: close() must drain via `_outbox.join()` and the
    pump must `task_done()` — not the racy `while not empty()` poll."""
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import WebSocketTransport

    close_src = inspect.getsource(WebSocketTransport.close)
    assert "_outbox.join()" in close_src
    assert "while not self._outbox.empty()" not in close_src

    pump_src = inspect.getsource(WebSocketTransport._send_pump)
    assert pump_src.count("self._outbox.task_done()") == 2  # success + except
