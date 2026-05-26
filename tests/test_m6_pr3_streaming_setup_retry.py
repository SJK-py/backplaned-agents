"""Tests for PR #3 of the M6 sequence — streaming setup-retry +
meta-delta hint emission.

What this PR landed:
  - `LlmDelta.meta` field on the dataclass; dispatch forwards it to
    `LlmDeltaFrame.meta`.
  - `StreamInterrupted` exception for mid-stream failures.
  - `LlmService._generate_stream_with_setup_retry` async generator:
      * Pre-first-delta retry loop (only retries the SAME preset; no
        fallback chain for streaming per design doc §6).
      * Yields `LlmDelta(meta={"kind": "retry_pending", ...})` during
        the backoff between attempts.
      * Post-first-delta failure raises `StreamInterrupted`.
  - `setup_retry` outcome on `llm_fallback_attempts_total`.
  - `service.generate(stream=True)` routes through the wrapper.
  - Dispatch catches `StreamInterrupted` and emits the typed
    `stream_interrupted` code.

Drives a programmable stub adapter that produces failures /
successes in a controlled sequence, plus a stub classifier that maps
specific exception class names to typed retry hints.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import patch

import pytest

from bp_protocol.frames import ErrorCode
from bp_router.llm.retry_classification import (
    LlmUpstreamError,
    RetryHint,
    StreamInterrupted,
)
from bp_router.llm.service import LlmDelta

# ---------------------------------------------------------------------------
# Stub adapter — programmable streaming generator
# ---------------------------------------------------------------------------


class _StreamStubAdapter:
    """Stand-in for a `ProviderAdapter` whose `generate(stream=True)`
    pulls behaviour from a per-call queue.

    Each entry in `_attempts` is one of:
      * a list of `LlmDelta` objects → iterator yields them in turn
      * an Exception class to raise BEFORE the first delta
      * a tuple `(deltas, mid_stream_exc)` → yield deltas, then raise

    `calls` counts how many times `generate(stream=True)` was awaited.
    """

    provider_name = "stream-stub"
    concrete_model = "stub"

    def __init__(self) -> None:
        self._attempts: list[Any] = []
        self.calls = 0

    def push(self, attempt: Any) -> _StreamStubAdapter:
        self._attempts.append(attempt)
        return self

    @staticmethod
    def _classify(exc: BaseException) -> RetryHint:
        cls_name = type(exc).__name__
        if cls_name == "FakeRateLimit":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                retry_after_seconds=1.5,
                upstream_class=cls_name,
            )
        if cls_name == "FakeTimeout":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
                upstream_class=cls_name,
            )
        if cls_name == "FakeBadRequest":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
                upstream_class=cls_name,
            )
        return RetryHint(
            code=ErrorCode.INTERNAL_ERROR,
            upstream_class=cls_name,
        )

    async def generate(
        self,
        messages: list[Any],
        *,
        tools: Any = None,
        tool_choice: Any = None,
        temperature: Any = None,
        max_tokens: Any = None,
        stream: bool = False,
        provider_options: Any = None,
    ) -> Any:
        if not stream:
            raise AssertionError("test stub only supports stream=True")
        self.calls += 1
        if not self._attempts:
            raise RuntimeError(
                f"_StreamStubAdapter ran out of attempts (call #{self.calls})"
            )
        outcome = self._attempts.pop(0)
        return self._make_iter(outcome)

    @staticmethod
    async def _make_iter(outcome: Any) -> Any:
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome("simulated upstream failure")
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple):
            deltas, mid_stream_exc = outcome
            for d in deltas:
                yield d
            raise mid_stream_exc
        for d in outcome:
            yield d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wire_stream_stub(svc: Any, preset: Any, adapter: Any) -> None:
    """Mirrors `tests.conftest.cache_key_for` — pre-populate the
    adapter cache so `_resolve_one` returns the stub."""
    secret_marker = (
        f"inline:{preset.name}" if preset.api_key else preset.api_key_ref
    )
    base_url_marker = preset.base_url or "-"
    cache_key = (
        f"{preset.provider}::{preset.concrete_model}::"
        f"{base_url_marker}::{secret_marker}"
    )
    svc._adapters[cache_key] = adapter


async def _drain(stream: Any) -> list[LlmDelta]:
    out: list[LlmDelta] = []
    async for d in stream:
        out.append(d)
    return out


async def _run_drain(svc: Any, preset: str = "p") -> list[LlmDelta]:
    """`generate(stream=True)` and drain. Caller handles exceptions."""
    from bp_router.llm.service import Message

    stream = await svc.generate(
        [Message(role="user", content="hi")],
        preset=preset,
        user_level="admin",
        stream=True,
    )
    return await _drain(stream)


# ---------------------------------------------------------------------------
# Service-layer streaming setup-retry behaviour
# ---------------------------------------------------------------------------


def test_streaming_no_failure_yields_deltas_unchanged() -> None:
    """Smoke test — when nothing fails, the wrapper is transparent
    and yields the adapter's deltas in order."""
    from tests.conftest import make_llm_service, make_preset

    svc = make_llm_service()
    p = make_preset("p")
    svc._register_preset_for_test(p)
    adapter = _StreamStubAdapter().push([
        LlmDelta(text="hello"),
        LlmDelta(text=" world"),
        LlmDelta(finish_reason="stop"),
    ])
    _wire_stream_stub(svc, p, adapter)

    out = asyncio.run(_run_drain(svc))

    assert [d.text for d in out if d.text] == ["hello", " world"]
    assert any(d.finish_reason == "stop" for d in out)
    # No meta deltas in a clean run.
    assert all(d.meta is None for d in out)
    assert adapter.calls == 1


