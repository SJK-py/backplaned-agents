"""Tests for the fourth-pass review M-3 + M-5 fixes.

  - M-3: SDK `WebSocketTransport._send_pump` re-queues the frame on
    `ws.send` failure (broken pipe, peer reset, asyncio.CancelledError
    on supervisor reconnect). Mirrors the router-side C-3 fix in
    `bp_router/ws_hub.py:_send_loop`. Without the symmetric fix, a
    frame already popped from `_outbox` before `ws.send` raised was
    silently lost — Result / Ack / Progress frames the router was
    expecting vanished, and the parent task hung until the deadline
    sweep failed it.
  - M-5: `_handle_llm_request` rejects duplicate `correlation_id`s
    rather than silently orphaning the prior router-side asyncio.Task.
    The orphan would have stopped being tracked in `entry.llm_tasks`,
    couldn't be cancelled on `_on_disconnect`, kept consuming
    provider tokens until upstream completion, and couldn't be
    reached by the `Cancel{ref_correlation_id=...}` abort path.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# M-3: SDK send-pump re-queue on ws.send failure
# ===========================================================================


def test_m3_send_pump_requeues_frame_on_ws_send_exception() -> None:
    """Behavioural: when `ws.send` raises a regular Exception, the
    frame must be put back on `_outbox` so a subsequent reconnect
    can drain it. The send_pump itself re-raises so the supervisor
    triggers reconnect."""
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import WebSocketTransport

    async def _scenario() -> None:
        # Construct a transport without going through `connect()`
        # (avoids the network handshake).
        config = MagicMock()
        config.progress_buffer_size = 4
        info = MagicMock()
        info.agent_id = "agent-A"
        t = WebSocketTransport(config, info=info)

        # Simulated frame and a ws.send that raises.
        frame = MagicMock()
        frame.type = "Result"
        ws = MagicMock()
        ws.send = AsyncMock(side_effect=ConnectionResetError("peer gone"))

        # Pre-populate the outbox.
        await t._outbox.put(frame)

        # Pump should re-queue + re-raise.
        with pytest.raises(ConnectionResetError):
            await t._send_pump(ws)

        # Frame must be back on the outbox.
        assert t._outbox.qsize() == 1
        # And it's the same object we put in (no Pydantic round-trip).
        recovered = await t._outbox.get()
        assert recovered is frame

    asyncio.run(_scenario())


def test_m3_send_pump_requeues_on_cancellation() -> None:
    """`asyncio.CancelledError` mid-send must also re-queue the
    frame. The supervisor reconnect path cancels `_send_pump` to
    swap to a fresh socket; the frame popped just before the
    cancel must NOT be lost."""
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import WebSocketTransport

    async def _scenario() -> None:
        config = MagicMock()
        config.progress_buffer_size = 4
        info = MagicMock()
        info.agent_id = "agent-A"
        t = WebSocketTransport(config, info=info)

        frame = MagicMock()
        frame.type = "Result"
        ws = MagicMock()
        ws.send = AsyncMock(side_effect=asyncio.CancelledError())

        await t._outbox.put(frame)

        with pytest.raises(asyncio.CancelledError):
            await t._send_pump(ws)

        assert t._outbox.qsize() == 1
        recovered = await t._outbox.get()
        assert recovered is frame

    asyncio.run(_scenario())


def test_m3_send_pump_logs_when_outbox_full_on_requeue() -> None:
    """If the outbox is FULL when we try to re-queue, the frame
    is dropped — but we log a warning so operators can correlate
    with downstream "missing terminal" reports. Pin the log
    event name so a future refactor can't silently swallow it."""
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import WebSocketTransport

    src = inspect.getsource(WebSocketTransport._send_pump)
    assert "frame_dropped_send_failed_queue_full" in src, (
        "review4-M3 regression: outbox-full warning event missing"
    )
    assert "QueueFull" in src


