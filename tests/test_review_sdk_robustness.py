"""R9 MEDIUM: bp_sdk robustness (4 batched; a 5th candidate dropped
after verification).

Fresh-eyes R9 review of the agent-facing SDK surfaced four
robustness gaps:

SDK-1 `_run_handler` had no `except asyncio.CancelledError`
   (`CancelledError` is `BaseException`, not `Exception`). A
   cancelled handler task propagated the cancellation straight
   past the ResultFrame send → the calling parent's spawn future
   hung to `correlation_timeout` with NO terminal frame. And
   `_drain_in_flight` only tripped the cooperative cancel_token at
   the shutdown deadline, never hard-cancelling — an uncooperative
   handler ran unbounded past shutdown (and the new CancelledError
   path was unreachable).

SDK-2 `subscribe_progress` silently overwrote an existing
   subscriber, orphaning the prior queue (its SpawnStream blocked
   on `queue.get()` to timeout; its terminal Result mis-delivered
   to the new consumer).

SDK-3 `_handle_llm_delta` did `await queue.put()` into an
   UNBOUNDED queue — an abandoned stream grew memory one delta per
   inbound frame for the rest of the recv loop.

SDK-5 `_buffer_pending_progress` dropped the NEW task_id at the
   total-task cap, so a burst of never-subscribed task_ids
   permanently pinned all slots until each task's unrelated
   Result landed.

(SDK-4 — `_stream_with_retry` "deferred finally" — was DROPPED:
`_stream_one_attempt` raises `_raise_for_error` inside the
generator body, so its `finally` runs synchronously during the
raise unwind, before `_stream_with_retry` catches `LlmCallError`.
No deferred abort, no double provider call. Verified non-issue.)
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from unittest.mock import MagicMock

import pytest

try:
    from pydantic import BaseModel

    # Module-level so the handler decorator's decoration-time
    # `typing.get_type_hints` can resolve the annotation (a
    # function-local class is NOT importable from the handler
    # module's namespace — same constraint as test_delegation.py's
    # module-level `_DelegationPayload`).
    class _CancelPayload(BaseModel):
        x: int = 0
except Exception:  # pragma: no cover - pydantic always present in CI
    _CancelPayload = None  # type: ignore[assignment]


# ===========================================================================
# SDK-1: _run_handler emits a CANCELLED Result then re-raises;
#        _drain_in_flight escalates to handler_task.cancel()
# ===========================================================================


def test_run_handler_has_cancellederror_branch() -> None:
    pytest.importorskip("pydantic")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._run_handler)
    # The asyncio.CancelledError branch exists, BEFORE the broad
    # `except Exception` (it's BaseException so order is for
    # clarity, but the branch must be present and set CANCELLED).
    assert "except asyncio.CancelledError" in src
    assert "cancelled_exc" in src
    # It must re-raise (asyncio contract) — via the post-send
    # finally so the terminal Result is delivered first.
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]
    raises_cancelled = any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Name)
        and n.exc.id == "cancelled_exc"
        for n in ast.walk(fn)
    )
    assert raises_cancelled, "must re-raise the stored CancelledError"


def test_run_handler_sends_result_on_cancel_then_reraises() -> None:
    """Behavioural: a handler whose task is cancelled still emits a
    terminal CANCELLED ResultFrame, and the CancelledError still
    propagates afterwards."""
    pytest.importorskip("pydantic")
    pytest.importorskip("fastapi")
    from bp_protocol.frames import NewTaskFrame
    from bp_protocol.types import AgentInfo, TaskStatus
    from bp_sdk.agent import Agent
    from bp_sdk.dispatch import Dispatcher
    from bp_sdk.transport.inproc import InProcessTransport

    agent = Agent(info=AgentInfo(agent_id="agt_a", description="t"))

    started = asyncio.Event()

    @agent.handler
    async def slow(ctx, payload: _CancelPayload) -> None:  # type: ignore[no-untyped-def]
        started.set()
        await asyncio.sleep(60)  # will be cancelled

    transport = InProcessTransport()
    transport.attach(inbound=asyncio.Queue(), outbound=asyncio.Queue())
    disp = Dispatcher(agent, transport)

    frame = NewTaskFrame(
        agent_id="router", trace_id="0" * 32, span_id="0" * 16,
        task_id="tsk_c", destination_agent_id="agt_a",
        user_id="u", session_id="s", payload={"x": 1},
    )

    async def _run() -> None:
        handler = agent._handlers_by_mode["_CancelPayload"]
        from bp_sdk.context import CancelToken, TaskContext

        ctx = TaskContext(
            task_id="tsk_c", parent_task_id=None, user_id="u",
            user_level="tier0", session_id="s",
            trace_id="0" * 32, span_id="0" * 16, deadline=None,
            cancel_token=CancelToken(),
            log=MagicMock(),
        )
        t = asyncio.create_task(
            disp._run_handler(handler, ctx, _CancelPayload(x=1), frame)
        )
        await started.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # Despite the cancellation, a terminal CANCELLED ResultFrame
        # was queued on the transport outbound.
        sent = transport._outbound.get_nowait()
        assert sent.type == "Result"
        assert sent.status == TaskStatus.CANCELLED
        assert sent.task_id == "tsk_c"

    asyncio.run(_run())


def test_drain_in_flight_hard_cancels_handler_task() -> None:
    pytest.importorskip("pydantic")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._drain_in_flight)
    # At the deadline it must BOTH trip the token AND hard-cancel
    # the handler task.
    assert "cancel_token.trip(" in src
    assert "handler_task.cancel()" in src
    trip_idx = src.index("cancel_token.trip(")
    cancel_idx = src.index("handler_task.cancel()")
    # Both inside the same `now >= deadline` branch (cancel after
    # the trip).
    assert trip_idx < cancel_idx


# ===========================================================================
# SDK-2: subscribe_progress terminates a displaced subscriber
# ===========================================================================


def test_subscribe_progress_closes_displaced_subscriber() -> None:
    pytest.importorskip("pydantic")
    from bp_sdk.peers import _STREAM_CLOSED

    async def _run() -> None:
        disp = _bare_dispatcher()
        q1 = disp.subscribe_progress("tsk_x")
        assert disp._progress_subscribers["tsk_x"] is q1
        # Second subscribe for the same task displaces q1.
        q2 = disp.subscribe_progress("tsk_x")
        assert disp._progress_subscribers["tsk_x"] is q2
        # The displaced queue got the close sentinel so its
        # SpawnStream ends via StopAsyncIteration instead of
        # hanging.
        assert q1.get_nowait() is _STREAM_CLOSED

    asyncio.run(_run())


def test_subscribe_progress_no_sentinel_when_no_prior() -> None:
    """First subscribe (no prior subscriber) must NOT inject the
    sentinel — the fresh queue starts empty."""
    pytest.importorskip("pydantic")

    async def _run() -> None:
        disp = _bare_dispatcher()
        q = disp.subscribe_progress("tsk_y")
        assert q.empty()

    asyncio.run(_run())


# ===========================================================================
# SDK-3: bounded LLM stream queue + drop-on-full
# ===========================================================================


def test_llm_stream_queue_is_bounded() -> None:
    from bp_sdk import llm

    assert hasattr(llm, "_LLM_STREAM_QUEUE_MAX")
    assert isinstance(llm._LLM_STREAM_QUEUE_MAX, int)
    assert llm._LLM_STREAM_QUEUE_MAX > 0
    src = inspect.getsource(llm)
    assert "asyncio.Queue(maxsize=_LLM_STREAM_QUEUE_MAX)" in src


def test_handle_llm_delta_drops_on_full_not_blocks() -> None:
    """`_handle_llm_delta` must `put_nowait` + drop-on-QueueFull,
    not `await queue.put` (which blocks the recv loop on an
    abandoned stream)."""
    pytest.importorskip("pydantic")
    from bp_sdk import dispatch

    def _no_blocking_queue_put(fn) -> bool:  # type: ignore[no-untyped-def]
        """AST: no `await <q>.put(...)` Call (comment/docstring
        mentions of the old pattern must not trip a substring
        check). `put_nowait` is fine."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Await)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr == "put"
            ):
                return False
        return True

    src = inspect.getsource(dispatch.Dispatcher._handle_llm_delta)
    assert "put_nowait(" in src
    assert "except asyncio.QueueFull" in src
    assert _no_blocking_queue_put(dispatch.Dispatcher._handle_llm_delta)

    res_src = inspect.getsource(dispatch.Dispatcher._handle_llm_result)
    # The terminal frame uses the same non-blocking pattern.
    assert "put_nowait(" in res_src
    assert _no_blocking_queue_put(dispatch.Dispatcher._handle_llm_result)