def test_first_delta_retriable_failure_emits_meta_then_retries() -> None:
    """When the first attempt fails with a retriable code (rate
    limit), the wrapper emits a `meta` delta with the hint, sleeps,
    and retries. Successful retry yields content deltas."""
    from tests.conftest import make_llm_service, make_preset

    FakeRateLimit = type("FakeRateLimit", (Exception,), {})

    svc = make_llm_service()
    # max_retries=1 → 2 attempts total. First fails, second succeeds.
    p = make_preset("p", max_retries=1)
    svc._register_preset_for_test(p)
    adapter = (
        _StreamStubAdapter()
        .push(FakeRateLimit)
        .push([LlmDelta(text="recovered"), LlmDelta(finish_reason="stop")])
    )
    _wire_stream_stub(svc, p, adapter)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("asyncio.sleep", _record_sleep):
        out = asyncio.run(_run_drain(svc))

    # Adapter called twice (initial + 1 retry).
    assert adapter.calls == 2

    # First yielded delta is the meta hint, second is content.
    # R5: `retry_after_seconds` in the meta is the jittered value
    # the wrapper actually slept for (provider hint × uniform(0.8,
    # 1.2)), so compare structure + ranges instead of exact bytes.
    meta = next(d for d in out if d.meta is not None)
    assert meta.meta["kind"] == "retry_pending"
    assert meta.meta["attempt"] == 1
    assert meta.meta["max_attempts"] == 2
    assert meta.meta["reason_code"] == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert 1.2 <= meta.meta["retry_after_seconds"] <= 1.8
    contents = [d.text for d in out if d.text]
    assert contents == ["recovered"]
    # Classifier supplied 1.5 → jittered into [1.2, 1.8].
    assert len(sleeps) == 1
    assert 1.2 <= sleeps[0] <= 1.8


def test_first_delta_non_retriable_failure_raises_typed_error() -> None:
    """A `BadRequestError` is non-retriable. No meta delta, no
    sleep, just raise the typed code."""
    from tests.conftest import make_llm_service, make_preset

    FakeBadRequest = type("FakeBadRequest", (Exception,), {})

    svc = make_llm_service()
    p = make_preset("p", max_retries=3)  # would retry retriable codes
    svc._register_preset_for_test(p)
    adapter = _StreamStubAdapter().push(FakeBadRequest)
    _wire_stream_stub(svc, p, adapter)

    sleeps: list[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)

    with patch("asyncio.sleep", _record_sleep):
        with pytest.raises(LlmUpstreamError) as exc_info:
            asyncio.run(_run_drain(svc))

    assert exc_info.value.code == ErrorCode.LLM_UPSTREAM_INVALID_REQUEST
    assert adapter.calls == 1  # NOT retried
    assert sleeps == []  # No backoff for non-retriable


