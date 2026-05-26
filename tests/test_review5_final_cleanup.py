"""Tests for the fifth-pass review final-cleanup fixes (M-1, M-2).

  - M-1: `_handle_llm_request`'s `_cleanup` callback uses identity
    (`current is _t`) before popping, so the prior task's queued
    cleanup can't accidentally remove the new task we just
    registered. Without this, the M-5 (review 4) "done-but-not-
    yet-cleaned-up" branch produced an orphaned new task: not in
    `entry.llm_tasks`, so `_on_disconnect` couldn't cancel it,
    and `Cancel{ref_correlation_id=corr}` couldn't reach it.
  - M-2: SDK `Dispatcher`'s handler catch-all emits a fixed
    `"internal_error"` message instead of `str(exc)`. Symmetric
    to the router-side review3-M2 fix; the SDK side never
    received the same treatment.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

# ===========================================================================
# M-1: _cleanup uses identity check, not blind pop
# ===========================================================================


def test_m1_cleanup_callback_uses_identity_check_in_source() -> None:
    """Source pin: `_cleanup` MUST test `entry.llm_tasks.get(corr)
    is _t` before popping. A regression that drops the identity
    check (back to `entry.llm_tasks.pop(corr, None)`) re-opens the
    orphan-task race that the review 4 M-5 fix's
    'done-but-not-yet-cleaned-up' branch creates."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_llm_request)
    # The closure body must check identity before popping.
    assert "current is _t" in src, (
        "review5-M1 regression: _cleanup no longer does identity "
        "check before popping — orphan-task race re-opened"
    )
    # And the get() must reference the captured correlation_id.
    assert "entry.llm_tasks.get(frame.correlation_id)" in src, (
        "review5-M1: identity check must read from the SAME dict "
        "key the cleanup is supposed to manage"
    )


def test_m1_simulated_cleanup_does_not_pop_replacement_task() -> None:
    """Behavioural simulation of the cleanup-vs-replacement race.

    Reproduces the closure shape from `_handle_llm_request`:
      1. Register task_A under correlation_id "X".
      2. Replace `entry.llm_tasks["X"]` with task_B (simulating
         what happens when the handler hits the "done-but-not-
         yet-cleaned-up" branch and creates a new task).
      3. Invoke cleanup_A (simulating the queued callback running).
      4. Verify task_B is STILL at "X" — cleanup_A's identity
         check should see `current is task_B`, not task_A, and
         skip the pop.
    """
    entry = MagicMock()
    entry.llm_tasks = {}
    correlation_id = "cid-X"

    # Two distinct sentinel tasks.
    task_A = object()
    task_B = object()

    # Recreate the cleanup closure shape from the M-1 fix verbatim.
    def _cleanup(_t: Any) -> None:
        current = entry.llm_tasks.get(correlation_id)
        if current is _t:
            entry.llm_tasks.pop(correlation_id, None)

    # Step 1: task_A registered under "X".
    entry.llm_tasks[correlation_id] = task_A

    # Step 2: The "done-but-not-yet-cleaned-up" branch fires; the
    # handler replaces task_A with a freshly-created task_B at "X".
    entry.llm_tasks[correlation_id] = task_B

    # Step 3: task_A's queued cleanup callback runs.
    _cleanup(task_A)

    # Step 4: task_B MUST still be at "X".
    assert entry.llm_tasks[correlation_id] is task_B, (
        "review5-M1 regression: prior task's cleanup popped the "
        "live entry that pointed at the replacement task"
    )


def test_m1_simulated_cleanup_still_pops_when_identity_matches() -> None:
    """Sanity-pin the happy path: when the dict still points at
    the cleanup's own task (the typical case where the previous
    task ran to completion, no replacement occurred), the pop
    still fires. Otherwise the dict would grow without bound."""
    entry = MagicMock()
    entry.llm_tasks = {}
    correlation_id = "cid-Y"
    task = object()

    def _cleanup(_t: Any) -> None:
        current = entry.llm_tasks.get(correlation_id)
        if current is _t:
            entry.llm_tasks.pop(correlation_id, None)

    entry.llm_tasks[correlation_id] = task
    _cleanup(task)
    assert correlation_id not in entry.llm_tasks, (
        "review5-M1 over-correction: identity-matching cleanup "
        "should still pop"
    )


def test_m1_simulated_cleanup_no_op_when_key_already_gone() -> None:
    """If the key was already popped (e.g. by an earlier identity-
    matching cleanup), the second cleanup must be a no-op, not a
    KeyError."""
    entry = MagicMock()
    entry.llm_tasks = {}
    correlation_id = "cid-Z"
    task = object()

    def _cleanup(_t: Any) -> None:
        current = entry.llm_tasks.get(correlation_id)
        if current is _t:
            entry.llm_tasks.pop(correlation_id, None)

    # Key never existed.
    _cleanup(task)  # must not raise
    assert correlation_id not in entry.llm_tasks