def test_m3_send_pump_re_raises_after_requeue() -> None:
    """The exception must be re-raised so the connection
    supervisor sees the failure and triggers reconnect. Source
    pin: `raise` follows the re-queue."""
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import WebSocketTransport

    src = inspect.getsource(WebSocketTransport._send_pump)
    # Find the except block and verify it ends with `raise`.
    except_idx = src.find("except (asyncio.CancelledError, Exception)")
    assert except_idx > 0, "except clause missing"
    # The raise must come after the re-queue logic.
    raise_idx = src.find("raise", except_idx)
    assert raise_idx > except_idx, (
        "review4-M3: send_pump must re-raise after re-queue so "
        "the supervisor sees the failure"
    )


def test_m3_router_and_sdk_have_symmetric_send_loop_recovery() -> None:
    """Cross-cutting pin: both sides must use the same shape. If
    a future PR diverges (e.g. router-side stops re-queuing), the
    pair becomes asymmetric again and this test catches it."""
    pytest.importorskip("websockets")
    pytest.importorskip("fastapi")
    from bp_router import ws_hub
    from bp_sdk.transport import ws as sdk_ws

    router_src = inspect.getsource(ws_hub._send_loop)
    sdk_src = inspect.getsource(sdk_ws.WebSocketTransport._send_pump)

    # Both must re-queue (`put_nowait` on the outbox) on send failure.
    assert "put_nowait(frame)" in router_src
    assert "put_nowait(frame)" in sdk_src
    # Both must log the same event name on QueueFull.
    assert "frame_dropped_send_failed_queue_full" in router_src
    assert "frame_dropped_send_failed_queue_full" in sdk_src


# ===========================================================================
# M-5: duplicate correlation_id rejection in _handle_llm_request
# ===========================================================================


