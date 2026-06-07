"""Catalog-construction visibility probes emit
`acl_decisions_total{decision="visibility"}`.

R4 second-pass review (low) noted that `docs/backplaned/acl.md §15`
declared a `decision = visibility | permission` label on
`acl_decisions_total`, but only the `permission` value was ever
emitted — the catalog-construction visibility path
(`compute_callable_user_levels`) called `is_allowed` directly,
bypassing `_record_metric`.

R5 fix: a parallel `_record_visibility_metric(decision)` emits
the missed label. Cardinality is bounded by pinning the
synthetic `rule_name` to `<batch>` — per-pair detail isn't
actionable from /metrics and would explode the series count.
"""

from __future__ import annotations

import pytest


def test_compute_callable_user_levels_emits_visibility_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Functional: `compute_callable_user_levels` emits one
    `decision="visibility"` increment per (callee × level)
    probed."""
    pytest.importorskip("asyncpg")
    pytest.importorskip("prometheus_client")
    from bp_router import acl
    from bp_router.observability.metrics import acl_decisions_total

    # Open allow-all rule.
    rules = [
        acl.Rule(
            ord=0, effect="allow", user_level="*",
            caller_pattern="*/*", callee_pattern="*/*",
        ),
    ]

    # Snapshot the counter for each effect we expect.
    before_allow = acl_decisions_total.labels(
        decision="visibility", effect="allow", rule_name="<batch>"
    )._value.get()  # type: ignore[attr-defined]

    out = acl.compute_callable_user_levels(
        rules,
        caller_id="agt_caller",
        caller_groups=["g1"],
        caller_capabilities=["cap.x"],
        callee_id="agt_callee",
        callee_groups=["g1"],
        callee_capabilities=["cap.y"],
        deployment_levels=["admin", "tier0", "tier1", "tier2"],
    )

    # 4 levels all allowed by the open rule.
    assert set(out) == {"admin", "tier0", "tier1", "tier2"}

    after_allow = acl_decisions_total.labels(
        decision="visibility", effect="allow", rule_name="<batch>"
    )._value.get()  # type: ignore[attr-defined]

    # One increment per allowed level.
    assert after_allow - before_allow == 4


def test_visibility_metric_uses_batch_rule_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source pin: the visibility recorder hard-codes
    `rule_name="<batch>"` regardless of which rule actually
    matched — high cardinality (per-pair rule_names) was the
    reason the visibility path bypassed the metric originally."""
    pytest.importorskip("asyncpg")
    import inspect

    from bp_router import acl

    src = inspect.getsource(acl._record_visibility_metric)
    assert 'rule_name="<batch>"' in src
    assert 'decision="visibility"' in src


def test_visibility_metric_default_deny_records_default_deny_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no rule matches (default deny), the recorder emits
    `effect="default_deny"` rather than `deny`. Pin the labelling
    so operators can graph default-deny rate distinctly."""
    pytest.importorskip("asyncpg")
    pytest.importorskip("prometheus_client")
    from bp_router import acl
    from bp_router.observability.metrics import acl_decisions_total

    # No rules → every probe falls through to default deny.
    rules: list[acl.Rule] = []

    before_default = acl_decisions_total.labels(
        decision="visibility", effect="default_deny", rule_name="<batch>"
    )._value.get()  # type: ignore[attr-defined]

    out = acl.compute_callable_user_levels(
        rules,
        caller_id="agt_caller",
        caller_groups=[],
        caller_capabilities=[],
        callee_id="agt_callee",
        callee_groups=[],
        callee_capabilities=[],
        deployment_levels=["admin", "tier0"],
    )
    assert out == []  # nothing allowed

    after_default = acl_decisions_total.labels(
        decision="visibility", effect="default_deny", rule_name="<batch>"
    )._value.get()  # type: ignore[attr-defined]

    # 2 levels probed, both default-denied.
    assert after_default - before_default == 2


def test_visibility_recorder_is_defensive_against_metric_errors() -> None:
    """The recorder swallows exceptions so a metric-import or
    registry hiccup doesn't break catalog construction. Pin the
    try/except shape."""
    pytest.importorskip("asyncpg")
    import inspect

    from bp_router import acl

    src = inspect.getsource(acl._record_visibility_metric)
    assert "try:" in src
    assert "except Exception" in src
    assert "acl visibility metric record failed" in src
