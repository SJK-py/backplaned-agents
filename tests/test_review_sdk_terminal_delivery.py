"""R11: terminal-frame delivery hardening (3 batched).

CRIT-1  `_drain_in_flight` hard-cancelled handler tasks but
   `break`'d WITHOUT awaiting them, so `run_until` tore the
   transport down before `_run_handler`'s
   `except asyncio.CancelledError` (R9) could emit the terminal
   CANCELLED Result — the calling parent hung to
   `correlation_timeout`. Now the cancelled tasks are awaited
   (bounded); ws transport flushes its outbox before cancelling
   the send pump.

HIGH-1  `_handle_llm_result` dropped the terminal `LlmResultFrame`
   on QueueFull (delta-style). A slow-but-ALIVE consumer (no
   timeout on `_queue_get_or_cancel`; router sends exactly one
   terminator) then blocked forever. Now the terminator evicts
   oldest deltas to guarantee delivery.

MED-1  `await files.cleanup()` in `_run_handler`'s `finally` could
   re-raise a SECOND `CancelledError` out of the `finally`,
   skipping the terminal Result send. Now captured, not
   propagated.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

try:
    from pydantic import BaseModel

    class _DrainPayload(BaseModel):
        x: int = 0
except Exception:  # pragma: no cover
    _DrainPayload = None  # type: ignore[assignment]


def _bare_dispatcher():  # type: ignore[no-untyped-def]
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.dispatch import Dispatcher
    from bp_sdk.transport.inproc import InProcessTransport

    agent = Agent(info=AgentInfo(agent_id="agt_a", description="t"))
    transport = InProcessTransport()
    transport.attach(inbound=asyncio.Queue(), outbound=asyncio.Queue())
    return agent, transport, Dispatcher(agent, transport)


# ===========================================================================
# CRIT-1 — drain awaits hard-cancelled handlers; terminal Result delivered
# ===========================================================================


def test_drain_awaits_cancelled_handler_and_terminal_result_is_sent() -> None:
    """Behavioural: an uncooperative handler (ignores cancel_token)
    is hard-cancelled at the drain deadline. `_drain_in_flight`
    must AWAIT it so `_run_handler`'s CancelledError path emits the
    terminal CANCELLED ResultFrame BEFORE drain returns (pre-fix
    the frame was never sent — parent hung)."""
    pytest.importorskip("pydantic")
    pytest.importorskip("fastapi")
    from unittest.mock import MagicMock

    from bp_protocol.frames import NewTaskFrame
    from bp_protocol.types import TaskStatus
    from bp_sdk.context import CancelToken, TaskContext
    from bp_sdk.dispatch import _ActiveTask

    agent, transport, disp = _bare_dispatcher()
    started = asyncio.Event()

    @agent.handler
    async def slow(ctx, payload: _DrainPayload) -> None:  # type: ignore[no-untyped-def]
        started.set()
        await asyncio.sleep(60)  # ignores cancel_token entirely

    frame = NewTaskFrame(
        agent_id="router", trace_id="0" * 32, span_id="0" * 16,
        task_id="tsk_d", destination_agent_id="agt_a",
        user_id="u", session_id="s", payload={"x": 1},
    )

    async def _run() -> None:
        handler = agent._handlers_by_mode["_DrainPayload"]
        ct = CancelToken()
        ctx = TaskContext(
            task_id="tsk_d", parent_task_id=None, user_id="u",
            user_level="tier0", session_id="s",
            trace_id="0" * 32, span_id="0" * 16, deadline=None,
            cancel_token=ct, log=MagicMock(),
        )
        ht = asyncio.create_task(
            disp._run_handler(handler, ctx, _DrainPayload(x=1), frame)
        )
        disp._active["tsk_d"] = _ActiveTask(
            task_id="tsk_d", cancel_token=ct, handler_task=ht
        )
        await started.wait()

        # grace_s=0 → immediate hard-cancel; the fix must AWAIT ht.
        await asyncio.wait_for(
            disp._drain_in_flight(grace_s=0.0, hard_drain_timeout_s=5.0),
            timeout=5.0,
        )

        # The cancelled handler ran its CANCELLED-Result emission to
        # completion before drain returned.
        assert ht.done()
        sent = transport._outbound.get_nowait()
        assert sent.type == "Result"
        assert sent.status == TaskStatus.CANCELLED
        assert sent.status_code == 499
        assert sent.task_id == "tsk_d"
        assert disp._active == {}

    asyncio.run(_run())


def test_drain_in_flight_awaits_the_cancelled_tasks_sourcepin() -> None:
    """AST pin: at the deadline, `_drain_in_flight` collects the
    handler tasks and `await`s them (asyncio.wait) — guards against
    a refactor reintroducing the cancel-then-break-without-await
    bug."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    src = textwrap.dedent(inspect.getsource(dispatch.Dispatcher._drain_in_flight))
    tree = ast.parse(src).body[0]
    # An `await asyncio.wait(...)` (or gather) appears, and a bare
    # `break` is NOT the statement immediately after the cancel loop
    # without an intervening await.
    has_await_wait = any(
        isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr in ("wait", "gather")
        for n in ast.walk(tree)
    )
    assert has_await_wait, "_drain_in_flight must await the cancelled handlers"
    assert "handler_task.cancel()" in src


