"""`acl_decisions_total{rule_name}` label is bounded.

R4 second-pass review (low) flagged `rule_name` as an
admin-supplied string used directly as a Prometheus label.
Prometheus stores one series per unique label combination — an
admin pasting unique strings (request IDs, user emails,
timestamps) into rule names creates unbounded series count.

R5 fix: `_bound_metric_label(rule_name)` truncates to 64 chars,
replaces non-slug chars with `_`, and preserves the synthetic
`<default>` / `<self_call>` / `<unnamed>` labels intact.

The underlying `Rule.name` keeps the admin-supplied string —
the bound is metric-side only.
"""

from __future__ import annotations

import pytest


def test_short_slug_passes_unchanged() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.acl import _bound_metric_label

    assert _bound_metric_label("allow_internal_calls") == "allow_internal_calls"
    assert _bound_metric_label("rank0:llm:default") == "rank0:llm:default"


def test_none_and_empty_normalised_to_unnamed() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.acl import _bound_metric_label

    assert _bound_metric_label(None) == "<unnamed>"
    assert _bound_metric_label("") == "<unnamed>"


def test_synthetic_labels_preserve_angle_brackets() -> None:
    """The three synthetic labels (`<default>`, `<self_call>`,
    `<unnamed>`) must round-trip unchanged. The bounder's
    allowed-char set includes `<` and `>` for this reason."""
    pytest.importorskip("asyncpg")
    from bp_router.acl import _bound_metric_label

    assert _bound_metric_label("<default>") == "<default>"
    assert _bound_metric_label("<self_call>") == "<self_call>"
    assert _bound_metric_label("<unnamed>") == "<unnamed>"


def test_long_name_truncated_to_64_chars() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.acl import _METRIC_RULE_NAME_MAX_LEN, _bound_metric_label

    very_long = "x" * 1000
    out = _bound_metric_label(very_long)
    assert len(out) == _METRIC_RULE_NAME_MAX_LEN


def test_special_chars_replaced_with_underscore() -> None:
    """Admin pastes a request ID or email-shaped name; spaces /
    `@` / `=` / `?` / `&` / `/` get folded to `_`. Slug shape
    keeps Prometheus exposition stable (label values are
    interpreted as UTF-8 but tools and operators expect a slug)."""
    pytest.importorskip("asyncpg")
    from bp_router.acl import _bound_metric_label

    assert _bound_metric_label("rule with spaces") == "rule_with_spaces"
    assert _bound_metric_label("alice@example.com") == "alice_example.com"
    assert _bound_metric_label("rule?param=x&y=z") == "rule_param_x_y_z"


def test_high_cardinality_input_bounded() -> None:
    """A high-cardinality admin input (e.g. request_id-like
    UUIDs in the rule name) yields a bounded label. The
    cardinality isn't reduced to 1 — admins still create
    distinct names — but the per-label length is bounded."""
    pytest.importorskip("asyncpg")
    from bp_router.acl import _METRIC_RULE_NAME_MAX_LEN, _bound_metric_label

    bogus = "req_" + "x" * 200 + "_id"
    out = _bound_metric_label(bogus)
    assert len(out) == _METRIC_RULE_NAME_MAX_LEN


def test_metric_path_uses_bounder() -> None:
    """Source pin: `_record_metric` calls the bounder rather
    than passing `decision.rule_name` raw to `labels()`."""
    pytest.importorskip("asyncpg")
    import inspect

    from bp_router import acl

    src = inspect.getsource(acl._record_metric)
    assert "_bound_metric_label(decision.rule_name)" in src
    # Old raw `decision.rule_name or "<unnamed>"` gone from the
    # metric path.
    assert 'decision.rule_name or "<unnamed>"' not in src
