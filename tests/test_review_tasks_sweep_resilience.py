"""R8 MEDIUM: tasks.py sweep/cancel/GC resilience (4 batched).

Fresh-eyes R8 MEDIUM pass over `bp_router/tasks.py` surfaced four
correctness/resilience gaps that all share a theme: a single
failure or a concurrency window silently corrupts a batch or a
fan-out target.

#3 `cancel_task` read the fan-out targets (active/caller agent)
   OUTSIDE the cancel transaction, in autocommit — a concurrent
   `_admit_delegation` flip could change `active_agent_id` between
   the CANCELLED commit and that SELECT, sending the CancelFrame
   to the wrong agent while the real executor kept running.

#4 `_gc_files_once` deleted storage bytes after the ref-count
   transaction committed; a concurrent cross-user dedup
   `insert_file` for the same sha256 in that window got its bytes
   deleted out from under it (404 on next download).

#5 `_sweep_once` / `fail_inflight_for_agent` shared one DB conn
   across N `fail_task` calls; an unexpected error on one row
   propagated out, abandoning every remaining row in the batch
   (and risking an aborted-transaction cascade on the shared conn).

#6 `admit_task`'s failure-path `fail_task` calls could raise
   (pool exhausted / DB blip), skipping the `raise AdmitError` so
   the caller got a generic `internal_error` and the QUEUED row
   was never transitioned — a zombie holding spawn-depth budget.
"""
from __future__ import annotations

import inspect

from bp_router import tasks as tasks_mod

# ===========================================================================
# #3 cancel_task: owner read inside the transaction
# ===========================================================================


def test_cancel_task_reads_owner_inside_transaction() -> None:
    """The `SELECT active_agent_id, caller_agent_id, parent_task_id`
    must sit INSIDE the `async with conn.transaction()` block that
    `task_transition` runs in, so the fan-out plan is a consistent
    snapshot under the same row lock — not a post-commit autocommit
    read a concurrent delegation flip can race."""
    src = inspect.getsource(tasks_mod.cancel_task)

    txn_idx = src.index("async with conn.transaction():")
    owner_select_idx = src.index(
        "SELECT active_agent_id, caller_agent_id, parent_task_id"
    )
    notify_idx = src.index("_notify_task_terminal(state, tid)")

    # The owner SELECT must come AFTER the transaction opens...
    assert txn_idx < owner_select_idx
    # ...and BEFORE `_notify_task_terminal` (which runs post-block).
    assert owner_select_idx < notify_idx

    # Structural pin: the owner select must be more-indented than the
    # `_notify_task_terminal` call (i.e. nested in the with-block).
    lines = src.splitlines()
    owner_line = next(
        ln for ln in lines
        if "SELECT active_agent_id, caller_agent_id" in ln
    )
    notify_line = next(
        ln for ln in lines if "_notify_task_terminal(state, tid)" in ln
    )
    owner_indent = len(owner_line) - len(owner_line.lstrip())
    notify_indent = len(notify_line) - len(notify_line.lstrip())
    assert owner_indent > notify_indent, (
        "owner SELECT must be nested deeper than _notify_task_terminal "
        "— i.e. inside the conn.transaction() block"
    )


# ===========================================================================
# #4 _gc_files_once: re-check refs before storage delete
# ===========================================================================


def test_gc_files_rechecks_refs_before_storage_delete() -> None:
    src = inspect.getsource(tasks_mod._gc_files_once)

    delete_loop = src.index("for file_id, sha256 in storage_to_delete")
    recheck = src.index("count_other_file_refs", delete_loop)
    storage_delete = src.index("state.file_store.delete(sha256)")

    # The re-check must come AFTER the loop starts and BEFORE the
    # storage delete in that iteration.
    assert delete_loop < recheck < storage_delete
    # On a positive re-check the delete is skipped (continue), logged.
    assert "file_gc_storage_delete_skipped_reref" in src
    # A failed re-check is conservative: skip the delete, not delete.
    assert "file_gc_recheck_failed_skipping_delete" in src
    skip_log = src.index("file_gc_recheck_failed_skipping_delete")
    # The conservative-skip path must `continue` (not fall through
    # to the delete) — the `continue` follows the skip log.
    assert "continue" in src[skip_log:storage_delete]


def test_gc_files_recheck_conn_released_before_storage_delete() -> None:
    """The re-check connection is held only for the COUNT and
    released (its `async with` block closes) BEFORE the slow
    `file_store.delete` — preserving the M4 'no DB conn across
    storage I/O' invariant."""
    src = inspect.getsource(tasks_mod._gc_files_once)
    # Within the storage-delete loop body, the recheck acquire and
    # its transaction must close before file_store.delete is called.
    loop_body = src[src.index("for file_id, sha256 in storage_to_delete"):]
    recheck_acq = loop_body.index("async with pool.acquire()")
    storage_delete = loop_body.index("state.file_store.delete(sha256)")
    assert recheck_acq < storage_delete
    # The delete sits at the loop-body indent, NOT nested under the
    # `async with pool.acquire()` re-check block.
    lines = loop_body.splitlines()
    acq_line = next(ln for ln in lines if "async with pool.acquire()" in ln)
    del_line = next(
        ln for ln in lines if "state.file_store.delete(sha256)" in ln
    )
    acq_indent = len(acq_line) - len(acq_line.lstrip())
    del_indent = len(del_line) - len(del_line.lstrip())
    assert del_indent <= acq_indent, (
        "file_store.delete must NOT be nested inside the re-check "
        "pool.acquire() block (conn must be released first)"
    )


