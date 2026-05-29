"""Result/Cancel fan-out must tolerate a saturated recipient outbox.

Pre-release blocker: `deliver_frame(..., await_ack=False)` raises
`asyncio.QueueFull` when the recipient's outbox is full (it has already
recorded `deliver_frame_dropped_total` + logged `deliver_frame_queue_full`).
The terminal/cancel fan-out sites in `tasks.py` caught ONLY
`AgentNotConnected`, so a QueueFull:

  * in `complete_task` propagated to `_handle_result`'s `except Exception`,
    which false-acked the reporting agent `internal_error` even though the
    task's terminal state was already committed; and
  * in `cancel_task` / `fail_task`'s cascade loops aborted fan-out to ALL
    remaining descendants — re-opening the orphan / token-burn leak the
    cascade exists to prevent (siblings never get their synthetic CANCELLED
    or their CancelFrame).

Fix: those fan-out handlers now catch `(AgentNotConnected, asyncio.QueueFull)`
— drop-and-continue (the drop is already metered/logged in `deliver_frame`).
The admit-path delivery (`as exc` sites) is intentionally NOT broadened: a
full destination outbox there must still fail the admit.

Source-pin style, matching `test_review_tasks_sweep_resilience.py` (the DB
round-trip runs in the integration suite).
"""

from __future__ import annotations

import inspect

from bp_router import tasks as tasks_mod


def _stmt_lines(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for raw in src.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip()), s))
    return out


def _fanout_handlers_cover_queuefull(src: str) -> tuple[int, int]:
    """Return (await_ack_false_sites, queuefull_handlers) within `src`.

    Counts `await_ack=False` `deliver_frame` calls and the
    `except (AgentNotConnected, asyncio.QueueFull):` handlers that guard
    them. For these functions the two counts should match.
    """
    # Count over statement lines only — `await_ack=False` also appears in a
    # comment in fail_task, which would inflate a naive src.count().
    stmts = [s for _, s in _stmt_lines(src)]
    deliver_sites = sum(s.count("await_ack=False") for s in stmts)
    broadened = sum(
        s.count("except (AgentNotConnected, asyncio.QueueFull):") for s in stmts
    )
    return deliver_sites, broadened


def test_complete_task_caller_fanout_tolerates_queue_full() -> None:
    src = inspect.getsource(tasks_mod.complete_task)
    sites, broadened = _fanout_handlers_cover_queuefull(src)
    assert sites == 1, f"expected one await_ack=False fan-out, got {sites}"
    assert broadened == 1, (
        "complete_task's caller fan-out must catch asyncio.QueueFull so a "
        "saturated caller outbox doesn't become a false internal_error ack "
        "for an already-committed terminal"
    )
    # And it must NOT still narrow-catch only AgentNotConnected there.
    assert "except AgentNotConnected:" not in src


def test_cancel_task_fanout_tolerates_queue_full() -> None:
    src = inspect.getsource(tasks_mod.cancel_task)
    sites, broadened = _fanout_handlers_cover_queuefull(src)
    # Two fan-out sites per descendant: synthetic Result + CancelFrame.
    assert sites == 2
    assert broadened == 2, (
        "both cancel_task fan-out sites must catch QueueFull so one "
        "saturated recipient can't abort the cancel of sibling descendants"
    )
    assert "except AgentNotConnected:" not in src


def test_fail_task_cascade_tolerates_queue_full() -> None:
    src = inspect.getsource(tasks_mod.fail_task)
    sites, broadened = _fanout_handlers_cover_queuefull(src)
    # Cascade: synthetic Result + CancelFrame per child, plus the parent
    # FAILED fan-out = 3 sites.
    assert sites == 3
    assert broadened == 3, (
        "fail_task's cascade + parent fan-out must catch QueueFull so a "
        "saturated recipient can't abandon the rest of the failed subtree"
    )
    assert "except AgentNotConnected:" not in src


def test_admit_path_delivery_not_broadened() -> None:
    """Regression guard: the admit-path delivery (where a full destination
    outbox SHOULD fail the spawn) must keep catching only AgentNotConnected,
    not silently swallow QueueFull."""
    src = inspect.getsource(tasks_mod.admit_task)
    assert "except AgentNotConnected as exc:" in src
    # admit_task must not have been swept into the broadened form.
    assert "asyncio.QueueFull" not in src
