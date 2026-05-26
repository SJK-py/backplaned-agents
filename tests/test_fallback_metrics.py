"""Tests for the fallback-chain Prometheus counters (review M17).

`prometheus_client` isn't installed in CI, so we monkeypatch the
metrics module's Counter handles with a recording stub. Each test
asserts which counter+labels combinations got incremented along a
specific code path through `LlmService._call_with_fallback`.

The recorder mirrors the real Counter API (`labels(**).inc()`) so
the production code's call sites stay unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Stub Prometheus Counter
# ---------------------------------------------------------------------------


class _RecordingCounter:
    """Mimics `prometheus_client.Counter`'s `labels(**).inc()` chain.

    Each call appends `(label_dict, increment)` to `_calls`. Tests
    assert on the recorded tuples.
    """

    def __init__(self, name: str, recorder: list[tuple[str, dict[str, str], float]]) -> None:
        self._name = name
        self._recorder = recorder

    def labels(self, **labels: str) -> _LabeledCounter:
        return _LabeledCounter(self._name, labels, self._recorder)


class _LabeledCounter:
    def __init__(
        self,
        name: str,
        labels: dict[str, str],
        recorder: list[tuple[str, dict[str, str], float]],
    ) -> None:
        self._name = name
        self._labels = labels
        self._recorder = recorder

    def inc(self, amount: float = 1.0) -> None:
        self._recorder.append((self._name, self._labels, amount))


@pytest.fixture
def fallback_metrics(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str], float]]:
    """Replace the five fallback-related counter handles with recorders.

    Returns the shared recorder list. Tests inspect it after exercising
    the service.
    """
    import sys
    import types

    recorder: list[tuple[str, dict[str, str], float]] = []
    fake_metrics = types.ModuleType("bp_router.observability.metrics")
    for counter_name in (
        "llm_fallback_attempts_total",
        "llm_fallback_chain_exhausted_total",
        "llm_fallback_used_total",
        "llm_fallback_skipped_tier_total",
        "llm_tier_gate_denied_total",
    ):
        setattr(
            fake_metrics, counter_name, _RecordingCounter(counter_name, recorder)
        )

    # Inject under the metrics module path. The service does
    # `from bp_router.observability import metrics` (lazily) and reads
    # attributes via `getattr`, so this stub is enough.
    obs_pkg = types.ModuleType("bp_router.observability")
    obs_pkg.metrics = fake_metrics  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bp_router.observability", obs_pkg)
    monkeypatch.setitem(sys.modules, "bp_router.observability.metrics", fake_metrics)
    return recorder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outcomes_for(
    recorder: list[tuple[str, dict[str, str], float]],
    counter_name: str,
) -> list[dict[str, str]]:
    """Filter recorded calls by counter name; return label dicts in order."""
    return [labels for name, labels, _ in recorder if name == counter_name]


# ---------------------------------------------------------------------------
# Successful first-try call
# ---------------------------------------------------------------------------


def test_first_try_success_increments_attempts_success(
    fallback_metrics: list[tuple[str, dict[str, str], float]],
) -> None:
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    p = make_preset("primary")
    svc._register_preset_for_test(p)
    stub = wire_stub_adapter(svc, p)
    stub.push("OK")

    async def _attempt(preset):
        return stub, await stub.generate()

    result = asyncio.run(svc._call_with_fallback(
        preset_name="primary", user_level="admin", attempt=_attempt,
    ))
    assert result == (stub, "primary", "OK")

    attempts = _outcomes_for(fallback_metrics, "llm_fallback_attempts_total")
    assert attempts == [{"preset": "primary", "outcome": "success"}]
    # No fallback used, no chain exhausted, no tier denial.
    assert _outcomes_for(fallback_metrics, "llm_fallback_used_total") == []
    assert _outcomes_for(fallback_metrics, "llm_fallback_chain_exhausted_total") == []
    assert _outcomes_for(fallback_metrics, "llm_tier_gate_denied_total") == []


# ---------------------------------------------------------------------------
# Retry-then-success on the same preset
# ---------------------------------------------------------------------------


def test_retry_then_success_records_retry_and_success(
    fallback_metrics: list[tuple[str, dict[str, str], float]],
) -> None:
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    p = make_preset("flaky", max_retries=2)
    svc._register_preset_for_test(p)
    stub = wire_stub_adapter(svc, p)
    stub.push(RuntimeError("transient")).push("OK")

    async def _attempt(preset):
        return stub, await stub.generate()

    asyncio.run(svc._call_with_fallback(
        preset_name="flaky", user_level="admin", attempt=_attempt,
    ))

    # First attempt: retry. Second attempt: success.
    attempts = _outcomes_for(fallback_metrics, "llm_fallback_attempts_total")
    assert attempts == [
        {"preset": "flaky", "outcome": "retry"},
        {"preset": "flaky", "outcome": "success"},
    ]
    assert _outcomes_for(fallback_metrics, "llm_fallback_used_total") == []


# ---------------------------------------------------------------------------
# Last attempt of a preset failing → outcome=failed (not retry)
# ---------------------------------------------------------------------------


def test_final_attempt_failure_is_outcome_failed_not_retry(
    fallback_metrics: list[tuple[str, dict[str, str], float]],
) -> None:
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    a = make_preset("a", max_retries=1, fallback_preset="b")
    b = make_preset("b")  # success target
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)
    stub_a = wire_stub_adapter(svc, a)
    stub_b = wire_stub_adapter(svc, b)
    stub_a.push(RuntimeError("first")).push(RuntimeError("second"))
    stub_b.push("OK from b")

    used: dict[str, Any] = {}

    async def _attempt(preset):
        if preset.name == "a":
            return stub_a, await stub_a.generate()
        used["adapter"] = stub_b
        return stub_b, await stub_b.generate()

    asyncio.run(svc._call_with_fallback(
        preset_name="a", user_level="admin", attempt=_attempt,
    ))

    # a: 1 retry (intermediate), 1 failed (last attempt before fallback).
    # b: 1 success.
    attempts = _outcomes_for(fallback_metrics, "llm_fallback_attempts_total")
    assert attempts == [
        {"preset": "a", "outcome": "retry"},
        {"preset": "a", "outcome": "failed"},
        {"preset": "b", "outcome": "success"},
    ]
    # Fallback rescued the request — record the rescue.
    used_metric = _outcomes_for(fallback_metrics, "llm_fallback_used_total")
    assert used_metric == [{"root_preset": "a", "winning_preset": "b"}]


# ---------------------------------------------------------------------------
# Whole chain exhausted
# ---------------------------------------------------------------------------


def test_chain_exhausted_increments_chain_exhausted_total(
    fallback_metrics: list[tuple[str, dict[str, str], float]],
) -> None:
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    a = make_preset("a", fallback_preset="b")
    b = make_preset("b")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)
    stub_a = wire_stub_adapter(svc, a).push(RuntimeError("a"))
    stub_b = wire_stub_adapter(svc, b).push(RuntimeError("b"))

    async def _attempt(preset):
        if preset.name == "a":
            return stub_a, await stub_a.generate()
        return stub_b, await stub_b.generate()

    # PR #2 wraps chain-exhaustion failures into `LlmUpstreamError`;
    # the underlying RuntimeError is preserved as `__cause__`.
    from bp_router.llm.retry_classification import LlmUpstreamError

    with pytest.raises(LlmUpstreamError):
        asyncio.run(svc._call_with_fallback(
            preset_name="a", user_level="admin", attempt=_attempt,
        ))

    # `a` and `b` both fail their last (and only) attempt → outcome=failed.
    attempts = _outcomes_for(fallback_metrics, "llm_fallback_attempts_total")
    assert attempts == [
        {"preset": "a", "outcome": "failed"},
        {"preset": "b", "outcome": "failed"},
    ]
    # And the chain-exhausted counter gets the root_preset label.
    exhausted = _outcomes_for(fallback_metrics, "llm_fallback_chain_exhausted_total")
    assert exhausted == [{"root_preset": "a"}]
    # Nothing succeeded so no fallback_used.
    assert _outcomes_for(fallback_metrics, "llm_fallback_used_total") == []


# ---------------------------------------------------------------------------
# First-preset tier denial → tier_gate_denied
# ---------------------------------------------------------------------------


def test_first_preset_tier_denial_increments_tier_gate_denied(
    fallback_metrics: list[tuple[str, dict[str, str], float]],
) -> None:
    from bp_router.llm.presets import PresetNotAllowedError
    from tests.conftest import make_llm_service, make_preset

    svc = make_llm_service()
    p = make_preset("restricted", min_user_level="tier0")
    svc._register_preset_for_test(p)

    async def _attempt(preset):
        raise AssertionError("attempt() must NOT run when tier gate denies")

    with pytest.raises(PresetNotAllowedError):
        asyncio.run(svc._call_with_fallback(
            preset_name="restricted", user_level="tier3", attempt=_attempt,
        ))

    denials = _outcomes_for(fallback_metrics, "llm_tier_gate_denied_total")
    assert denials == [{"preset": "restricted"}]
    # And no attempts were recorded — denial happens before the loop.
    assert _outcomes_for(fallback_metrics, "llm_fallback_attempts_total") == []


# ---------------------------------------------------------------------------
# Mid-chain tier skip → fallback_skipped_tier (NOT tier_gate_denied)
# ---------------------------------------------------------------------------


def test_mid_chain_tier_skip_increments_fallback_skipped_tier(
    fallback_metrics: list[tuple[str, dict[str, str], float]],
) -> None:
    """A fallback target the user can't access is silently skipped —
    that's NOT a tier-gate denial (no error to the caller); it's a
    fallback-skip event."""
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    a = make_preset("a", fallback_preset="b")  # *
    b = make_preset("b", min_user_level="tier0", fallback_preset="c")  # restricted
    c = make_preset("c")  # *
    for p in (a, b, c):
        svc._register_preset_for_test(p)
    stub_a = wire_stub_adapter(svc, a).push(RuntimeError("a fail"))
    stub_c = wire_stub_adapter(svc, c).push("OK from c")

    async def _attempt(preset):
        if preset.name == "a":
            return stub_a, await stub_a.generate()
        if preset.name == "b":
            raise AssertionError("b must be skipped, not attempted")
        return stub_c, await stub_c.generate()

    asyncio.run(svc._call_with_fallback(
        preset_name="a", user_level="tier3", attempt=_attempt,
    ))

    # a: 1 failed → walks to b. b: skipped (tier mismatch). c: success.
    attempts = _outcomes_for(fallback_metrics, "llm_fallback_attempts_total")
    assert attempts == [
        {"preset": "a", "outcome": "failed"},
        {"preset": "c", "outcome": "success"},
    ]
    # The skip got its own metric.
    skipped = _outcomes_for(fallback_metrics, "llm_fallback_skipped_tier_total")
    assert skipped == [{"preset": "b"}]
    # First-preset denial counter must NOT fire (a is `*`, the user
    # legitimately reaches it; b is the silent skip target).
    assert _outcomes_for(fallback_metrics, "llm_tier_gate_denied_total") == []
    # The fallback rescued the request.
    used = _outcomes_for(fallback_metrics, "llm_fallback_used_total")
    assert used == [{"root_preset": "a", "winning_preset": "c"}]


# ---------------------------------------------------------------------------
# Resilience: missing prometheus_client doesn't crash the service
# ---------------------------------------------------------------------------


def test_missing_metrics_module_does_not_break_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `_inc_metric` helper swallows any failure — operators
    running without `prometheus_client` shouldn't see crashes."""
    import sys

    # Force the metrics import to fail.
    monkeypatch.setitem(sys.modules, "bp_router.observability.metrics", None)
    from bp_router.llm.service import LlmService

    LlmService._inc_metric("nonexistent_counter", foo="bar")  # must not raise


def test_missing_counter_attribute_does_not_break_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the named counter doesn't exist on the metrics module, the
    helper short-circuits cleanly."""
    import sys
    import types

    fake = types.ModuleType("bp_router.observability.metrics")
    monkeypatch.setitem(sys.modules, "bp_router.observability.metrics", fake)

    from bp_router.llm.service import LlmService

    LlmService._inc_metric("definitely_not_a_real_counter", x="y")
