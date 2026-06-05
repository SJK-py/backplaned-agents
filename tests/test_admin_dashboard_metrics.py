"""bp_admin dashboard — metric-card builder for the router-metrics panel.

Pure-function tests for the BFF's summary→cards transform; no app/HTTP.
"""

from __future__ import annotations

from bp_admin.pages.dashboard import _human, _metric_cards, _top_breakdown

_SUMMARY = {
    "llm": {
        "errors_total": 3,
        "errors_by_code": {"upstream_unavailable": 1, "upstream_timeout": 2},
        "fallback_chain_exhausted_total": 1,
        "fallback_used_total": 0,
        "calls_total": 1500,
        "calls_by_provider": {"anthropic": 1500},
        "tokens_in": 1200,
        "tokens_out": 340,
        "cost_microusd": 1_500_000,
    },
    "tasks": {"active": 2, "active_by_state": {"running": 2}},
    "infra": {"redis_health": 1, "ws_connected_agents": 4},
}


def _card(cards: list[dict], label: str) -> dict:
    return next(c for c in cards if c["label"] == label)


def test_human() -> None:
    assert _human(42) == "42"
    assert _human(1500) == "1.5k"
    assert _human(2_000_000) == "2.0M"


def test_top_breakdown_sorts_and_caps() -> None:
    out = _top_breakdown({"a": 1, "b": 5, "c": 3}, limit=2)
    assert out == "b 5, c 3"
    assert _top_breakdown({}) is None
    assert _top_breakdown(None) is None


def test_metric_cards_values_and_alert_tone() -> None:
    cards = _metric_cards(_SUMMARY)
    errors = _card(cards, "LLM upstream errors")
    assert errors["value"] == 3
    assert errors["tone"] == "alert"  # non-zero errors flagged
    assert "upstream_timeout 2" in errors["sub"]

    exhausted = _card(cards, "Failed (chain exhausted)")
    assert exhausted["value"] == 1 and exhausted["tone"] == "alert"

    assert _card(cards, "LLM calls (ok)")["value"] == "1.5k"
    assert _card(cards, "Tokens")["value"] == "1.5k"  # 1200 + 340
    assert _card(cards, "LLM cost")["value"] == "$1.50"
    assert _card(cards, "Active tasks")["value"] == 2
    assert _card(cards, "Redis")["value"] == "OK"


def test_metric_cards_healthy_has_no_alert() -> None:
    healthy = {
        "llm": {"errors_total": 0, "fallback_chain_exhausted_total": 0},
        "tasks": {"active": 0},
        "infra": {"redis_health": 1},
    }
    cards = _metric_cards(healthy)
    assert all(c["tone"] is None for c in cards)
    assert _card(cards, "Redis")["value"] == "OK"


def test_metric_cards_redis_down_alerts() -> None:
    cards = _metric_cards({"llm": {}, "tasks": {}, "infra": {"redis_health": 0}})
    redis = _card(cards, "Redis")
    assert redis["value"] == "DOWN" and redis["tone"] == "alert"


def test_metric_cards_none_summary_is_empty() -> None:
    assert _metric_cards(None) == []
