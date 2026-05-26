"""Tests for the M6 protocol bump (PR #1 of the implementation sequence).

What this PR landed:
  - Extended `bp_protocol.frames.ErrorCode` with the eight LLM upstream
    classification codes (RESERVED — emitted by PR #2).
  - Added `RETRIABLE_LLM_CODES` frozenset as the single source of
    truth for the code ↔ retriable wire flag mapping.
  - Added `LlmResultError` typed sub-model. Migrated
    `LlmResultFrame.error` from `Optional[dict[str, str]]` to
    `Optional[LlmResultError]`. `retriable` auto-fills from `code` via
    `RETRIABLE_LLM_CODES` when not specified.
  - Added `LlmDeltaMeta` typed sub-model. Added
    `LlmDeltaFrame.meta` field with a mutual-exclusivity validator —
    when `meta` is set, every content field on the frame must be
    None / False.

All RESERVED — no router code emits these yet. PR #2 wires the
classifiers; PR #3 wires the streaming setup-retry.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bp_protocol.frames import (
    RETRIABLE_LLM_CODES,
    ErrorCode,
    LlmDeltaFrame,
    LlmDeltaMeta,
    LlmResultError,
    LlmResultFrame,
)

# ---------------------------------------------------------------------------
# ErrorCode constants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attr,wire", [
    ("LLM_UPSTREAM_TIMEOUT", "upstream_timeout"),
    ("LLM_UPSTREAM_RATE_LIMITED", "upstream_rate_limited"),
    ("LLM_UPSTREAM_UNAVAILABLE", "upstream_unavailable"),
    ("LLM_UPSTREAM_INVALID_REQUEST", "upstream_invalid_request"),
    ("LLM_UPSTREAM_AUTH_FAILED", "upstream_auth_failed"),
    ("LLM_UPSTREAM_CONTENT_FILTER", "upstream_content_filter"),
    ("LLM_UPSTREAM_QUOTA_EXHAUSTED", "upstream_quota_exhausted"),
    ("LLM_STREAM_INTERRUPTED", "stream_interrupted"),
])
def test_new_error_codes_use_design_doc_wire_strings(
    attr: str, wire: str
) -> None:
    """The wire strings come from `docs/design/llm-retriable-errors.md` §3.
    SDK clients across language ecosystems hard-code these — drift
    here breaks every client at once."""
    assert getattr(ErrorCode, attr) == wire


def test_retriable_set_includes_only_transient_classes() -> None:
    """The retriable subset matches the design doc §11.1 + §3 table."""
    assert RETRIABLE_LLM_CODES == frozenset({
        "upstream_timeout",
        "upstream_rate_limited",
        "upstream_unavailable",
        "auth_lookup_failed",
        "internal_error",
    })


def test_retriable_set_excludes_permanent_failures() -> None:
    """Permanent-failure codes from §3 must NOT be in the retriable
    set — retrying them just burns credits / triggers more rate-limit
    storms."""
    for permanent in (
        ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
        ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
        ErrorCode.LLM_UPSTREAM_CONTENT_FILTER,
        ErrorCode.LLM_UPSTREAM_QUOTA_EXHAUSTED,
        ErrorCode.LLM_STREAM_INTERRUPTED,
        ErrorCode.LLM_PRESET_UNKNOWN,
        ErrorCode.LLM_PRESET_NOT_ALLOWED,
    ):
        assert permanent not in RETRIABLE_LLM_CODES, (
            f"{permanent} is marked retriable; review the design doc"
        )


# ---------------------------------------------------------------------------
# LlmResultError typed sub-model
# ---------------------------------------------------------------------------


def test_result_error_auto_derives_retriable_from_code() -> None:
    """Caller sets `code`; `retriable` defaults to whatever the code
    classifies as. Single source of truth = `RETRIABLE_LLM_CODES`."""
    err = LlmResultError(code=ErrorCode.LLM_UPSTREAM_TIMEOUT)
    assert err.retriable is True

    err = LlmResultError(code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST)
    assert err.retriable is False


def test_result_error_auto_derives_retriable_for_internal_error() -> None:
    """§11.1 resolution: `internal_error` stays retriable as the
    catch-all bucket for unknown transients."""
    err = LlmResultError(code=ErrorCode.INTERNAL_ERROR)
    assert err.retriable is True


def test_result_error_explicit_retriable_overrides_auto_derived() -> None:
    """Operators who want a specific `internal_error` flagged
    not-retriable for telemetry can override."""
    err = LlmResultError(
        code=ErrorCode.INTERNAL_ERROR,
        message="permanent assertion failure",
        retriable=False,
    )
    assert err.retriable is False


def test_result_error_unknown_code_defaults_to_not_retriable() -> None:
    """A future code we don't recognise defaults to not-retriable.
    Catches the case where a future router emits a code older SDKs
    don't know about — they default to "permanent" rather than
    burning attempts."""
    err = LlmResultError(code="some_future_unknown_code")
    assert err.retriable is False


def test_result_error_retry_after_seconds_optional() -> None:
    err = LlmResultError(code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED)
    assert err.retry_after_seconds is None
    err2 = LlmResultError(
        code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
        retry_after_seconds=12.5,
    )
    assert err2.retry_after_seconds == 12.5


def test_result_error_extra_fields_ignored_for_forward_compat() -> None:
    """`model_config = {"extra": "ignore"}` lets the router add new
    fields to the error payload without breaking older SDKs.

    This is intentional asymmetry vs. `_FrameBase` (which forbids
    extras): frame fields grow only on a `protocol_version` bump,
    but error-payload fields are additive. A future router shipping
    a new `provider_request_id` MUST NOT `ValidationError` on every
    in-the-wild SDK at parse time — see design doc §9 on backwards
    compat.
    """
    err = LlmResultError(
        code=ErrorCode.INTERNAL_ERROR,
        message="boom",
        mystery_field="ignored",  # type: ignore[call-arg]
    )
    # Known fields populate normally.
    assert err.code == ErrorCode.INTERNAL_ERROR
    assert err.message == "boom"
    # Unknown field is dropped, not preserved on the model. Old SDKs
    # never see it; new SDKs that DO know about it would have it
    # declared as a real field.
    assert not hasattr(err, "mystery_field")


def test_result_frame_round_trip_with_unknown_error_payload_field() -> None:
    """End-to-end forward-compat: a router-shaped JSON `LlmResultFrame`
    carrying an unknown key inside `error` parses cleanly through the
    discriminated union. Catches the regression where `extra=forbid`
    on `LlmResultError` would `ValidationError` the entire frame."""
    import json

    from bp_protocol.frames import parse_frame

    wire = {
        "type": "LlmResult",
        "agent_id": "router",
        "trace_id": "tr",
        "span_id": "sp",
        "ref_correlation_id": "r1",
        "error": {
            "code": ErrorCode.LLM_UPSTREAM_TIMEOUT,
            "message": "boom",
            "retry_after_seconds": 1.5,
            # Field a future router version might add.
            "provider_request_id": "req-abc-123",
        },
    }
    frame = parse_frame(json.dumps(wire))
    # Frame parsed; `error` is the typed sub-model with known fields
    # populated and the unknown one silently dropped.
    assert frame.error is not None
    assert frame.error.code == ErrorCode.LLM_UPSTREAM_TIMEOUT
    assert frame.error.retry_after_seconds == 1.5
    assert not hasattr(frame.error, "provider_request_id")


def test_delta_meta_extra_fields_ignored_for_forward_compat() -> None:
    """Same forward-compat policy on `LlmDeltaMeta` — a future router
    shipping a new field on the streaming retry-pending hint must not
    break older agents."""
    meta = LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=0.5,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
        progress_text="retrying",  # type: ignore[call-arg]
    )
    assert meta.kind == "retry_pending"
    assert meta.attempt == 1
    assert not hasattr(meta, "progress_text")


def test_result_error_full_wire_round_trip_via_pydantic() -> None:
    """Coverage gap: assert all `LlmResultError` fields survive a
    full Pydantic → JSON → parse round-trip end-to-end. Catches a
    regression where the typed sub-model's default behaviour
    diverges between construction and deserialization."""

    from bp_protocol.frames import parse_frame

    original = LlmResultFrame(
        agent_id="router",
        trace_id="tr",
        span_id="sp",
        ref_correlation_id="r1",
        error=LlmResultError(
            code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
            message="boom",
            retry_after_seconds=2.5,
            upstream_class="RateLimitError",
        ),
    )
    # Serialize via Pydantic the same way the router does on the wire.
    payload = original.model_dump_json()
    parsed = parse_frame(payload)
    assert parsed.error is not None
    assert parsed.error.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    assert parsed.error.message == "boom"
    assert parsed.error.retry_after_seconds == 2.5
    assert parsed.error.upstream_class == "RateLimitError"
    # `retriable` was None on construction — auto-derived from the
    # code via the `_derive_retriable` validator. Verify it survives
    # serialization and re-parsing.
    assert parsed.error.retriable is True


def test_delta_meta_full_wire_round_trip_via_pydantic() -> None:
    """Same end-to-end check for `LlmDeltaMeta` on a `LlmDeltaFrame`.
    The mutex-with-content invariant is enforced on construction,
    so the round-trip needs to preserve it AND the meta fields
    themselves must come back with the right types (e.g.
    `attempt: int`, not coerced to float)."""

    from bp_protocol.frames import LlmDeltaFrame, parse_frame

    original = LlmDeltaFrame(
        agent_id="router",
        trace_id="tr",
        span_id="sp",
        ref_correlation_id="r1",
        meta=LlmDeltaMeta(
            kind="retry_pending",
            attempt=2,
            max_attempts=3,
            retry_after_seconds=4.5,
            reason_code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
        ),
    )
    parsed = parse_frame(original.model_dump_json())
    assert parsed.meta is not None
    assert parsed.meta.kind == "retry_pending"
    assert parsed.meta.attempt == 2
    assert parsed.meta.max_attempts == 3
    assert parsed.meta.retry_after_seconds == 4.5
    assert parsed.meta.reason_code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    # All content fields must be None per the mutex invariant.
    assert parsed.text is None
    assert parsed.tool_call is None
    assert parsed.finish_reason is None


# ---------------------------------------------------------------------------
# LlmResultFrame.error backwards compatibility
# ---------------------------------------------------------------------------


def test_result_frame_accepts_dict_for_error_field_legacy_callers() -> None:
    """Existing call sites construct `LlmResultFrame(error={"code": ...,
    "message": ...})`. Pydantic v2 coerces the dict into
    `LlmResultError` automatically — we verify that path stays open."""
    frame = LlmResultFrame(
        type="LlmResult",
        trace_id="trc",
        span_id="spn",
        agent_id="router",
        ref_correlation_id="corr_1",
        error={"code": ErrorCode.INTERNAL_ERROR, "message": "boom"},
    )
    assert isinstance(frame.error, LlmResultError)
    assert frame.error.code == ErrorCode.INTERNAL_ERROR
    assert frame.error.message == "boom"
    assert frame.error.retriable is True


def test_result_frame_accepts_typed_error_instance() -> None:
    err = LlmResultError(
        code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
        message="429",
        retry_after_seconds=5.0,
    )
    frame = LlmResultFrame(
        type="LlmResult",
        trace_id="trc",
        span_id="spn",
        agent_id="router",
        ref_correlation_id="corr_1",
        error=err,
    )
    assert frame.error is err


def test_result_frame_serialises_error_with_new_fields() -> None:
    """Wire shape: `model_dump()` produces the JSON form, including
    the new optional fields. Old SDKs reading `error["code"]` and
    `error["message"]` keep working — those keys are still present."""
    frame = LlmResultFrame(
        type="LlmResult",
        trace_id="trc",
        span_id="spn",
        agent_id="router",
        ref_correlation_id="corr_1",
        error=LlmResultError(
            code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
            message="429",
            retry_after_seconds=5.0,
        ),
    )
    dumped = frame.model_dump()
    assert dumped["error"]["code"] == "upstream_rate_limited"
    assert dumped["error"]["message"] == "429"
    assert dumped["error"]["retriable"] is True
    assert dumped["error"]["retry_after_seconds"] == 5.0


# ---------------------------------------------------------------------------
# LlmDeltaMeta typed sub-model
# ---------------------------------------------------------------------------


def test_delta_meta_kind_must_be_retry_pending() -> None:
    """Only `kind="retry_pending"` is defined today. Other values
    are reserved — adding a new kind is a protocol bump."""
    LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=2.0,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
    )
    with pytest.raises(ValidationError):
        LlmDeltaMeta(
            kind="something_else",  # type: ignore[arg-type]
            attempt=1,
            max_attempts=3,
            retry_after_seconds=2.0,
            reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
        )


def test_delta_meta_attempt_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        LlmDeltaMeta(
            kind="retry_pending",
            attempt=0,
            max_attempts=3,
            retry_after_seconds=1.0,
            reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
        )


def test_delta_meta_attempt_cannot_exceed_max_attempts() -> None:
    """Validator catches a malformed meta where the just-failed
    attempt is reported beyond the configured cap — guards against
    a router-side off-by-one when emitting the hint."""
    with pytest.raises(ValidationError, match="cannot exceed"):
        LlmDeltaMeta(
            kind="retry_pending",
            attempt=4,
            max_attempts=3,
            retry_after_seconds=1.0,
            reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
        )


def test_delta_meta_retry_after_seconds_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        LlmDeltaMeta(
            kind="retry_pending",
            attempt=1,
            max_attempts=3,
            retry_after_seconds=-0.5,
            reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
        )


# ---------------------------------------------------------------------------
# LlmDeltaFrame.meta mutual-exclusivity validator
# ---------------------------------------------------------------------------


def _meta_delta() -> LlmDeltaMeta:
    return LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=2.0,
        reason_code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
    )


def _frame_kwargs() -> dict:
    return {
        "type": "LlmDelta",
        "trace_id": "trc",
        "span_id": "spn",
        "agent_id": "router",
        "ref_correlation_id": "corr",
    }


def test_delta_frame_with_meta_only_passes() -> None:
    """A pure status-hint delta — meta set, every content field None /
    False — is the canonical retry_pending shape."""
    frame = LlmDeltaFrame(**_frame_kwargs(), meta=_meta_delta())
    assert frame.meta is not None
    assert frame.text is None
    assert frame.tool_call is None


def test_delta_frame_without_meta_can_carry_content() -> None:
    """Content deltas (the existing common case) keep working unchanged."""
    frame = LlmDeltaFrame(**_frame_kwargs(), text="Hello")
    assert frame.meta is None
    assert frame.text == "Hello"


@pytest.mark.parametrize("content_kwarg", [
    {"text": "boom"},
    {"tool_call": {"id": "c1", "name": "f", "args": {}}},
    {"finish_reason": "stop"},
    {"usage": {"input_tokens": 1, "output_tokens": 1}},
    {"thought_signature": "abc"},
    {"reasoning_block": {"type": "thinking"}},
    {"thought": True},
])
def test_delta_frame_meta_with_any_content_field_rejected(
    content_kwarg: dict,
) -> None:
    """Mutual-exclusivity invariant: meta + any content field in the
    same frame is a validation error. Each content field tested
    individually so the failure message names the offender."""
    with pytest.raises(ValidationError, match="content fields must"):
        LlmDeltaFrame(
            **_frame_kwargs(),
            meta=_meta_delta(),
            **content_kwarg,
        )


def test_delta_frame_meta_with_multiple_content_fields_lists_all_offenders() -> None:
    """When several content fields slip past, the validator names
    every one — easier to debug than a one-at-a-time error stream."""
    try:
        LlmDeltaFrame(
            **_frame_kwargs(),
            meta=_meta_delta(),
            text="x",
            finish_reason="stop",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    except ValidationError as exc:
        msg = str(exc)
        # All three offenders should appear in the same error message.
        assert "text" in msg
        assert "finish_reason" in msg
        assert "usage" in msg
    else:
        pytest.fail("expected ValidationError")