def test_ws_close_flushes_outbox_before_cancelling_pump() -> None:
    """AST/source pin: `WebSocketTransport.close()` drains `_outbox`
    (bounded by `_CLOSE_DRAIN_TIMEOUT_S`) BEFORE `_closed.set()` /
    cancelling loop tasks — otherwise a shutdown-cancelled
    handler's terminal frame is lost with the send pump. The drain
    is `_outbox.join()` (audit MED-2), NOT an `empty()` poll, so it
    also covers the frame currently in-flight inside `ws.send`."""
    from bp_sdk.transport import ws

    assert isinstance(ws._CLOSE_DRAIN_TIMEOUT_S, (int, float))
    assert ws._CLOSE_DRAIN_TIMEOUT_S > 0
    src = inspect.getsource(ws.WebSocketTransport.close)
    drain_idx = src.index("_outbox.join()")
    closed_idx = src.index("self._closed.set()")
    cancel_idx = src.index(".cancel()")
    assert drain_idx < closed_idx < cancel_idx, (
        "outbox drain must precede _closed.set() and pump cancel"
    )
    assert "while not self._outbox.empty()" not in src  # not the racy poll
    assert "asyncio.timeout(" in src  # bounded


# ===========================================================================
# HIGH-1 — terminal LlmResultFrame is never drop-on-full
# ===========================================================================


def test_llm_terminator_evicts_oldest_instead_of_dropping() -> None:
    """Behavioural: saturate the bounded stream queue, then a
    terminal LlmResultFrame must still land (oldest delta evicted),
    and the call returns promptly (recv loop never blocks)."""
    pytest.importorskip("pydantic")
    from bp_protocol.frames import LlmResultFrame
    from bp_sdk import llm

    _, _, disp = _bare_dispatcher()

    async def _run() -> None:
        cid = "corr-term"
        q: asyncio.Queue = asyncio.Queue(maxsize=llm._LLM_STREAM_QUEUE_MAX)
        disp._llm_streams[cid] = q
        for i in range(llm._LLM_STREAM_QUEUE_MAX):
            q.put_nowait(("delta", i))

        term = LlmResultFrame(
            agent_id="router", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id=cid,
        )
        await asyncio.wait_for(disp._handle_llm_result(term), timeout=1.0)

        # Still at the cap, but the TERMINATOR is now present.
        assert q.qsize() == llm._LLM_STREAM_QUEUE_MAX
        drained = [q.get_nowait() for _ in range(q.qsize())]
        assert term in drained, "terminal frame must be delivered, not dropped"
        # Exactly one oldest delta was evicted to make room.
        assert ("delta", 0) not in drained
        assert ("delta", 1) in drained

    asyncio.run(_run())


def test_handle_llm_result_has_no_drop_and_return_for_terminator() -> None:
    """AST pin: the streaming branch must NOT `return` straight out
    of an `except asyncio.QueueFull` (the old drop). It must loop
    evicting until the terminator is enqueued."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    src = textwrap.dedent(
        inspect.getsource(dispatch.Dispatcher._handle_llm_result)
    )
    tree = ast.parse(src).body[0]
    # No QueueFull handler whose body is just a log + (implicit)
    # fallthrough that drops the frame: assert a get_nowait()
    # eviction exists in the streaming path.
    assert "get_nowait()" in src
    assert "QueueFull" in src
    # The terminal put is inside a loop (retry after eviction).
    has_while = any(isinstance(n, ast.While) for n in ast.walk(tree))
    assert has_while, "terminator enqueue must retry after eviction"


# ===========================================================================
# MED-1 — files.cleanup() CancelledError must not abort the terminal send
# ===========================================================================


def test_run_handler_cleanup_cancellederror_is_captured_not_propagated() -> None:
    """AST pin: the `await files.cleanup()` in `_run_handler`'s
    `finally` is guarded by `except asyncio.CancelledError` that
    folds into `cancelled_exc` (so a second cancel during cleanup
    can't skip the terminal Result send + the contractual
    re-raise)."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    src = textwrap.dedent(inspect.getsource(dispatch.Dispatcher._run_handler))
    assert "await files.cleanup()" in src
    # AST: find the `try` whose body calls `files.cleanup()`; one of
    # its handlers must be `except asyncio.CancelledError` whose
    # body assigns `cancelled_exc`.
    tree = ast.parse(src).body[0]
    ok = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(
            isinstance(c, ast.Attribute) and c.attr == "cleanup"
            for stmt in node.body
            for c in ast.walk(stmt)
        ):
            continue
        for h in node.handlers:
            is_cancelled = (
                isinstance(h.type, ast.Attribute)
                and h.type.attr == "CancelledError"
            )
            assigns = any(
                isinstance(t, ast.Name) and t.id == "cancelled_exc"
                for n in ast.walk(h)
                if isinstance(n, ast.Assign)
                for t in n.targets
            )
            if is_cancelled and assigns:
                ok = True
    assert ok, "files.cleanup() CancelledError must fold into cancelled_exc"