def test_attempts_exhausted_raises_typed_error_with_last_hint() -> None:
    """When every attempt fails retriably and we run out of retries,
    surface `LlmUpstreamError` carrying the last hint."""
    from tests.conftest import make_llm_service, make_preset

    FakeTimeout = type("FakeTimeout", (Exception,), {})

    svc = make_llm_service()
    p = make_preset("p", max_retries=2)  # 3 attempts total
    svc._register_preset_for_test(p)
    adapter = (
        _StreamStubAdapter()
        .push(FakeTimeout)
        .push(FakeTimeout)
        .push(FakeTimeout)
    )
    _wire_stream_stub(svc, p, adapter)

    async def _no_sleep(_s: float) -> None:
        return None

    with patch("asyncio.sleep", _no_sleep):
        with pytest.raises(LlmUpstreamError) as exc_info:
            asyncio.run(_run_drain(svc))

    assert exc_info.value.code == ErrorCode.LLM_UPSTREAM_TIMEOUT
    assert adapter.calls == 3


def test_mid_stream_failure_raises_stream_interrupted() -> None:
    """Once deltas have started flowing, a subsequent failure can't
    be retried — the agent has partial output. Surface as
    `StreamInterrupted`."""
    from tests.conftest import make_llm_service, make_preset

    FakeMidDrop = type("FakeMidDrop", (Exception,), {})

    svc = make_llm_service()
    p = make_preset("p", max_retries=3)  # plenty of retries available
    svc._register_preset_for_test(p)
    adapter = _StreamStubAdapter().push((
        [LlmDelta(text="part 1"), LlmDelta(text="part 2")],
        FakeMidDrop("connection reset"),
    ))
    _wire_stream_stub(svc, p, adapter)

    out: list[LlmDelta] = []

    async def _drive() -> None:
        from bp_router.llm.service import Message
        stream = await svc.generate(
            [Message(role="user", content="hi")],
            preset="p", user_level="admin", stream=True,
        )
        async for d in stream:
            out.append(d)

    with pytest.raises(StreamInterrupted) as exc_info:
        asyncio.run(_drive())

    # Two deltas reached the agent before the drop.
    assert exc_info.value.after_n_deltas == 2
    assert [d.text for d in out] == ["part 1", "part 2"]
    # Adapter NOT called again — mid-stream failures don't retry.
    assert adapter.calls == 1


def test_meta_delta_emits_correct_attempt_numbers() -> None:
    """Two retries → two meta deltas with `attempt` 1, 2 and
    `max_attempts` always reflecting the configured cap."""
    from tests.conftest import make_llm_service, make_preset

    FakeTimeout = type("FakeTimeout", (Exception,), {})

    svc = make_llm_service()
    p = make_preset("p", max_retries=2)  # 3 attempts total
    svc._register_preset_for_test(p)
    adapter = (
        _StreamStubAdapter()
        .push(FakeTimeout)
        .push(FakeTimeout)
        .push([LlmDelta(text="finally"), LlmDelta(finish_reason="stop")])
    )
    _wire_stream_stub(svc, p, adapter)

    async def _no_sleep(_s: float) -> None:
        return None

    with patch("asyncio.sleep", _no_sleep):
        out = asyncio.run(_run_drain(svc))

    metas = [d.meta for d in out if d.meta is not None]
    assert len(metas) == 2
    assert metas[0]["attempt"] == 1
    assert metas[0]["max_attempts"] == 3
    assert metas[1]["attempt"] == 2
    assert metas[1]["max_attempts"] == 3
    # All meta deltas carry the failure's typed code.
    assert all(m["reason_code"] == ErrorCode.LLM_UPSTREAM_TIMEOUT for m in metas)


def test_streaming_does_not_walk_fallback_chain() -> None:
    """Even when the requested preset has a fallback configured,
    streaming retries the SAME preset only — never walks. Chain
    walks would mid-flight switch the agent to a different model."""
    from tests.conftest import make_llm_service, make_preset

    FakeTimeout = type("FakeTimeout", (Exception,), {})

    svc = make_llm_service()
    primary = make_preset("primary", max_retries=1, fallback_preset="backup")
    backup = make_preset("backup")
    svc._register_preset_for_test(primary)
    svc._register_preset_for_test(backup)

    primary_stub = _StreamStubAdapter().push(FakeTimeout).push(FakeTimeout)
    backup_stub = _StreamStubAdapter()
    _wire_stream_stub(svc, primary, primary_stub)
    _wire_stream_stub(svc, backup, backup_stub)

    async def _no_sleep(_s: float) -> None:
        return None

    with patch("asyncio.sleep", _no_sleep):
        with pytest.raises(LlmUpstreamError):
            asyncio.run(_run_drain(svc, preset="primary"))

    # Primary tried twice (initial + 1 retry); backup NEVER touched.
    assert primary_stub.calls == 2
    assert backup_stub.calls == 0


