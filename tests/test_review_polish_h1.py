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


# --- embedding preset is operator config, never a user slot -----------------


def test_embedding_preset_is_operator_config_only() -> None:
    """The embedding model must stay an explicit, operator-set preset.

    Making it user-selectable silently invalidates every vector already
    written to that user's LanceDB — a wrong-answer bug with no error and no
    migration path ([docs/design/router-resolved-preset-slots.md] §12). So it
    is absent from the slot taxonomy and absent from `user_config`."""
    from bp_agents import slots
    from bp_agents.db.models import UserConfigRow
    from bp_agents.settings import SuiteSettings

    assert "embedding" not in slots.SLOTS
    assert not [f for f in UserConfigRow.model_fields if f.startswith("preset")]
    assert (
        SuiteSettings.model_fields["default_preset_embedding"].default
        == "default_embedding"
    )


# --- doc drift --------------------------------------------------------------


def test_observability_doc_drops_stale_metric() -> None:
    doc = (_REPO / "docs/backplaned/observability.md").read_text()
    assert "router_result_attachments_dropped_total" not in doc
