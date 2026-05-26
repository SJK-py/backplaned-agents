"""`cancel_task` aborts router-side LLM Tasks for the cancelled task.

R8 fourth-pass review (HIGH, tasks.py): pre-R8 `cancel_task` sent
a `CancelFrame` to the active_agent but did NOT abort the
router-side `asyncio.Task` running the provider call for that
task. The provider streaming continued until the call naturally
finished — tokens kept being billed for work nobody would ever
use. The `CancelFrame`-to-agent path only cancels at the agent's
level; the router's own LLM Task is independent.

R8 fix (HIGH):
  - `dispatch._handle_llm_request` stamps the originating
    `task_id` on the Task via `setattr(task, "_bp_task_id", ...)`.
  - `cancel_task` calls the helper after the DB sweep, before
    the CancelFrame fanout.

R8 MEDIUM perf addendum: the helper no longer scans every live
socket × every in-flight LLM task (O(M·K) on every cancel).
`dispatch._handle_llm_request` now also indexes the Task into
`state.llm_tasks_by_task_id[task_id]` (a `dict[str, set[Task]]`,
pruned by the Task's done-callback), and
`_abort_router_side_llm_tasks` does an O(1) lookup per cancelled
task_id against that index. These tests exercise the index
contract.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest


def test_dispatch_stamps_task_id_on_llm_task() -> None:
    """Source pin: `_handle_llm_request` sets the
    `_bp_task_id` attribute on the LLM asyncio.Task. Without it,
    the cancel helper can't find which router-side Tasks belong
    to which task_id."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_llm_request)
    assert 'setattr(task, "_bp_task_id"' in src


def test_abort_helper_cancels_matching_tasks() -> None:
    """Functional pin: build a state whose `llm_tasks_by_task_id`
    index maps task_A → {t1, t3}, task_B → {t2}, task_C → {t4}.
    Aborting task_A cancels only t1 and t3."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_module

    async def _run() -> None:
        async def _idle() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass

        t1 = asyncio.create_task(_idle())
        t2 = asyncio.create_task(_idle())
        t3 = asyncio.create_task(_idle())
        t4 = asyncio.create_task(_idle())

        state = MagicMock()
        # The O(1) index dispatch._handle_llm_request now populates.
        state.llm_tasks_by_task_id = {
            "task_A": {t1, t3},
            "task_B": {t2},
            "task_C": {t4},
        }

        # Cancel task_A — should cancel t1 and t3.
        n = tasks_module._abort_router_side_llm_tasks(state, {"task_A"})
        assert n == 2

        # Let the cancels propagate.
        await asyncio.sleep(0)
        assert t1.cancelled() or t1.done()
        assert t3.cancelled() or t3.done()
        # task_B and task_C untouched.
        assert not t2.cancelled()
        assert not t4.cancelled()

        # Cleanup.
        for t in (t1, t2, t3, t4):
            if not t.done():
                t.cancel()
        await asyncio.gather(t1, t2, t3, t4, return_exceptions=True)

    asyncio.run(_run())


def test_abort_helper_handles_empty_set() -> None:
    """Defensive: empty task_ids → no work, returns 0."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_module
    state = MagicMock()
    state.llm_tasks_by_task_id = {}
    assert tasks_module._abort_router_side_llm_tasks(state, set()) == 0


def test_abort_helper_no_index_returns_zero() -> None:
    """Defensive: a state without the index (older test harness /
    partial init) returns 0 rather than raising."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_module
    state = MagicMock()
    state.llm_tasks_by_task_id = None
    assert tasks_module._abort_router_side_llm_tasks(
        state, {"task_X"}
    ) == 0


def test_abort_helper_skips_done_tasks() -> None:
    """A Task that's already `done()` shouldn't be cancelled
    again — the count returned reflects only fresh cancels."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_module

    async def _run() -> None:
        async def _quick() -> None:
            pass
        t = asyncio.create_task(_quick())
        await t  # let it finish

        state = MagicMock()
        state.llm_tasks_by_task_id = {"task_X": {t}}

        assert tasks_module._abort_router_side_llm_tasks(
            state, {"task_X"}
        ) == 0

    asyncio.run(_run())


def test_cancel_task_calls_abort_helper() -> None:
    """Source pin: `cancel_task` calls
    `_abort_router_side_llm_tasks` for the set of cancelled
    task_ids BEFORE the CancelFrame fanout. The order matters —
    aborting before fanout means the router-side Task isn't
    still consuming tokens while we send the CancelFrame."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.cancel_task)
    assert "_abort_router_side_llm_tasks" in src
    abort_idx = src.index("_abort_router_side_llm_tasks")
    # Fanout includes the CancelFrame send.
    cancel_send_idx = src.index("CancelFrame(")
    assert abort_idx < cancel_send_idx
