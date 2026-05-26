"""Bounded FIFO queue on ProgressEmitter.

A single drain consumes the queue in order. If the transport hangs
(synchronous wait, not an exception) the drain stalls; without a
bound the queue would grow with every `chunk(...)` — an LLM streaming
10K tokens would OOM the process. The bound is the queue's `maxsize`
(`_PENDING_EMITS_SOFT_CAP`): once full, further emits are dropped with
a warn log carrying the dropped event name + the cap.

These tests pin the cap constant, the drop-warn, and that normal
under-cap operation delivers everything in order.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_emitter() -> tuple[Any, MagicMock]:
    """Build a ProgressEmitter with a transport whose `.send` is
    mocked and awaitable. Returns (emitter, transport_send_mock)."""
    from bp_sdk.progress import ProgressEmitter

    ctx = MagicMock()
    ctx.trace_id = "0" * 32
    ctx.span_id = "0" * 16
    ctx.task_id = "task_abc"

    transport = MagicMock()
    transport.send = AsyncMock()

    dispatcher = MagicMock()
    dispatcher.agent.info.agent_id = "agt_test"
    dispatcher.transport = transport

    return ProgressEmitter(ctx, dispatcher), transport.send


def test_emitter_exposes_soft_cap_constant() -> None:
    """Class-level constant so deployments can tune it via subclass /
    monkeypatch before the emitter (and thus its queue) is built."""
    from bp_sdk.progress import ProgressEmitter

    assert hasattr(ProgressEmitter, "_PENDING_EMITS_SOFT_CAP")
    assert isinstance(ProgressEmitter._PENDING_EMITS_SOFT_CAP, int)
    assert ProgressEmitter._PENDING_EMITS_SOFT_CAP > 0


def test_queue_maxsize_tracks_the_cap_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must actually be wired to the constant — a
    regression hard-coding `Queue()` (unbounded) would defeat the
    OOM defence silently."""
    from bp_sdk.progress import ProgressEmitter

    monkeypatch.setattr(ProgressEmitter, "_PENDING_EMITS_SOFT_CAP", 7)

    async def _run() -> None:
        emitter, _ = _make_emitter()
        assert emitter._queue.maxsize == 7

    asyncio.run(_run())


def test_emit_below_cap_delivers_everything_in_order() -> None:
    """Under the cap the drain delivers every queued frame, FIFO."""

    async def _run() -> None:
        emitter, send = _make_emitter()
        for i in range(10):
            emitter.chunk(f"chunk {i}")
        await emitter.aclose()  # deterministic flush point
        assert send.await_count == 10
        contents = [c.args[0].content for c in send.await_args_list]
        assert contents == [f"chunk {i}" for i in range(10)]

    asyncio.run(_run())


def test_emit_at_cap_drops_new_calls_with_warn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wedge `transport.send` so the drain stalls on the first item;
    the queue then fills to `maxsize` and further emits drop with a
    warn log. With cap=2 the layout is: 1 item in the wedged drain,
    2 in the full queue, the rest dropped."""
    from bp_sdk.progress import ProgressEmitter

    monkeypatch.setattr(ProgressEmitter, "_PENDING_EMITS_SOFT_CAP", 2)

    async def _run() -> None:
        emitter, _ = _make_emitter()

        entered = asyncio.Event()
        block = asyncio.Event()

        async def _wedged_send(*_a: Any, **_kw: Any) -> None:
            entered.set()
            await block.wait()

        emitter._dispatcher.transport.send = _wedged_send  # type: ignore[attr-defined]

        emitter.chunk("a")  # drain dequeues this and wedges in send
        await entered.wait()
        # Queue is now empty (a is in the wedged drain). Fill it.
        emitter.chunk("b")
        emitter.chunk("c")  # queue == [b, c], size 2 == maxsize
        assert emitter._queue.full()

        with caplog.at_level(logging.WARNING, logger="bp_sdk.progress"):
            emitter.chunk("d")  # QueueFull -> drop
            emitter.chunk("e")  # QueueFull -> drop

        # Queue did not grow past the cap.
        assert emitter._queue.qsize() == 2

        drops = [
            r for r in caplog.records
            if r.message == "progress_emit_dropped_queue_full"
        ]
        assert len(drops) == 2
        for r in drops:
            assert r.dropped_event == "chunk"  # type: ignore[attr-defined]
            assert r.cap == 2  # type: ignore[attr-defined]

        # Unblock + bounded teardown.
        block.set()
        await emitter.aclose()

    asyncio.run(_run())
