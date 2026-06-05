"""bp_router metrics snapshot — `snapshot_summary()` powers the admin
dashboard's metric cards via `GET /v1/admin/metrics/summary`.

Counters are a global singleton, so we assert on DELTAS (snapshot before,
increment, snapshot after) rather than absolute values — robust to whatever
other tests in the session have already recorded.
"""

from __future__ import annotations


def test_snapshot_summary_reflects_increments() -> None:
    from bp_router.observability import metrics as m

    before = m.snapshot_summary()
    m.llm_errors_total.labels(
        provider="anthropic", error_code="upstream_timeout"
    ).inc()
    m.llm_calls_total.labels(
        model="balanced", provider="anthropic", status="stop"
    ).inc()
    after = m.snapshot_summary()

    assert after["llm"]["errors_total"] == before["llm"]["errors_total"] + 1
    assert (
        after["llm"]["errors_by_code"].get("upstream_timeout", 0)
        == before["llm"]["errors_by_code"].get("upstream_timeout", 0) + 1
    )
    assert (
        after["llm"]["errors_by_provider"].get("anthropic", 0)
        == before["llm"]["errors_by_provider"].get("anthropic", 0) + 1
    )
    assert after["llm"]["calls_total"] == before["llm"]["calls_total"] + 1


def test_snapshot_summary_shape() -> None:
    """The curated keys the admin model + dashboard cards depend on."""
    from bp_router.observability import metrics as m

    s = m.snapshot_summary()
    assert set(s) == {"llm", "tasks", "infra"}
    assert {
        "calls_total", "errors_total", "errors_by_code",
        "fallback_chain_exhausted_total", "tokens_in", "tokens_out",
        "cost_microusd",
    } <= set(s["llm"])
    assert "active" in s["tasks"]
    assert {"redis_health", "ws_connected_agents"} <= set(s["infra"])


def test_admin_metrics_summary_model_accepts_snapshot() -> None:
    """The router endpoint does `MetricsSummary(**snapshot_summary())` —
    pin that contract so a snapshot-shape change can't 500 the route."""
    from bp_router.api.admin import MetricsSummary
    from bp_router.observability.metrics import snapshot_summary

    model = MetricsSummary(**snapshot_summary())
    assert set(model.llm) and "active" in model.tasks
