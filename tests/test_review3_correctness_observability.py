"""Tests for the third-pass review correctness + observability bundle.

  - H-2: `POST /v1/admin/tasks/test` no longer busy-polls the DB. A
    new in-process `state.task_terminal_events` map gives O(1)
    wakeup when the terminal-state writer
    (`complete_task`/`cancel_task`/`fail_task`) sets the event.
    Multi-worker deployments fall through to a 1-s coarse poll.
  - H-5: `frames_total` Prometheus counter no longer carries the
    `agent_id` label — that produced unbounded series cardinality.
  - M-1: `bp_router/api/sessions.py` open/close session now wraps
    the row mutation + audit append in `conn.transaction()` so the
    pair is atomic (DB-H1 family — same shape PR #67 fixed
    elsewhere).
  - M-2: `_handle_llm_request`'s catch-all no longer leaks
    `str(exc)` to the calling agent in the result frame; emits a
    fixed `"internal_error"` message instead. Full traceback
    still logged via `logger.exception` for ops.
  - M-8: `Settings._admin_secret_required_when_mounted` and
    `_no_prompt_logging_in_prod` are `model_validator(mode="after")`
    so they see the WHOLE model, not a partial dict that depends on
    Pydantic's field-by-field ordering.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

# ===========================================================================
# H-2: test_task uses asyncio.Event-based wakeup, not 50 ms busy-poll
# ===========================================================================


def test_h2_app_state_initialises_task_terminal_events_dict() -> None:
    """The lifespan must populate `state.task_terminal_events = {}`
    before any router code runs — a missing attribute would crash
    `_notify_task_terminal` and the test endpoint."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module.lifespan)
    assert "task_terminal_events" in src, (
        "review3-H2 regression: lifespan no longer initialises the "
        "task_terminal_events map"
    )
    assert "task_terminal_events = {}" in src.replace(
        " = {}\n", " = {}"
    ).replace("  ", " ")


def test_h2_notify_task_terminal_helper_exists() -> None:
    """`_notify_task_terminal(state, task_id)` must exist in
    bp_router.tasks and be a no-op when no listener is registered."""
    from bp_router import tasks as tasks_module

    assert hasattr(tasks_module, "_notify_task_terminal")
    fn = tasks_module._notify_task_terminal
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["state", "task_id"]

    # No-op when state has no map at all.
    state_no_map = MagicMock(spec=[])
    fn(state_no_map, "any-task")  # must not raise

    # No-op when no listener for the task.
    state_with_map = MagicMock()
    state_with_map.task_terminal_events = {}
    fn(state_with_map, "missing-task")
    assert state_with_map.task_terminal_events == {}


def test_h2_notify_sets_and_pops_the_event() -> None:
    """When a listener IS registered, the helper must set the
    event AND pop it from the map so it's not re-fired."""
    from bp_router import tasks as tasks_module

    async def _scenario() -> None:
        event = asyncio.Event()
        state = MagicMock()
        state.task_terminal_events = {"task-X": event}
        tasks_module._notify_task_terminal(state, "task-X")
        assert event.is_set()
        assert "task-X" not in state.task_terminal_events

    asyncio.run(_scenario())


def test_h2_complete_task_calls_notify_after_commit() -> None:
    """Source pin: `complete_task` must invoke
    `_notify_task_terminal` AFTER the inner `conn.transaction()`
    block, so a listener that wakes via the event sees the
    committed row when it queries (review item H-2)."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.complete_task)
    assert "_notify_task_terminal" in src, (
        "review3-H2 regression: complete_task no longer notifies"
    )
    # Must appear AFTER the transaction context manager closes.
    notify_idx = src.find("_notify_task_terminal")
    txn_idx = src.find("async with conn.transaction()")
    assert notify_idx > txn_idx > 0, (
        "review3-H2: notify call must follow the inner transaction"
    )


def test_h2_cancel_task_notifies_per_tid() -> None:
    """`cancel_task` works on a list of task ids; the notify must
    fire for each id whose transition succeeded."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.cancel_task)
    assert "_notify_task_terminal(state, tid)" in src


