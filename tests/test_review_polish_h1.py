"""Second-pass low-priority polish — H1 (safe correctness + docs).

- QUEUED→SUCCEEDED is now a legal transition, so a Result(succeeded) that
  lands while the task is still QUEUED (a very fast executor) isn't dropped as
  a "duplicate" and stranded until the deadline sweep.
- fail_task cascades descendant cancellations with a reason that names the
  parent's real terminal cause (`parent_timed_out` vs `parent_failed`).
- The suite `preset_embedding` column default matches the code/settings
  (`default_embedding`, an embedding-capable preset — `default` is chat-only).
- docs: the stale `router_result_attachments_dropped_total` metric is removed.
"""

from __future__ import annotations

import inspect
import pathlib

from bp_protocol.types import TaskState
from bp_router import state, tasks

_REPO = pathlib.Path(__file__).resolve().parent.parent


# --- QUEUED→SUCCEEDED -------------------------------------------------------


def test_queued_to_succeeded_now_allowed() -> None:
    assert state.is_allowed(TaskState.QUEUED, TaskState.SUCCEEDED)
    assert TaskState.SUCCEEDED in state.allowed_transitions(TaskState.QUEUED)


def test_terminal_states_remain_dead_ends() -> None:
    for t in (
        TaskState.SUCCEEDED, TaskState.FAILED,
        TaskState.CANCELLED, TaskState.TIMED_OUT,
    ):
        assert state.allowed_transitions(t) == frozenset()


# --- F4: timeout cascade reason ---------------------------------------------


def test_fail_task_cascade_reason_tracks_terminal_state() -> None:
    src = inspect.getsource(tasks.fail_task)
    assert "cascade_reason = (" in src
    assert "terminal_state == TaskState.TIMED_OUT" in src
    assert '"parent_timed_out"' in src
    # The three cascade emit sites use the computed reason; only the
    # else-branch literal "parent_failed" remains.
    assert src.count('"parent_failed"') == 1
    assert "reason=cascade_reason" in src
    assert '"message": cascade_reason' in src


# --- preset_embedding default alignment -------------------------------------


def test_preset_embedding_default_aligned() -> None:
    mig = (
        _REPO / "bp_agents/migrations/versions/0001_suite_initial.py"
    ).read_text()
    assert "preset_embedding         text NOT NULL DEFAULT 'default_embedding'" in mig
    from bp_agents.db import queries
    sig = inspect.signature(queries.create_user_config)
    assert sig.parameters["preset_embedding"].default == "default_embedding"
    from bp_agents.settings import SuiteSettings
    assert (
        SuiteSettings.model_fields["default_preset_embedding"].default
        == "default_embedding"
    )


# --- doc drift --------------------------------------------------------------


def test_observability_doc_drops_stale_metric() -> None:
    doc = (_REPO / "docs/backplaned/observability.md").read_text()
    assert "router_result_attachments_dropped_total" not in doc
