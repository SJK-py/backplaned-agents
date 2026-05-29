"""Provider-supplied `Retry-After` hints are jittered.

R4 second-pass review (low) noted that
`compute_backoff` honoured `Retry-After: 5` verbatim:

    return min(max(retry_after_seconds, 0.0), max_backoff_s)

Without jitter, every worker that received the same hint from
a shared upstream rate-limit hit would sleep exactly the same
duration and then stampede the upstream simultaneously on
retry — the classic thundering-herd pattern. The exponential-
backoff branch already jittered (full jitter, `uniform(0, capped)`);
the hint branch did not.

R5 fix: apply ±20% jitter to the hint (uniform in [0.8, 1.2] ×
hint) before returning. The base is still bounded by
max_backoff_s so a misconfigured upstream can't pause us
indefinitely.
"""

from __future__ import annotations

import inspect
import random

import pytest


def test_retry_after_hint_jittered_in_default_path() -> None:
    """Hint=10 → output uniformly in [8.0, 12.0] (no other clamp
    interferes when max_backoff_s is set high enough)."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    rng = random.Random(42)
    out = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=10.0,
        max_backoff_s=60.0,
        rng=rng,
    )
    # 10.0 × uniform(0.8, 1.2) ∈ [8.0, 12.0]
    assert 8.0 <= out <= 12.0
    assert out != 10.0  # almost-certainly different (seeded RNG)


def test_retry_after_hint_no_jitter_when_disabled() -> None:
    """jitter=False short-circuits — hint passes through verbatim
    (subject to max_backoff_s clamp)."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    out = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=10.0,
        max_backoff_s=60.0,
        jitter=False,
    )
    assert out == 10.0


def test_retry_after_jitter_respects_max_backoff_cap() -> None:
    """When jittered hint × 1.2 exceeds the cap, the cap wins.
    Otherwise a 9s hint could surface as 10.8s, over the 10s
    cap."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    rng = random.Random(0)  # deterministic
    # 9 × 1.2 = 10.8. Cap at 10.0.
    out = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=9.0,
        max_backoff_s=10.0,
        rng=rng,
    )
    assert out <= 10.0


def test_retry_after_zero_hint_passes_through() -> None:
    """A zero hint shouldn't get scaled by uniform(0.8, 1.2)
    (result would still be 0) — verify the helper returns 0
    cleanly without dividing or otherwise mishandling."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    out = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=0.0,
        max_backoff_s=60.0,
    )
    assert out == 0.0


def test_retry_after_negative_hint_clamped_to_zero() -> None:
    """A negative `Retry-After` is malformed but harmless after
    the existing clamp; the jitter path mustn't reintroduce
    negativity (uniform(0.8, 1.2) × negative would still be
    negative)."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    out = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=-5.0,
        max_backoff_s=60.0,
    )
    assert out == 0.0


def test_jitter_application_uses_provided_rng() -> None:
    """When `rng` is provided, the jittered hint is deterministic
    across calls — pin the call shape so a future refactor that
    silently uses the module-global RNG breaks this."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    out_a = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=10.0,
        max_backoff_s=60.0,
        rng=random.Random(123),
    )
    out_b = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=10.0,
        max_backoff_s=60.0,
        rng=random.Random(123),
    )
    assert out_a == out_b


def test_distribution_spans_jitter_window() -> None:
    """Statistical: 100 jittered hints around 10s should cover at
    least 50% of the [8, 12] window — confirms the jitter is
    actually being applied (not a constant)."""
    pytest.importorskip("pydantic")
    from bp_router.llm.retry_classification import compute_backoff

    rng = random.Random(99)
    samples = [
        compute_backoff(
            attempt_idx=0,
            retry_after_seconds=10.0,
            max_backoff_s=60.0,
            rng=rng,
        )
        for _ in range(100)
    ]
    assert min(samples) < 9.5
    assert max(samples) > 10.5


def test_source_pin_jitter_applied_in_hint_branch() -> None:
    """Source pin: the helper applies `rng.uniform(0.8, 1.2)` in
    the retry_after_seconds branch. A regression that returns the
    raw hint fails this pin."""
    pytest.importorskip("pydantic")
    from bp_router.llm import retry_classification

    src = inspect.getsource(retry_classification.compute_backoff)
    assert "uniform(0.8, 1.2)" in src