def test_h2_fail_task_notifies_after_commit() -> None:
    """fail_task notifies for `task_id` after its commit."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.fail_task)
    assert "_notify_task_terminal" in src


def test_h2_test_task_uses_event_wait_not_busy_poll() -> None:
    """Source pin: the admin test endpoint MUST use
    `asyncio.wait_for(event.wait(), ...)` and MUST NOT loop with
    `await asyncio.sleep(0.05)`. The 50-ms busy-poll regression is
    exactly what H-2 fixed."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.test_task)
    assert "asyncio.wait_for(" in src
    assert "event.wait()" in src
    # The original 0.05-s busy-poll must be gone.
    assert "asyncio.sleep(0.05)" not in src, (
        "review3-H2 regression: 50-ms busy-poll re-introduced"
    )
    # Listener registration before reading the row.
    assert "task_terminal_events[task_id] = event" in src
    # Cleanup in finally block.
    assert "task_terminal_events.pop(task_id" in src
    assert "review3-H2" not in src or "H-2" in src  # citation present


# ===========================================================================
# H-5: frames_total has no agent_id label
# ===========================================================================


def test_h5_frames_total_label_set_excludes_agent_id() -> None:
    """`router_frames_total` must NOT carry an `agent_id` label.
    Per-agent rates are not actionable from /metrics and the label
    creates unbounded series cardinality (RSS bloat, scrape size
    bloat, query latency)."""
    from bp_router.observability.metrics import frames_total

    label_names = list(frames_total._labelnames)  # type: ignore[attr-defined]
    assert "agent_id" not in label_names, (
        "review3-H5 regression: agent_id back as a label on "
        "router_frames_total — unbounded cardinality"
    )
    # Must still distinguish direction + type — those are bounded.
    assert "direction" in label_names
    assert "type" in label_names


def test_h5_call_sites_pass_only_direction_and_type() -> None:
    """The two `frames_total.labels(...)` call sites must pass
    `direction` + `type` only — no `agent_id` kwarg (which the
    Counter would reject after the label-set change anyway, but
    pin the shape so a regression that re-adds the label set at
    BOTH metric definition AND call sites doesn't slip through)."""
    import re

    from bp_router import dispatch as dispatch_mod
    from bp_router import ws_hub

    pat = re.compile(r"frames_total\.labels\(([^)]*)\)")

    dispatch_calls = pat.findall(inspect.getsource(dispatch_mod))
    ws_calls = pat.findall(inspect.getsource(ws_hub))

    assert dispatch_calls, "dispatch.py no longer increments frames_total"
    assert ws_calls, "ws_hub.py no longer increments frames_total"

    for call_args in dispatch_calls + ws_calls:
        assert "agent_id" not in call_args, (
            f"review3-H5 regression: frames_total label call "
            f"includes agent_id: {call_args!r}"
        )
        assert "direction" in call_args
        assert "type" in call_args


# ===========================================================================
# M-1: sessions.py wraps row + audit in conn.transaction()
# ===========================================================================


def test_m1_open_session_wraps_in_transaction() -> None:
    """`open_session` must wrap the row insert + audit append in
    `async with conn.transaction()` so a partial failure in the
    audit append doesn't leave the session row committed without
    its audit row (DB-H1 family). Same shape PR #67 applied to
    the rest of the API."""
    pytest.importorskip("fastapi")
    from bp_router.api import sessions as sessions_mod

    src = inspect.getsource(sessions_mod.open_session)
    assert "async with conn.transaction()" in src, (
        "review3-M1 regression: open_session no longer atomic"
    )
    assert "open_session" in src
    assert "append_audit_event" in src


def test_m1_close_session_wraps_in_transaction() -> None:
    """Same atomicity contract as open_session for close."""
    pytest.importorskip("fastapi")
    from bp_router.api import sessions as sessions_mod

    src = inspect.getsource(sessions_mod.close_session)
    # The post-cancel close+audit block must be wrapped.
    assert "async with conn.transaction()" in src
    assert "close_session(\n" in src or "close_session(session_id)" in src
    assert "append_audit_event" in src


