"""Tests for PR #2 of the M6 sequence — per-provider exception
classifiers + backoff + typed-code emission on chain exhaustion.

Three layers tested:

  1. **Per-provider `_classify` mapping.** Each adapter's classifier
     maps SDK exception class names to typed `RetryHint`s. We don't
     instantiate real SDK exceptions (the SDKs aren't installed in
     CI); we use stub exceptions whose `type(exc).__name__` matches
     the SDK class names the classifiers look for.

  2. **`compute_backoff` schedule.** Honours `retry_after_seconds`
     when set; otherwise exponential 0.5 × 2^N with full jitter,
     capped at 10s. Defaults match the SDK-side `RetryPolicy` from
     design doc §11.2.

  3. **`_call_with_fallback` integration.** On chain exhaustion the
     wrapper raises `LlmUpstreamError` carrying the LAST classified
     hint. `dispatch._run_llm_call` catches and emits the typed code.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC
from typing import Any
from unittest.mock import patch

import pytest

from bp_protocol.frames import ErrorCode
from bp_router.llm.retry_classification import (
    DEFAULT_HINT,
    LlmUpstreamError,
    RetryHint,
    compute_backoff,
    safe_classify,
)

# ---------------------------------------------------------------------------
# OpenAI / openai-compatible classifier (shared)
# ---------------------------------------------------------------------------


def _make_named_exc(name: str, *, message: str = "boom", retry_after: Any = None):
    """Build a stub exception whose `type().__name__` equals `name`.

    The classifiers match by `type(exc).__name__` (avoiding a hard
    dependency on the SDK at import time). A dynamically-named class
    is sufficient for the test."""
    cls = type(name, (Exception,), {})
    exc = cls(message)
    if retry_after is not None:
        # Stub the response.headers shape the SDK uses.
        class _Headers:
            def __init__(self, value: Any) -> None:
                self._v = value
            def get(self, k: str) -> Any:
                return self._v if k.lower() == "retry-after" else None
        class _Response:
            def __init__(self, hdrs: Any) -> None:
                self.headers = hdrs
        exc.response = _Response(_Headers(retry_after))
    return exc


@pytest.mark.parametrize("class_name,expected_code", [
    ("RateLimitError",            ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    ("APITimeoutError",           ErrorCode.LLM_UPSTREAM_TIMEOUT),
    ("APIConnectionError",        ErrorCode.LLM_UPSTREAM_TIMEOUT),
    ("InternalServerError",       ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("AuthenticationError",       ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("PermissionDeniedError",     ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("BadRequestError",           ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("UnprocessableEntityError",  ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("ConflictError",             ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("NotFoundError",             ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    # Unknown class → fall through to internal_error (still retriable
    # so the long tail of unknowns gets one router-side retry).
    ("MystifyingError",           ErrorCode.INTERNAL_ERROR),
])
def test_openai_classifier_mapping(class_name: str, expected_code: str) -> None:
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc(class_name)
    hint = classify_openai_exception(exc)
    assert hint.code == expected_code
    assert hint.upstream_class == class_name


def test_openai_classifier_extracts_retry_after_for_rate_limit() -> None:
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc("RateLimitError", retry_after=12)
    hint = classify_openai_exception(exc)
    assert hint.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert hint.retry_after_seconds == 12.0


def test_openai_classifier_handles_missing_response() -> None:
    """A `RateLimitError` without a `.response` attribute (some
    older SDKs / mocks) shouldn't crash — `retry_after_seconds`
    just stays None and the schedule kicks in."""
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc("RateLimitError")  # no response attached
    hint = classify_openai_exception(exc)
    assert hint.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert hint.retry_after_seconds is None


def test_openai_classifier_handles_garbage_retry_after_value() -> None:
    """A truly malformed `Retry-After` (neither numeric nor a parseable
    HTTP-date) falls back to None — the schedule kicks in."""
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc("RateLimitError", retry_after="not-a-real-value")
    hint = classify_openai_exception(exc)
    assert hint.retry_after_seconds is None


def test_openai_classifier_parses_http_date_retry_after_in_past() -> None:
    """L4: `Retry-After: <HTTP-date>` form is now handled. WAF-fronted
    endpoints (Cloudflare, Akamai) use the date form for ban-list
    responses — without this, the SDK falls back to a 10s exponential
    cap when the server actually wants minutes-to-hours.

    Date in the past clamps to 0.0 (clock skew / server bug — caller's
    schedule treats it as "retry immediately" which is safer than a
    negative sleep)."""
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc(
        "RateLimitError", retry_after="Fri, 31 Dec 1999 23:59:59 GMT"
    )
    hint = classify_openai_exception(exc)
    assert hint.retry_after_seconds == 0.0


def test_openai_classifier_parses_http_date_retry_after_in_future() -> None:
    """A future HTTP-date returns the delta in seconds. The retry-policy
    `max_backoff_s` (default 10s) caps it downstream — extremely long
    waits don't actually pause us for hours."""
    from datetime import datetime, timedelta

    from bp_router.llm.providers._openai_client import classify_openai_exception

    future = datetime.now(UTC) + timedelta(seconds=42)
    # RFC 7231 §7.1.3 IMF-fixdate format.
    formatted = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    exc = _make_named_exc("RateLimitError", retry_after=formatted)
    hint = classify_openai_exception(exc)
    # Allow a small wall-clock delta (test scheduling jitter; CI
    # machines can take a few hundred ms between datetime.now() calls).
    assert hint.retry_after_seconds is not None
    assert 30.0 <= hint.retry_after_seconds <= 50.0