def test_m5_duplicate_correlation_id_rejects_with_frame_invalid() -> None:
    """Behavioural: when `entry.llm_tasks` already has an entry for
    `frame.correlation_id` AND that entry isn't done, the second
    request must be rejected with an LlmResult carrying error.code =
    'frame_invalid' AND the prior task must remain unchanged."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import ErrorCode, LlmRequestFrame, LlmResultFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        # Build an entry with an already-running LLM task at the
        # contested correlation_id.
        first_task = asyncio.ensure_future(asyncio.sleep(3600))
        await asyncio.sleep(0)  # let it start
        try:
            entry = MagicMock()
            entry.agent_id = "agent-A"
            entry.llm_tasks = {"cid-X": first_task}
            entry.outbox = MagicMock()
            entry.outbox.put = AsyncMock()

            state = MagicMock()

            duplicate_frame = LlmRequestFrame(
                agent_id="agent-A",
                trace_id="0" * 32,
                span_id="0" * 16,
                correlation_id="cid-X",  # same as in flight
                kind="generate",
                messages=[{"role": "user", "content": "dup"}],
            )

            await dispatch._handle_llm_request(state, entry, duplicate_frame)

            # The first task is UNTOUCHED — we don't cancel or
            # replace it. The buggy agent's first request keeps
            # running.
            assert not first_task.cancelled()
            assert entry.llm_tasks["cid-X"] is first_task

            # An LlmResult error frame was put on the outbox.
            entry.outbox.put.assert_awaited_once()
            sent_frame = entry.outbox.put.call_args.args[0]
            assert isinstance(sent_frame, LlmResultFrame)
            assert sent_frame.ref_correlation_id == "cid-X"
            assert sent_frame.error is not None
            assert sent_frame.error.code == ErrorCode.FRAME_INVALID
            assert "duplicate" in sent_frame.error.message.lower()
        finally:
            first_task.cancel()
            try:
                await first_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_scenario())


def test_m5_done_existing_task_does_not_block_new_request() -> None:
    """Edge case: if the existing entry IS done (cleanup
    callback hasn't fired yet because tasks can complete before
    `add_done_callback`'s callback runs), the new request must
    proceed normally — the cleanup will eventually `pop` and
    `pop` is idempotent."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import LlmRequestFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        # An already-completed task at the correlation_id.
        async def _done() -> None:
            return None

        prior_task = asyncio.ensure_future(_done())
        await prior_task  # let it complete
        assert prior_task.done()

        entry = MagicMock()
        entry.agent_id = "agent-A"
        entry.llm_tasks = {"cid-X": prior_task}
        entry.outbox = MagicMock()
        entry.outbox.put = AsyncMock()

        state = MagicMock()
        # Stub _run_llm_call so we don't actually run an LLM call.
        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod._run_llm_call

        async def _stub_run(*args: Any, **kwargs: Any) -> None:
            return None

        dispatch_mod._run_llm_call = _stub_run
        try:
            new_frame = LlmRequestFrame(
                agent_id="agent-A",
                trace_id="0" * 32,
                span_id="0" * 16,
                correlation_id="cid-X",
                kind="generate",
                messages=[{"role": "user", "content": "ok"}],
            )
            await dispatch._handle_llm_request(state, entry, new_frame)
            # No rejection — outbox.put was NOT called with an
            # error LlmResult.
            entry.outbox.put.assert_not_awaited()
            # llm_tasks now holds the NEW task.
            assert entry.llm_tasks["cid-X"] is not prior_task
            # Drain the new task we just kicked off so it doesn't
            # leak.
            await asyncio.gather(
                *[t for t in [entry.llm_tasks.get("cid-X")] if t is not None],
                return_exceptions=True,
            )
        finally:
            dispatch_mod._run_llm_call = original

    asyncio.run(_scenario())


def test_m5_fresh_correlation_id_takes_normal_path() -> None:
    """Sanity-pin the happy path: a non-colliding correlation_id
    creates a new task and registers it."""
    pytest.importorskip("fastapi")
    from bp_protocol.frames import LlmRequestFrame
    from bp_router import dispatch

    async def _scenario() -> None:
        entry = MagicMock()
        entry.agent_id = "agent-A"
        entry.llm_tasks = {}
        entry.outbox = MagicMock()
        entry.outbox.put = AsyncMock()

        state = MagicMock()
        from bp_router import dispatch as dispatch_mod

        original = dispatch_mod._run_llm_call

        async def _stub_run(*args: Any, **kwargs: Any) -> None:
            return None

        dispatch_mod._run_llm_call = _stub_run
        try:
            frame = LlmRequestFrame(
                agent_id="agent-A",
                trace_id="0" * 32,
                span_id="0" * 16,
                correlation_id="cid-fresh",
                kind="generate",
                messages=[{"role": "user", "content": "hi"}],
            )
            await dispatch._handle_llm_request(state, entry, frame)
            # No rejection.
            entry.outbox.put.assert_not_awaited()
            # Task registered.
            assert "cid-fresh" in entry.llm_tasks
            # Drain so we don't leak.
            await asyncio.gather(
                *[t for t in [entry.llm_tasks.get("cid-fresh")] if t is not None],
                return_exceptions=True,
            )
        finally:
            dispatch_mod._run_llm_call = original

    asyncio.run(_scenario())


def test_m5_source_pin_handler_checks_existing_before_create_task() -> None:
    """Source pin: `_handle_llm_request` MUST check
    `entry.llm_tasks` membership BEFORE `asyncio.create_task`. A
    regression that flips the order would let the orphan-and-
    overwrite pattern back in."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_llm_request)
    member_idx = src.find("frame.correlation_id in entry.llm_tasks")
    create_idx = src.find("asyncio.create_task(")
    assert member_idx > 0, (
        "review4-M5 regression: dedup membership check missing"
    )
    assert create_idx > 0
    assert member_idx < create_idx, (
        "review4-M5: dedup check must run BEFORE create_task; "
        "otherwise the orphan-and-overwrite race is back"
    )
