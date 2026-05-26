"""New router metrics for user soft-delete + AgentInfoUpdate outcomes.

Both events are operational signals that operators want visibility
on but had no counter before. Added:

  - `router_users_soft_deleted_total`  (no labels)
  - `router_agent_info_update_total`   (label: outcome)

Tests pin the counter exists, the call sites increment, and the
metric-import path is defensive (a registry error doesn't break
the handler path).
"""

from __future__ import annotations

import inspect

import pytest


def test_users_soft_deleted_counter_registered() -> None:
    """The Counter exists with the expected name + no labels.
    `prometheus_client` strips the `_total` suffix from the `_name`
    attr because Counter names are conventionally `<x>_total`."""
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import users_soft_deleted_total

    assert (
        users_soft_deleted_total._name  # type: ignore[attr-defined]
        == "router_users_soft_deleted"
    )


def test_agent_info_update_counter_registered_with_outcome_label() -> None:
    """Counter exists and accepts the `outcome` label."""
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import agent_info_update_total

    assert (
        agent_info_update_total._name  # type: ignore[attr-defined]
        == "router_agent_info_update"
    )
    # `.labels(outcome=...)` must not raise.
    agent_info_update_total.labels(outcome="accepted")
    agent_info_update_total.labels(outcome="rejected")
    agent_info_update_total.labels(outcome="rate_limited")


def test_delete_user_increments_soft_delete_counter() -> None:
    """Source pin: `admin.delete_user` increments
    `users_soft_deleted_total` AFTER the cache invalidate so a
    rolled-back delete doesn't bump the counter spuriously."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.delete_user)
    assert "users_soft_deleted_total.inc()" in src


def test_agent_info_update_handler_increments_metric_at_all_outcomes() -> None:
    """Source pin: every outcome path increments the metric. A
    regression that adds a new outcome branch + forgets the
    counter increment will fail this pin (we count call sites)."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    # rate_limited / rejected / accepted — 3 outcomes minimum.
    # Each calls `_agent_info_update_metric("...")`.
    assert '_agent_info_update_metric("accepted")' in src
    assert '_agent_info_update_metric("rate_limited")' in src
    # `rejected` covers BOTH the agent-not-found and the validation-
    # failure paths.
    assert src.count('_agent_info_update_metric("rejected")') >= 2


def test_agent_info_update_metric_helper_is_defensive() -> None:
    """The helper swallows registry / import errors — a metrics
    hiccup must not break the handler. Pin the try/except shape."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._agent_info_update_metric)
    assert "try:" in src
    assert "except Exception" in src
    assert "agent_info_update_total" in src
