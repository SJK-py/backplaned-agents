"""Two SDK correlation doc clarifications from the third-pass review.

  1. `PendingMap.start_reaper` documents that reaper-side
     timeouts are silently swallowed when the awaiter wrapped its
     wait in `asyncio.shield` (as `SpawnStream` does). The
     behaviour is BY DESIGN; pre-R6 the docstring was silent on
     the interaction.

  2. `Dispatcher.register_for_task` documents the brief race
     window when `pmap.register` returns an already-resolved
     future (early-resolve buffer hit) and `_untrack` schedules
     via `call_soon`. The race is benign — `pmap.reject`'s `cid
     in self._pending` guard short-circuits — but a future
     refactor removing the guard would expose it.

Plus a minor code change: the reaper logs `correlation_reaper_cancelled`
on CancelledError so a stray cancellation (bug elsewhere in the
supervisor) leaves an operator-visible trail.
"""

from __future__ import annotations

import inspect

import pytest


def test_start_reaper_docstring_mentions_shielded_futures() -> None:
    pytest.importorskip("pydantic")
    from bp_sdk.correlation import PendingMap

    doc = PendingMap.start_reaper.__doc__ or ""
    assert "asyncio.shield" in doc
    # And explains the consequence + intent.
    assert "silently swallowed" in doc.lower() or "swallow" in doc.lower()


def test_reap_logs_cancellation() -> None:
    """Source pin: the reaper's CancelledError branch logs before
    returning. Pre-R6 it returned silently."""
    pytest.importorskip("pydantic")
    from bp_sdk import correlation

    src = inspect.getsource(correlation.PendingMap._reap)
    assert "correlation_reaper_cancelled" in src
    assert "logger.debug" in src


def test_register_for_task_docstring_explains_resolve_race() -> None:
    pytest.importorskip("pydantic")
    from bp_sdk.dispatch import Dispatcher

    doc = Dispatcher.register_for_task.__doc__ or ""
    assert "early-resolve buffer" in doc.lower()
    assert "benign" in doc.lower()
    # Cross-reference to the guard that makes it safe.
    assert "self._pending" in doc


def test_correlation_module_has_logger() -> None:
    """The reaper's new log line requires a module-level logger."""
    pytest.importorskip("pydantic")
    from bp_sdk import correlation

    assert hasattr(correlation, "logger")
    assert correlation.logger.name == "bp_sdk.correlation"