def test_m1_done_branch_comment_explains_identity_check() -> None:
    """The 'done-but-not-yet-cleaned-up' branch comment in
    `_handle_llm_request` MUST reference the identity-check
    behaviour so a future maintainer reading the branch
    understands why the pre-existing pending callback won't
    clobber the new task."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_llm_request)
    # Find the done-branch fall-through region.
    done_idx = src.find("Done-but-not-yet-cleaned-up")
    assert done_idx > 0
    # Within ~500 chars of the comment, the identity-check
    # rationale must appear (pin the educational invariant).
    region = src[done_idx:done_idx + 500]
    assert "identity" in region.lower() or "is _t" in region


# ===========================================================================
# M-2: SDK handler catch-all emits "internal_error", not str(exc)
# ===========================================================================


def test_m2_sdk_handler_catch_all_emits_fixed_internal_error_message() -> None:
    """Source pin: the `except Exception` branch in the SDK
    `Dispatcher`'s handler invocation MUST emit a fixed
    `"internal_error"` string in `error["message"]`, NOT
    `str(exc)`. Mirrors the router-side review3-M2 fix; the
    symmetric SDK-side path was previously missed."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch as sdk_dispatch

    src = inspect.getsource(sdk_dispatch)
    # The fixed redacted message must be present.
    assert '"message": "internal_error"' in src, (
        "review5-M2 regression: SDK handler catch-all no longer "
        "emits a fixed internal_error message"
    )
    # The buggy `str(exc)` form must NOT appear in the catch-all
    # branch.  The HandlerError / CancellationError branches
    # legitimately use `str(exc)` since those are typed and the
    # message is part of the contract — we're only banning it in
    # the unclassified `except Exception` block. We can't perfectly
    # scope this with a simple substring check, but we CAN verify
    # the fixed string is present (which means the branch was
    # converted) — the L-2-style cross-cutting test below covers
    # the negative.


def test_m2_sdk_handler_catch_all_does_not_format_exc_into_internal_error() -> None:
    """Cross-cutting pin: locate the `'code': 'InternalError'` line
    and verify the surrounding error dict does NOT compose
    `str(exc)`. Catches a regression that re-formats the exception
    into the message field."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch as sdk_dispatch

    src = inspect.getsource(sdk_dispatch)
    idx = src.find('"code": "InternalError"')
    assert idx > 0, (
        "review5-M2: handler catch-all branch must still emit "
        "the InternalError code; missing entirely?"
    )
    # Look at the dict-literal region around this line.
    region = src[max(0, idx - 100):idx + 400]
    assert '"message": "internal_error"' in region, (
        "review5-M2: InternalError branch must emit fixed "
        "'internal_error' message"
    )
    # The bare `str(exc)` form must NOT appear in this region.
    assert '"message": str(exc)' not in region, (
        "review5-M2 regression: SDK handler catch-all still "
        "leaks str(exc) — exception strings can carry host "
        "names, file paths, env-var hints, etc."
    )


def test_m2_sdk_handler_typed_exception_branches_unchanged() -> None:
    """The CancellationError / HandlerError branches above the
    catch-all DO use `str(exc)` legitimately — those are typed
    SDK errors and the message is part of the contract. Pin so
    the M-2 fix doesn't accidentally over-correct and break
    typed-error semantics."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch as sdk_dispatch

    src = inspect.getsource(sdk_dispatch)
    # CancellationError branch keeps its typed message.
    assert '"code": "cancelled", "message": str(exc)' in src, (
        "review5-M2 over-correction: CancellationError branch "
        "lost its typed message — those exceptions are part of "
        "the SDK contract"
    )
    # HandlerError branch keeps its typed message.
    assert (
        '"code": type(exc).__name__, "message": str(exc)' in src
    ), (
        "review5-M2 over-correction: HandlerError branch lost "
        "its typed message"
    )


def test_m2_router_and_sdk_now_have_symmetric_exception_redaction() -> None:
    """Cross-cutting pin: BOTH the router-side
    `_run_llm_call` catch-all AND the SDK-side `Dispatcher`
    handler catch-all emit the fixed `"internal_error"` string.
    A future PR that fixes one without the other re-opens the
    asymmetry that has now been the source of TWO findings
    (review3-M2 + review5-M2)."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch as router_dispatch
    from bp_sdk import dispatch as sdk_dispatch

    router_src = inspect.getsource(router_dispatch)
    sdk_src = inspect.getsource(sdk_dispatch)

    # Both must emit the fixed redacted message.
    assert '_err_result("internal_error"' in router_src, (
        "review5-M2 cross-cutting: router-side review3-M2 fix "
        "appears to have regressed"
    )
    assert '"message": "internal_error"' in sdk_src, (
        "review5-M2 cross-cutting: SDK-side fix missing"
    )