def test_empty_stream_terminates_cleanly() -> None:
    """An empty iterator (StopAsyncIteration on first __anext__) is a
    successful no-op response, not a retriable failure."""
    from tests.conftest import make_llm_service, make_preset

    svc = make_llm_service()
    p = make_preset("p", max_retries=2)
    svc._register_preset_for_test(p)
    adapter = _StreamStubAdapter().push([])  # empty list of deltas
    _wire_stream_stub(svc, p, adapter)

    out = asyncio.run(_run_drain(svc))

    assert out == []
    assert adapter.calls == 1  # No retry on empty


def test_setup_retry_metric_increments_per_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`llm_fallback_attempts_total{outcome=setup_retry}` increments
    once per pre-first-delta retry."""
    import sys
    import types

    recorder: list[tuple[str, dict[str, str], float]] = []

    class _Counter:
        def __init__(self, name: str) -> None:
            self._name = name
        def labels(self, **labels: str) -> _Labeled:
            return _Labeled(self._name, labels)

    class _Labeled:
        def __init__(self, name: str, labels: dict[str, str]) -> None:
            self._name = name
            self._labels = labels
        def inc(self, amount: float = 1.0) -> None:
            recorder.append((self._name, self._labels, amount))

    fake_metrics = types.ModuleType("bp_router.observability.metrics")
    for n in (
        "llm_fallback_attempts_total",
        "llm_fallback_chain_exhausted_total",
        "llm_fallback_used_total",
        "llm_fallback_skipped_tier_total",
        "llm_tier_gate_denied_total",
    ):
        setattr(fake_metrics, n, _Counter(n))
    # Stub both the package and the submodule so `from
    # bp_router.observability import metrics` resolves to the fake.
    obs_pkg = types.ModuleType("bp_router.observability")
    obs_pkg.metrics = fake_metrics  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bp_router.observability", obs_pkg)
    monkeypatch.setitem(
        sys.modules, "bp_router.observability.metrics", fake_metrics
    )

    from tests.conftest import make_llm_service, make_preset

    FakeTimeout = type("FakeTimeout", (Exception,), {})

    svc = make_llm_service()
    p = make_preset("p", max_retries=2)
    svc._register_preset_for_test(p)
    adapter = (
        _StreamStubAdapter()
        .push(FakeTimeout)
        .push(FakeTimeout)
        .push([LlmDelta(text="ok")])
    )
    _wire_stream_stub(svc, p, adapter)

    async def _no_sleep(_s: float) -> None:
        return None

    with patch("asyncio.sleep", _no_sleep):
        asyncio.run(_run_drain(svc))

    outcomes = [
        labels["outcome"]
        for name, labels, _ in recorder
        if name == "llm_fallback_attempts_total"
    ]
    # Two retries (setup_retry × 2) followed by one success.
    assert outcomes == ["setup_retry", "setup_retry", "success"]


# ---------------------------------------------------------------------------
# Frame-level wiring — LlmDelta.meta forwards to LlmDeltaFrame.meta
# ---------------------------------------------------------------------------


def test_lldelta_dataclass_has_meta_field() -> None:
    """The service-layer `LlmDelta` dataclass has a `meta` field for
    the wrapper's status hints. Defaults to None."""
    d = LlmDelta(text="hi")
    assert d.meta is None
    d2 = LlmDelta(meta={"kind": "retry_pending", "attempt": 1, "max_attempts": 3,
                        "retry_after_seconds": 0.5, "reason_code": "x"})
    assert d2.meta is not None
    assert d2.text is None  # mutex with content fields by convention


