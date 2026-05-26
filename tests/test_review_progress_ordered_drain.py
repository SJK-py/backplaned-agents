"""ProgressEmitter ordering + observability + dispatcher teardown.

The single-FIFO-consumer rework exists so progress frames reach the
wire in the handler's call order. The old per-call
`asyncio.create_task(self.emit(...))` design let a slow early send be
overtaken by a fast later one (scrambled stream). It also swallowed
every `transport.send` exception silently, and the drain task was
never stopped at handler teardown.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_emitter() -> tuple[Any, MagicMock]:
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


def test_order_preserved_even_when_early_sends_are_slower() -> None:
    """The decisive property. `send` for earlier items sleeps LONGER
    than for later ones. Independent per-call tasks would let the
    short later sends finish first (out-of-order on the wire). The
    single FIFO consumer awaits each send before the next, so the
    delivered order is exactly the enqueue order regardless of
    per-send latency."""

    async def _run() -> None:
        emitter, _ = _make_emitter()
        delivered: list[str] = []

        async def _timed_send(frame: Any) -> None:
            # Descending delay: "0" waits longest, "4" shortest.
            idx = int(frame.content)
            await asyncio.sleep((5 - idx) * 0.01)
            delivered.append(frame.content)

        emitter._dispatcher.transport.send = _timed_send  # type: ignore[attr-defined]

        for i in range(5):
            emitter.chunk(str(i))
        await emitter.aclose()

        assert delivered == ["0", "1", "2", "3", "4"]

    asyncio.run(_run())


def test_emit_failure_is_logged_not_silently_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing `transport.send` must (a) be observable — a debug
    log, not the old silent `pass` — and (b) NOT kill the drain: the
    next queued frame still goes out."""

    async def _run() -> None:
        emitter, _ = _make_emitter()
        calls: list[str] = []

        async def _flaky_send(frame: Any) -> None:
            calls.append(frame.content)
            if frame.content == "boom":
                raise RuntimeError("transport blew up")

        emitter._dispatcher.transport.send = _flaky_send  # type: ignore[attr-defined]

        with caplog.at_level(logging.DEBUG, logger="bp_sdk.progress"):
            emitter.chunk("ok-1")
            emitter.chunk("boom")
            emitter.chunk("ok-2")
            await emitter.aclose()

        # The drain survived the failing send and delivered the rest.
        assert calls == ["ok-1", "boom", "ok-2"]
        failed = [
            r for r in caplog.records
            if r.message == "progress_emit_failed"
        ]
        assert len(failed) == 1
        assert failed[0].dropped_event == "chunk"  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_aclose_is_bounded_when_transport_is_wedged() -> None:
    """A drain wedged inside a hung `transport.send` must not stall
    handler teardown: `aclose()` flushes for at most
    `_ACLOSE_DRAIN_TIMEOUT_S` then force-cancels + reaps the drain."""
    from bp_sdk.progress import ProgressEmitter

    async def _run() -> None:
        emitter, _ = _make_emitter()
        # Shrink the budget so the test is fast but still exercises
        # the timeout->cancel->reap path.
        emitter._ACLOSE_DRAIN_TIMEOUT_S = 0.05

        async def _hang(*_a: Any, **_kw: Any) -> None:
            await asyncio.Event().wait()  # never resolves

        emitter._dispatcher.transport.send = _hang  # type: ignore[attr-defined]

        emitter.chunk("stuck")
        task = emitter._drain_task
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await emitter.aclose()
        elapsed = loop.time() - t0

        assert task is not None and task.done()
        assert elapsed < 1.0  # bounded, nowhere near hanging forever
        assert isinstance(ProgressEmitter._ACLOSE_DRAIN_TIMEOUT_S, float)

    asyncio.run(_run())


def test_dispatch_run_handler_closes_progress() -> None:
    """Source pin: the handler-teardown `finally` must call
    `progress.aclose()` — symmetric with `files.cleanup()`. Without
    it the drain task leaks past the handler and queued frames are
    lost. Guards against the hook being dropped in a refactor."""
    from bp_sdk.dispatch import Dispatcher

    src = inspect.getsource(Dispatcher._run_handler)
    assert "progress.aclose()" in src, (
        "progress drain is not closed in _run_handler teardown — "
        "queued Progress frames will leak / be lost"
    )
    # And it must be guarded like the files cleanup (None-safe).
    assert "progress = ctx.progress" in src