# ===========================================================================
# #5 sweep / fail_inflight: per-row isolation
# ===========================================================================


def _stmt_lines(src: str) -> list[tuple[int, str]]:
    """Return (indent, stripped) for each non-blank, non-comment
    line — so substring matches in comments/docstrings can't poison
    structural assertions."""
    out: list[tuple[int, str]] = []
    for raw in src.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip()), stripped))
    return out


def _has_isolation_block(src: str, loop_stmt: str, fail_call: str) -> bool:
    """True iff there's a `try:` ... `except Exception:` ...
    `continue` (real statements, not comment words) nested inside
    the `loop_stmt` for-loop, wrapping the `fail_call`."""
    lines = _stmt_lines(src)
    loop_i = next(
        i for i, (_, s) in enumerate(lines) if s.startswith(loop_stmt)
    )
    loop_indent = lines[loop_i][0]
    saw_try = saw_except = saw_continue = saw_fail = False
    for indent, s in lines[loop_i + 1:]:
        if indent <= loop_indent:
            break  # left the loop body
        if s == "try:":
            saw_try = True
        elif s.startswith("except Exception:"):
            saw_except = True
        elif s == "continue":
            saw_continue = True
        elif fail_call in s:
            saw_fail = True
    return saw_try and saw_except and saw_continue and saw_fail


def test_sweep_once_isolates_poison_row() -> None:
    src = inspect.getsource(tasks_mod._sweep_once)
    assert "timeout_sweep_row_failed" in src
    assert _has_isolation_block(src, "for row in rows:", "fail_task("), (
        "_sweep_once must wrap the per-row fail_task in "
        "try/except Exception/continue inside the for loop"
    )


def test_fail_inflight_isolates_poison_row() -> None:
    src = inspect.getsource(tasks_mod.fail_inflight_for_agent)
    assert "fail_inflight_row_failed" in src
    assert _has_isolation_block(src, "for r in rows:", "fail_task("), (
        "fail_inflight_for_agent must wrap the per-row fail_task in "
        "try/except Exception/continue inside the for loop"
    )
    # Still passes the held conn (M4 invariant preserved).
    assert "conn=conn" in src
    assert _count_acquire(src) == 1


def _count_acquire(src: str) -> int:
    return src.count("async with pool.acquire()")


# ===========================================================================
# #6 admit_task: AdmitError always propagates
# ===========================================================================


def test_admit_task_safe_fail_helper_swallows_fail_task_errors() -> None:
    """The failure-path `fail_task` calls must go through a helper
    that swallows (logs) a `fail_task` error so the subsequent
    `raise AdmitError` ALWAYS runs — otherwise the caller gets a
    generic internal_error and the QUEUED row zombies."""
    src = inspect.getsource(tasks_mod.admit_task)

    assert "_safe_fail" in src
    assert "admit_failure_fail_task_errored" in src

    # All three failure branches must call _safe_fail then raise
    # AdmitError (not bare fail_task).
    # Count: helper def + 3 call sites = 4 occurrences minimum.
    assert src.count("_safe_fail(") >= 4

    # The helper must wrap fail_task in try/except.
    helper_start = src.index("async def _safe_fail")
    helper_region = src[helper_start:helper_start + 1200]
    assert "await fail_task(" in helper_region
    assert "except Exception:" in helper_region

    # Each branch: _safe_fail call is followed by `raise AdmitError`.
    for branch_marker in (
        '"agent_disconnected", "destination agent has no live socket"',
        '"ack_timeout", "destination agent did not ack in time"',
    ):
        b = src.index(branch_marker)
        # The _safe_fail call precedes the AdmitError raise in the
        # same except handler.
        preceding = src[:b]
        assert preceding.rfind("_safe_fail(") > preceding.rfind(
            "await deliver_frame("
        ), f"branch {branch_marker!r} must call _safe_fail before raising"


def test_admit_task_no_bare_fail_task_in_failure_paths() -> None:
    """Regression guard: the failure paths must NOT call
    `fail_task` directly (that's the bug — a raising fail_task
    skips the AdmitError). The ONLY `await fail_task(` in
    `admit_task` must be the one inside the `_safe_fail` helper."""
    src = inspect.getsource(tasks_mod.admit_task)

    # Exactly one `await fail_task(` call site in the whole function.
    assert src.count("await fail_task(") == 1, (
        "admit_task must call fail_task exactly once (inside "
        "_safe_fail); failure branches use _safe_fail, not bare "
        "fail_task"
    )
    # And that single call site is lexically inside the _safe_fail
    # helper def (between `async def _safe_fail` and the next
    # top-of-helper-indent statement, i.e. `try:` for deliver_frame).
    helper_start = src.index("async def _safe_fail")
    fail_call = src.index("await fail_task(")
    deliver_try = src.index("ack = await deliver_frame(")
    assert helper_start < fail_call < deliver_try, (
        "the sole fail_task call must sit inside the _safe_fail "
        "helper, before the deliver_frame dispatch"
    )
