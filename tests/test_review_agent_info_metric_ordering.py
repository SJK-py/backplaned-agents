"""`_agent_info_update_metric("accepted")` increments after the DB
transaction commits, BEFORE the catalog broadcast + Ack send.

R4 second-pass review found the original PR #137 ordering placed
the increment AFTER `_ack(...accepted=True)`. A transient
transport failure between transaction commit and Ack send would
lose the counter increment despite the DB change being durable.
Operators graphing the metric would see undercounts proportional
to the WS disconnect rate.

Source-pin only — verifies the increment-call appears before
the broadcast + ack call in the function source. The functional
behavior (Counter actually fired) is covered by
`test_review_observability_metrics.py`.
"""

from __future__ import annotations

import inspect

import pytest


def test_metric_increment_runs_immediately_after_commit() -> None:
    """The success-path metric call MUST appear AFTER the
    `with conn.transaction():` block exits (commit) but BEFORE
    `push_catalog_update_to_all` and `_ack`. Pin via line
    ordering."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    lines = src.splitlines()

    # Find the success-path metric call (the "accepted" branch).
    accepted_idx = next(
        (i for i, line in enumerate(lines)
         if '_agent_info_update_metric("accepted")' in line),
        -1,
    )
    broadcast_idx = next(
        (i for i, line in enumerate(lines)
         if "push_catalog_update_to_all(state)" in line),
        -1,
    )
    ack_idx = next(
        (i for i, line in enumerate(lines)
         if "_ack(entry, frame, accepted=True)" in line),
        -1,
    )

    assert accepted_idx >= 0
    assert broadcast_idx >= 0
    assert ack_idx >= 0
    # Metric BEFORE broadcast.
    assert accepted_idx < broadcast_idx, (
        "_agent_info_update_metric('accepted') must run BEFORE "
        "push_catalog_update_to_all — otherwise a catalog-broadcast "
        "failure (Phase 4 helper logs internally) would lose the "
        "counter increment despite the DB commit being durable."
    )
    # And metric BEFORE ack.
    assert accepted_idx < ack_idx, (
        "_agent_info_update_metric('accepted') must run BEFORE _ack "
        "— otherwise a transient WS write failure between commit and "
        "ack would lose the counter increment despite the DB change."
    )


def test_metric_increment_still_runs_on_every_outcome_branch() -> None:
    """Regression guard: PR #137 added counter increments at four
    outcome branches (rate_limited, two `rejected` paths, accepted).
    R4 reordered the `accepted` branch — confirm the other branches
    still increment too. Counts the helper-call sites in the
    function source."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert '_agent_info_update_metric("rate_limited")' in src
    assert '_agent_info_update_metric("accepted")' in src
    # rejected covers BOTH agent_not_found and validation_error.
    assert src.count('_agent_info_update_metric("rejected")') >= 2