def test_handle_llm_delta_drops_when_consumer_not_draining() -> None:
    """Behavioural: fill the bounded queue, then a further delta is
    dropped (not awaited) and the call returns promptly."""
    pytest.importorskip("pydantic")
    from bp_protocol.frames import LlmDeltaFrame
    from bp_sdk import llm

    async def _run() -> None:
        disp = _bare_dispatcher()
        cid = "corr-1"
        q: asyncio.Queue = asyncio.Queue(maxsize=llm._LLM_STREAM_QUEUE_MAX)
        disp._llm_streams[cid] = q
        # Saturate the queue.
        for _ in range(llm._LLM_STREAM_QUEUE_MAX):
            q.put_nowait(object())
        df = LlmDeltaFrame(
            agent_id="router", trace_id="0" * 32, span_id="0" * 16,
            ref_correlation_id=cid, text="x",
        )
        # Must return promptly (not block on a full unbounded put).
        await asyncio.wait_for(disp._handle_llm_delta(df), timeout=1.0)
        # Still at the cap — the extra delta was dropped.
        assert q.qsize() == llm._LLM_STREAM_QUEUE_MAX

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bare_dispatcher():  # type: ignore[no-untyped-def]
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.dispatch import Dispatcher
    from bp_sdk.transport.inproc import InProcessTransport

    agent = Agent(info=AgentInfo(agent_id="agt_a", description="t"))
    transport = InProcessTransport()
    transport.attach(inbound=asyncio.Queue(), outbound=asyncio.Queue())
    return Dispatcher(agent, transport)