# ===========================================================================
# M-2: _handle_llm_request catch-all redacts exception text
# ===========================================================================


def test_m2_llm_request_catch_all_emits_fixed_message() -> None:
    """The catch-all `except Exception` in `_run_llm_call` (the
    actual LLM-call body; `_handle_llm_request` is just a thin
    wrapper that schedules it) must emit a FIXED `"internal_error"`
    message in the result frame, NOT `str(exc)`. Exception strings
    often leak host names, file paths, env-var hints, etc., and
    the result frame flows back to the calling agent (in
    production: user-controlled code via `ctx.llm.generate(...)`)."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    # The exception text must NOT be passed to _err_result anywhere
    # in the catch-all branch. The classified branches above (the
    # `LlmUpstreamError` + `LlmStreamInterrupted` paths) DO pass
    # `exc.message` — that's typed-and-redacted by the LLM service.
    # The bare-except branch is the one this finding is about.
    assert '_err_result("internal_error"' in src, (
        "review3-M2 regression: catch-all no longer emits a fixed "
        "internal_error message"
    )
    # The buggy `str(exc)` form must NOT appear in the catch-all.
    # We can't ban it globally because the classified branches
    # legitimately use `exc.message` (typed) — that's `.message`,
    # not `str(exc)`. So banning `str(exc)` is safe here.
    assert "_err_result(str(exc))" not in src, (
        "review3-M2 regression: catch-all is leaking str(exc) "
        "into the result frame"
    )


# ===========================================================================
# M-8: Settings cross-field validators run as model_validator(after)
# ===========================================================================


def test_m8_admin_secret_validator_uses_model_validator_after() -> None:
    """Source pin: the `_admin_secret_required_when_mounted`
    validator must be `@model_validator(mode="after")` not
    `@field_validator(...)`. The field-validator form depended on
    Pydantic's field-by-field ordering (alphabetical by default,
    so `admin_session_secret` runs BEFORE `serve_admin_ui` is
    populated → cross-field check sees None → silently passes)."""
    from bp_router import settings as settings_mod

    src = inspect.getsource(settings_mod)
    # Locate the validator block.
    idx = src.find("_admin_secret_required_when_mounted")
    assert idx > 0
    # The decorator on the line above must be model_validator.
    preceding = src[max(0, idx - 200):idx]
    assert "model_validator(mode=\"after\")" in preceding, (
        "review3-M8 regression: validator reverted to field_validator; "
        "cross-field check is now field-order-dependent"
    )


def test_m8_admin_secret_required_actually_fails_when_mounted_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural: with `serve_admin_ui=true` and no
    `admin_session_secret`, model construction MUST raise. The
    field-validator form silently passed when fields validated
    out of order; the model_validator form catches it
    unconditionally."""
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    # Strip env so test_h2's fields aren't poisoned.
    for var in (
        "ROUTER_SERVE_ADMIN_UI",
        "ROUTER_ADMIN_SESSION_SECRET",
        "ROUTER_JWT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[call-arg]
            db_url="postgres://test/test",
            public_url="https://router.test",
            jwt_secret="x" * 64,
            serve_admin_ui=True,
            admin_session_secret=None,
        )
    assert "ADMIN_SESSION_SECRET" in str(excinfo.value)


def test_m8_admin_secret_passes_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-pin the happy path: a properly-configured Settings
    constructs cleanly."""
    pytest.importorskip("pydantic_settings")
    from pydantic import SecretStr

    from bp_router.settings import Settings

    for var in (
        "ROUTER_SERVE_ADMIN_UI",
        "ROUTER_ADMIN_SESSION_SECRET",
        "ROUTER_JWT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = Settings(  # type: ignore[call-arg]
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=True,
        admin_session_secret=SecretStr("y" * 32),
    )
    assert cfg.serve_admin_ui is True
    assert cfg.admin_session_secret is not None
