"""ProgressEmitter lifecycle: a single FIFO drain task, retained and
reaped.

History: the emitter used to schedule one
`asyncio.create_task(self.emit(...))` per `chunk()` / `status()` /
`tool_call()` / `tool_result()` call, tracked in a strong-ref set so
the runtime's WEAK task book-keeping couldn't GC a pending emit. That
fixed the "Task was destroyed but it is pending!" leak but left the
emit *order* at the mercy of the event loop.

The rework replaces the per-call task set with ONE long-lived
consumer task (`_drain`) draining a FIFO queue. The leak concern
becomes: that single task must be strongly referenced on the
instance and reaped on `aclose()` (no orphan, no lost queued frames).
These tests pin that lifecycle.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock


def _make_emitter() -> tuple[Any, MagicMock]:
    """Build a ProgressEmitter with a mocked dispatcher + transport.

    Returns (emitter, transport_send_mock) so tests can assert on the
    frames put on the wire.
    """
    from bp_sdk.progress import ProgressEmitter

    ctx = MagicMock()
    ctx.trace_id = "0" * 32
    ctx.span_id = "0" * 16
    ctx.task_id = "task_abc"

    transport = MagicMock()
    transport.send = AsyncMock()

    dispatcher = MagicMock()
    dispatcher.agent.info.agent_id = "agt_under_test"
    dispatcher.transport = transport

    return ProgressEmitter(ctx, dispatcher), transport.send


def test_no_drain_task_until_first_emit() -> None:
    """A handler that never emits progress must not spawn an idle
    drain task."""

    async def _run() -> None:
        emitter, _ = _make_emitter()
        assert emitter._drain_task is None
        # aclose() on a never-used emitter is a clean no-op.
        await emitter.aclose()
        assert emitter._drain_task is None

    asyncio.run(_run())


def test_first_emit_spawns_a_single_retained_drain_task() -> None:
    """The drain task is created lazily on first emit AND held on the
    instance (strong ref) — a regression dropping the reference would
    let the runtime GC it mid-flight, silently stranding every queued
    Progress frame."""

    async def _run() -> None:
        emitter, _ = _make_emitter()
        emitter.chunk("a")
        first = emitter._drain_task
        assert isinstance(first, asyncio.Task)
        assert not first.done()
        # Subsequent emits reuse the SAME drain task (one consumer,
        # not one-per-call).
        emitter.status("running")
        assert emitter._drain_task is first
        await emitter.aclose()

    asyncio.run(_run())


def test_aclose_reaps_the_drain_task() -> None:
    """After `aclose()` the drain task is done (not left pending /
    orphaned) and the already-queued frame was delivered first."""

    async def _run() -> None:
        emitter, send = _make_emitter()
        emitter.chunk("hello")
        task = emitter._drain_task
        assert task is not None and not task.done()
        await emitter.aclose()
        assert task.done()
        # Best-effort flush: the queued frame went out before the
        # drain stopped.
        assert send.await_count == 1
        sent = send.await_args.args[0]
        assert sent.event == "chunk"
        assert sent.content == "hello"

    asyncio.run(_run())


def test_aclose_is_idempotent() -> None:
    """The dispatcher teardown `finally` may run `aclose()` once, but
    a defensive double-close (or a future second caller) must not
    raise."""

    async def _run() -> None:
        emitter, _ = _make_emitter()
        emitter.chunk("x")
        await emitter.aclose()
        await emitter.aclose()  # must not raise

    asyncio.run(_run())


def test_emit_after_close_is_dropped_not_resurrected() -> None:
    """Once closed, a late convenience call must NOT silently spawn a
    fresh drain task that outlives the handler."""

    async def _run() -> None:
        emitter, send = _make_emitter()
        emitter.chunk("a")
        await emitter.aclose()
        closed_task = emitter._drain_task
        emitter.chunk("late")  # post-close — dropped
        # No new drain task, nothing sent for the late call.
        assert emitter._drain_task is closed_task
        assert send.await_count == 1

    asyncio.run(_run())