# ---------------------------------------------------------------------------
# OpenAI classifier — finish-reason exceptions, OAuth, response validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name,expected_code", [
    # `client.parse(...)` helpers raise these — terminal, retry won't help.
    ("LengthFinishReasonError",     ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("ContentFilterFinishReasonError", ErrorCode.LLM_UPSTREAM_CONTENT_FILTER),
    # Response body fails Pydantic validation — proxy / CDN weirdness.
    # Treated as transient timeout-class so the bounded retry kicks in.
    ("APIResponseValidationError",  ErrorCode.LLM_UPSTREAM_TIMEOUT),
    # OAuth-specific authentication failure subclass.
    ("OAuthError",                  ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
])
def test_openai_classifier_extended_mapping(
    class_name: str, expected_code: str
) -> None:
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc(class_name)
    hint = classify_openai_exception(exc)
    assert hint.code == expected_code
    assert hint.upstream_class == class_name


@pytest.mark.parametrize("status_code,expected_code", [
    (429, ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    (502, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    (503, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    (504, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    (530, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),  # Cloudflare custom 5xx
    (418, ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
])
def test_openai_classifier_apistatuserror_parent_buckets_by_status(
    status_code: int, expected_code: str
) -> None:
    """`APIStatusError` is the parent of every typed HTTP-status
    exception. CDN-fronted endpoints can surface untyped status codes
    via the parent class (e.g. Cloudflare 530). Bucket them on
    `status_code` so we still retry the transient ones rather than
    treating them as `internal_error`."""
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc("APIStatusError")
    exc.status_code = status_code
    hint = classify_openai_exception(exc)
    assert hint.code == expected_code


def test_openai_classifier_apistatuserror_without_status_falls_through() -> None:
    """An APIStatusError stripped of `.status_code` should land in
    `internal_error` (still retriable for the long-tail-of-unknowns
    case) rather than a misclassified bucket."""
    from bp_router.llm.providers._openai_client import classify_openai_exception

    exc = _make_named_exc("APIStatusError")
    # No status_code attribute; bucket falls through.
    hint = classify_openai_exception(exc)
    assert hint.code == ErrorCode.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Anthropic classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name,expected_code", [
    ("RateLimitError",            ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    ("APITimeoutError",           ErrorCode.LLM_UPSTREAM_TIMEOUT),
    ("APIConnectionError",        ErrorCode.LLM_UPSTREAM_TIMEOUT),
    ("InternalServerError",       ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("OverloadedError",           ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("AuthenticationError",       ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("PermissionDeniedError",     ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("BadRequestError",           ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("UnprocessableEntityError",  ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("NotFoundError",             ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("ConflictError",             ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("MystifyingError",           ErrorCode.INTERNAL_ERROR),
])
def test_anthropic_classifier_mapping(
    class_name: str, expected_code: str
) -> None:
    from bp_router.llm.providers.anthropic import AnthropicAdapter

    exc = _make_named_exc(class_name)
    hint = AnthropicAdapter._classify(exc)
    assert hint.code == expected_code
    assert hint.upstream_class == class_name


@pytest.mark.parametrize("status_code,expected_code", [
    (429, ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    (502, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    (503, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    # Anthropic's 529 Overloaded sometimes surfaces via the parent
    # rather than the typed `OverloadedError` subclass — depends on
    # SDK version + intermediary CDN.
    (529, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    (418, ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
])
def test_anthropic_classifier_apistatuserror_parent_buckets_by_status(
    status_code: int, expected_code: str
) -> None:
    """Same CDN-fronted-status concern as the OpenAI side: 529 can
    bypass `OverloadedError` when an intermediary swaps the typed
    error for a plain status response, and we need it to still bucket
    as `upstream_unavailable`."""
    from bp_router.llm.providers.anthropic import AnthropicAdapter

    exc = _make_named_exc("APIStatusError")
    exc.status_code = status_code
    hint = AnthropicAdapter._classify(exc)
    assert hint.code == expected_code


def test_anthropic_classifier_response_validation_error() -> None:
    """`APIResponseValidationError` — proxy/CDN injecting an HTML
    error page or returning a partial JSON body. Treat as transient
    so the bounded retry path takes over."""
    from bp_router.llm.providers.anthropic import AnthropicAdapter

    exc = _make_named_exc("APIResponseValidationError")
    hint = AnthropicAdapter._classify(exc)
    assert hint.code == ErrorCode.LLM_UPSTREAM_TIMEOUT


# ---------------------------------------------------------------------------
# Gemini classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name,expected_code", [
    ("ResourceExhausted",        ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    ("DeadlineExceeded",         ErrorCode.LLM_UPSTREAM_TIMEOUT),
    ("ServiceUnavailable",       ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("InternalServerError",      ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("Unauthenticated",          ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("PermissionDenied",         ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("InvalidArgument",          ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("NotFound",                 ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("FailedPrecondition",       ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("AlreadyExists",            ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("MystifyingError",          ErrorCode.INTERNAL_ERROR),
])
def test_gemini_classifier_mapping(class_name: str, expected_code: str) -> None:
    from bp_router.llm.providers.gemini import GeminiAdapter

    exc = _make_named_exc(class_name)
    hint = GeminiAdapter._classify(exc)
    assert hint.code == expected_code


def test_gemini_classifier_extracts_retry_delay_from_details_proto() -> None:
    """google-api-core attaches `RetryInfo` protos on `exc.details`
    (NOT `exc.errors` — the latter is a free-form list of error
    strings). Stub the proto shape and verify the seconds + nanos
    fields flow through with sub-second precision."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    class _RetryDelay:
        seconds = 7
        nanos = 500_000_000  # half-second
    class _RetryInfoProto:
        retry_delay = _RetryDelay()

    exc = _make_named_exc("ResourceExhausted")
    exc.details = [_RetryInfoProto()]
    hint = GeminiAdapter._classify(exc)
    # 7 + 0.5 = 7.5
    assert hint.retry_after_seconds == 7.5


def test_gemini_classifier_ignores_legacy_errors_attribute() -> None:
    """Regression for the M1 review finding — the previous
    implementation read from `exc.errors`, which on
    `google.api_core.exceptions.GoogleAPICallError` is a list of
    free-form strings, not the structured detail protos. Verify the
    classifier no longer falls for that shape."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    class _RetryDelay:
        seconds = 7
        nanos = 0
    class _LegacyErrorEntry:
        retry_delay = _RetryDelay()

    exc = _make_named_exc("ResourceExhausted")
    # Old implementation would have picked this up. New one MUST NOT —
    # the proper shape lives on `.details` (left unset here).
    exc.errors = [_LegacyErrorEntry()]
    hint = GeminiAdapter._classify(exc)
    assert hint.retry_after_seconds is None


@pytest.mark.parametrize("class_name,status_code,expected_code", [
    # google-genai SDK uses `code` (HTTP status) on its error classes.
    ("ClientError", 429, ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    ("ClientError", 401, ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("ClientError", 403, ErrorCode.LLM_UPSTREAM_AUTH_FAILED),
    ("ClientError", 400, ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("ClientError", 404, ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ("ServerError", 500, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("ServerError", 502, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("ServerError", 503, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    ("ServerError", 504, ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
    # 408 — Request Timeout. Treat as the timeout bucket.
    ("ClientError", 408, ErrorCode.LLM_UPSTREAM_TIMEOUT),
    # APIError parent — same buckets via the same status field.
    ("APIError", 429, ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
])
def test_gemini_classifier_genai_errors_buckets_by_status(
    class_name: str, status_code: int, expected_code: str
) -> None:
    """L5 finding: `google.genai.errors.{ClientError, ServerError,
    APIError}` are the newer SDK's parallel hierarchy. They carry the
    HTTP status on `.code`. Bucket them the same way the
    google-api-core path does."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    exc = _make_named_exc(class_name)
    exc.code = status_code
    hint = GeminiAdapter._classify(exc)
    assert hint.code == expected_code
    assert hint.upstream_class == class_name


def test_gemini_classifier_genai_clienterror_without_code_falls_through() -> None:
    """A genai error without `.code` should land in `internal_error`
    rather than a misclassified bucket — same long-tail policy as the
    OpenAI / Anthropic side."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    exc = _make_named_exc("ClientError")
    # No code attribute set.
    hint = GeminiAdapter._classify(exc)
    assert hint.code == ErrorCode.INTERNAL_ERROR


def test_gemini_classifier_extracts_retry_delay_from_genai_json_payload() -> None:
    """google-genai (the newer SDK) raises `ClientError` whose
    `.details` is a JSON dict (the response body), not a list of
    protos. The standard google.rpc.Status shape carries
    `retryDelay: "30s"` strings under `error.details[].retryDelay`."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    exc = _make_named_exc("ClientError")
    exc.code = 429
    exc.details = {
        "error": {
            "code": 429,
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": "30s"},
            ],
        }
    }
    hint = GeminiAdapter._classify(exc)
    assert hint.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert hint.retry_after_seconds == 30.0


# ---------------------------------------------------------------------------
# Adapter wiring — every adapter exposes _classify
# ---------------------------------------------------------------------------


def test_every_adapter_exposes_classify_staticmethod() -> None:
    """Smoke test: each provider adapter has a `_classify` callable
    so `_call_with_fallback` can use it without polymorphic checks."""
    from bp_router.llm.providers.anthropic import AnthropicAdapter
    from bp_router.llm.providers.gemini import GeminiAdapter
    from bp_router.llm.providers.openai import (
        OpenAIAdapter,
        OpenAIEmbeddingsAdapter,
    )
    from bp_router.llm.providers.openai_compatible import (
        OpenAICompatibleAdapter,
        OpenAICompatibleEmbeddingsAdapter,
    )

    for adapter_cls in (
        OpenAIAdapter,
        OpenAIEmbeddingsAdapter,
        OpenAICompatibleAdapter,
        OpenAICompatibleEmbeddingsAdapter,
        AnthropicAdapter,
        GeminiAdapter,
    ):
        classify = getattr(adapter_cls, "_classify", None)
        assert callable(classify), (
            f"{adapter_cls.__name__}._classify missing"
        )
        # Smoke: an unknown exception falls through to internal_error.
        hint = classify(RuntimeError("test"))
        assert hint.code == ErrorCode.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# safe_classify
# ---------------------------------------------------------------------------


def test_safe_classify_returns_default_on_missing_classifier() -> None:
    """An adapter without `_classify` (e.g. a test stub) falls
    through to the default hint, NOT a crash."""

    class _StubAdapterNoClassify:
        pass

    hint = safe_classify(_StubAdapterNoClassify(), RuntimeError("x"))
    assert hint == DEFAULT_HINT


def test_safe_classify_returns_default_on_classifier_crash() -> None:
    """A buggy classifier (raises during classification) shouldn't
    take down the request — fall through to the default."""

    class _BadAdapter:
        @staticmethod
        def _classify(exc: BaseException) -> RetryHint:
            raise RuntimeError("classifier bug")

    hint = safe_classify(_BadAdapter(), RuntimeError("x"))
    assert hint == DEFAULT_HINT


def test_safe_classify_returns_default_when_classifier_returns_wrong_type() -> None:
    """If a future classifier returns something that isn't a
    `RetryHint` (e.g. a tuple or a string), the dispatch path
    shouldn't break."""

    class _WrongTypeAdapter:
        @staticmethod
        def _classify(exc: BaseException) -> Any:
            return "not a RetryHint"

    hint = safe_classify(_WrongTypeAdapter(), RuntimeError("x"))
    assert hint == DEFAULT_HINT


# ---------------------------------------------------------------------------
# compute_backoff schedule
# ---------------------------------------------------------------------------


def test_backoff_honours_retry_after_when_set() -> None:
    """Provider-supplied `Retry-After` wins over the schedule."""
    out = compute_backoff(
        attempt_idx=0, retry_after_seconds=5.0, jitter=False
    )
    assert out == 5.0


def test_backoff_caps_retry_after_at_max() -> None:
    """A misconfigured upstream sending `Retry-After: 3600` doesn't
    pause us for an hour — capped at `max_backoff_s`."""
    out = compute_backoff(
        attempt_idx=0,
        retry_after_seconds=3600.0,
        max_backoff_s=10.0,
        jitter=False,
    )
    assert out == 10.0


def test_backoff_clamps_negative_retry_after_to_zero() -> None:
    """A buggy server sending `Retry-After: -1` shouldn't produce a
    negative sleep."""
    out = compute_backoff(
        attempt_idx=0, retry_after_seconds=-1.0, jitter=False
    )
    assert out == 0.0


def test_backoff_uses_exponential_when_no_retry_after() -> None:
    """0.5 × 2^attempt without jitter (deterministic)."""
    assert compute_backoff(0, jitter=False) == 0.5
    assert compute_backoff(1, jitter=False) == 1.0
    assert compute_backoff(2, jitter=False) == 2.0
    assert compute_backoff(3, jitter=False) == 4.0
    assert compute_backoff(4, jitter=False) == 8.0


def test_backoff_caps_exponential_at_max() -> None:
    """Exponential growth caps at `max_backoff_s` to prevent
    multi-minute waits."""
    out = compute_backoff(10, jitter=False, max_backoff_s=10.0)
    assert out == 10.0


def test_backoff_jitter_yields_value_in_range() -> None:
    """Full jitter: result is uniformly sampled in [0, capped_value]."""
    rng = random.Random(42)
    out = compute_backoff(2, jitter=True, rng=rng, max_backoff_s=10.0)
    # capped value at attempt_idx=2 with default schedule = 2.0
    assert 0.0 <= out <= 2.0


# ---------------------------------------------------------------------------
# _call_with_fallback emits LlmUpstreamError with classified code
# ---------------------------------------------------------------------------


class _ClassifyingStubAdapter:
    """Test adapter that classifies a known stub exception name
    rather than every-RuntimeError-is-internal_error. Mirrors the
    real adapter contract."""

    provider_name = "stub-classifying"

    def __init__(self) -> None:
        self.outcomes: list[Any] = []

    def push(self, outcome: Any) -> _ClassifyingStubAdapter:
        self.outcomes.append(outcome)
        return self

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        if not self.outcomes:
            raise RuntimeError("ran out of outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @staticmethod
    def _classify(exc: BaseException) -> RetryHint:
        cls_name = type(exc).__name__
        # Map our test exception names to typed codes.
        if cls_name == "FakeRateLimit":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                retry_after_seconds=2.0,
                upstream_class=cls_name,
            )
        if cls_name == "FakeTimeout":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
                upstream_class=cls_name,
            )
        return RetryHint(
            code=ErrorCode.INTERNAL_ERROR,
            upstream_class=cls_name,
        )


def _wire_classifying_stub(svc: Any, preset: Any, adapter: Any) -> None:
    """Pre-populate the adapter cache so `_resolve_one` returns this
    classifying stub instead of attempting to build a real adapter."""
    secret_marker = (
        f"inline:{preset.name}" if preset.api_key else preset.api_key_ref
    )
    base_url_marker = preset.base_url or "-"
    cache_key = (
        f"{preset.provider}::{preset.concrete_model}::"
        f"{base_url_marker}::{secret_marker}"
    )
    svc._adapters[cache_key] = adapter


def test_chain_exhaustion_surfaces_typed_code_via_llm_upstream_error() -> None:
    """Classifier maps `FakeRateLimit` to `upstream_rate_limited`;
    `_call_with_fallback` raises `LlmUpstreamError` carrying that
    code (not the previous bare RuntimeError fallback)."""
    from tests.conftest import make_llm_service, make_preset

    FakeRateLimit = type("FakeRateLimit", (Exception,), {})

    svc = make_llm_service()
    p = make_preset("p")
    svc._register_preset_for_test(p)
    stub = _ClassifyingStubAdapter().push(FakeRateLimit("429"))
    _wire_classifying_stub(svc, p, stub)

    async def _attempt(preset):
        return stub, await stub.generate()

    # Skip backoff sleep so the test is fast — patch asyncio.sleep.
    async def _no_sleep(_seconds):  # noqa: ANN001
        return None

    with patch("asyncio.sleep", _no_sleep):
        with pytest.raises(LlmUpstreamError) as exc_info:
            asyncio.run(svc._call_with_fallback(
                preset_name="p", user_level="admin", attempt=_attempt,
            ))

    assert exc_info.value.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert exc_info.value.upstream_class == "FakeRateLimit"


def test_chain_exhaustion_uses_last_chain_step_hint() -> None:
    """The hint surfaced is from the LAST attempt across the chain.
    If preset `a` rate-limits and `b` 503s, the agent sees `b`'s
    typed code."""
    from tests.conftest import make_llm_service, make_preset

    FakeRateLimit = type("FakeRateLimit", (Exception,), {})
    FakeUnavailable = type("InternalServerError", (Exception,), {})

    svc = make_llm_service()
    a = make_preset("a", fallback_preset="b")
    b = make_preset("b")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    stub_a = _ClassifyingStubAdapter().push(FakeRateLimit("429"))
    stub_b = _ClassifyingStubAdapter().push(FakeUnavailable("503"))
    _wire_classifying_stub(svc, a, stub_a)
    _wire_classifying_stub(svc, b, stub_b)

    async def _attempt(preset):
        s = stub_a if preset.name == "a" else stub_b
        return s, await s.generate()

    async def _no_sleep(_seconds):  # noqa: ANN001
        return None

    with patch("asyncio.sleep", _no_sleep):
        with pytest.raises(LlmUpstreamError) as exc_info:
            asyncio.run(svc._call_with_fallback(
                preset_name="a", user_level="admin", attempt=_attempt,
            ))

    # Expect b's classification (the last step), NOT a's. The previous
    # version of this assertion used `or`, which allowed a partial
    # regression — one field correct, the other wrong — to slip
    # through. Tighten with `and` plus explicit negatives against a's
    # hint so the test self-documents what's being checked.
    exc = exc_info.value
    assert exc.code == ErrorCode.INTERNAL_ERROR
    assert exc.upstream_class == "InternalServerError"
    # And NOT a's hint.
    assert exc.code != ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert exc.upstream_class != "FakeRateLimit"


def test_call_with_fallback_sleeps_between_retries_using_hint() -> None:
    """When the classifier supplies `retry_after_seconds`, the
    inter-attempt sleep uses that value (capped). Verifies the
    hint flows through compute_backoff correctly."""
    from tests.conftest import make_llm_service, make_preset

    FakeRateLimit = type("FakeRateLimit", (Exception,), {})

    svc = make_llm_service()
    # max_retries=2 → 3 attempts total → 2 sleeps between them.
    p = make_preset("p", max_retries=2)
    svc._register_preset_for_test(p)
    stub = (
        _ClassifyingStubAdapter()
        .push(FakeRateLimit("429"))
        .push(FakeRateLimit("429"))
        .push("OK")
    )
    _wire_classifying_stub(svc, p, stub)

    sleeps: list[float] = []

    async def _record_sleep(seconds):  # noqa: ANN001
        sleeps.append(seconds)

    async def _attempt(preset):
        return stub, await stub.generate()

    with patch("asyncio.sleep", _record_sleep):
        asyncio.run(svc._call_with_fallback(
            preset_name="p", user_level="admin", attempt=_attempt,
        ))

    # Two sleeps between the three attempts. Both honour the
    # classifier's `retry_after_seconds=2.0` (capped at 10s default).
    # R5: provider-supplied Retry-After is now jittered ±20% to
    # avoid cross-worker stampede on shared rate-limit hits, so
    # each sleep is uniformly in [1.6, 2.4].
    assert len(sleeps) == 2
    for s in sleeps:
        assert 1.6 <= s <= 2.4, f"sleep {s} outside jitter window"


def test_call_with_fallback_no_sleep_before_walking_to_fallback() -> None:
    """The inter-attempt sleep applies to RETRIES on the same preset,
    not to the chain-walk transition. Walking to a fallback is a
    code-path switch, not a transient retry."""
    from tests.conftest import make_llm_service, make_preset

    svc = make_llm_service()
    a = make_preset("a", fallback_preset="b")  # max_retries=0 → 1 attempt
    b = make_preset("b")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    stub_a = _ClassifyingStubAdapter().push(RuntimeError("a-fail"))
    stub_b = _ClassifyingStubAdapter().push("OK from b")
    _wire_classifying_stub(svc, a, stub_a)
    _wire_classifying_stub(svc, b, stub_b)

    sleeps: list[float] = []

    async def _record_sleep(seconds):  # noqa: ANN001
        sleeps.append(seconds)

    async def _attempt(preset):
        s = stub_a if preset.name == "a" else stub_b
        return s, await s.generate()

    with patch("asyncio.sleep", _record_sleep):
        asyncio.run(svc._call_with_fallback(
            preset_name="a", user_level="admin", attempt=_attempt,
        ))

    # No sleep — `a` exhausted its single attempt and we walked
    # straight to `b`. No retry window to wait through.
    assert sleeps == []


# ---------------------------------------------------------------------------
# dispatch._run_llm_call surfaces the typed code on chain exhaustion
# ---------------------------------------------------------------------------


def test_dispatch_handles_llm_upstream_error_with_typed_code() -> None:
    """Source-check: `_run_llm_call` catches `LlmUpstreamError` and
    forwards `code` / `retry_after_seconds` / `upstream_class` to the
    `_err_result` builder."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    import inspect

    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    # The exception handler forwards the typed fields from the
    # `LlmUpstreamError` to the result builder.
    assert "except LlmUpstreamError" in src
    assert "exc.code" in src
    assert "exc.retry_after_seconds" in src
    assert "exc.upstream_class" in src
    # And the nested `_err_result` builder declares those parameters
    # (signature lines, not the call sites). `_err_result` is nested
    # inside `_run_llm_call` so its source is included in `src`
    # already — we just assert the parameter declarations are
    # present, which is what makes the kwarg forwarding above
    # type-check at construction time.
    assert "def _err_result" in src
    assert "retry_after_seconds: float | None" in src
    assert "upstream_class: str | None" in src