def test_dispatch_forwards_meta_field_to_frame() -> None:
    """Source check: the streaming aggregator constructs an
    `LlmDeltaFrame(meta=...)` for meta deltas, branched away from
    the content-field path so the mutex validator doesn't reject."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    assert "delta.meta is not None" in src
    assert "meta=delta.meta" in src


def test_dispatch_emits_stream_interrupted_code() -> None:
    """Source check: dispatch catches `StreamInterrupted` and emits
    `ErrorCode.LLM_STREAM_INTERRUPTED` in the terminal `LlmResultFrame`."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    assert "except StreamInterrupted" in src
    assert "LLM_STREAM_INTERRUPTED" in src
    # And the after_n_deltas count makes it into the log payload
    # (useful for diagnosing where the stream cut out).
    assert "after_n_deltas" in src


# ---------------------------------------------------------------------------
# Streaming setup-retry honours every code in RETRIABLE_LLM_CODES
# ---------------------------------------------------------------------------


def test_setup_retry_honours_every_retriable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for H2: `_generate_stream_with_setup_retry` reuses
    the protocol-side `RETRIABLE_LLM_CODES` constant instead of a
    hardcoded literal set. A new retriable code added to the
    protocol must automatically be picked up by the streaming retry
    boundary — otherwise the unary path retries on it and streaming
    silently doesn't.

    Drives the stub adapter once per code in `RETRIABLE_LLM_CODES`,
    asserts each one triggers a setup-retry rather than surfacing
    immediately. The dispatch-layer codes (e.g. `auth_lookup_failed`)
    aren't actually emitted by adapter classifiers but ARE in the
    set; we test the membership semantics, not whether any specific
    adapter raises them.
    """
    from bp_protocol.frames import RETRIABLE_LLM_CODES
    from tests.conftest import make_llm_service, make_preset

    async def _no_sleep(_s: float) -> None:
        return None

    for retriable_code in sorted(RETRIABLE_LLM_CODES):
        # Build a fresh service per code so attempts don't leak across.
        svc = make_llm_service()
        preset = make_preset("p", max_retries=1)
        svc._register_preset_for_test(preset)

        FakeExc = type("FakeExc", (Exception,), {})

        # Adapter that raises FakeExc on the first attempt, then yields
        # one content delta on the second. The classifier maps FakeExc
        # to `retriable_code` so the loop must retry.
        adapter = (
            _StreamStubAdapter()
            .push(FakeExc)
            .push([LlmDelta(text="ok")])
        )
        # Override the classifier to return the code under test.
        adapter._classify = staticmethod(  # type: ignore[method-assign]
            lambda exc, _code=retriable_code: RetryHint(
                code=_code, upstream_class="FakeExc"
            )
        )
        _wire_stream_stub(svc, preset, adapter)

        with patch("asyncio.sleep", _no_sleep):
            out = asyncio.run(_run_drain(svc))

        # Both attempts must have run — the retriable code triggered a
        # setup-retry. If H2 regressed (hardcoded set drifted), some
        # codes here would skip the retry and `out` would be empty.
        assert adapter.calls == 2, (
            f"code {retriable_code!r} did not trigger setup-retry; "
            f"adapter was called {adapter.calls}× (expected 2)"
        )
        # And the recovered delta is delivered.
        assert any(d.text == "ok" for d in out if d.text is not None)


def test_setup_retry_does_not_retry_non_retriable_code() -> None:
    """Negative side of the same boundary: a non-retriable code
    (e.g. `upstream_invalid_request`) surfaces on attempt 1 without
    a second attempt. Catches a regression where someone broadens
    `RETRIABLE_LLM_CODES` accidentally."""
    from tests.conftest import make_llm_service, make_preset

    svc = make_llm_service()
    preset = make_preset("p", max_retries=2)
    svc._register_preset_for_test(preset)

    FakeBadRequest = type("FakeBadRequest", (Exception,), {})
    adapter = (
        _StreamStubAdapter()
        .push(FakeBadRequest)
    )
    adapter._classify = staticmethod(  # type: ignore[method-assign]
        lambda exc: RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class="FakeBadRequest",
        )
    )
    _wire_stream_stub(svc, preset, adapter)

    async def _no_sleep(_s: float) -> None:
        return None

    with patch("asyncio.sleep", _no_sleep):
        with pytest.raises(LlmUpstreamError) as exc_info:
            asyncio.run(_run_drain(svc))

    assert exc_info.value.hint.code == ErrorCode.LLM_UPSTREAM_INVALID_REQUEST
    assert adapter.calls == 1, (
        f"non-retriable code triggered a retry (calls={adapter.calls})"
    )
