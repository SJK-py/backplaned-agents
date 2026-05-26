"""`AckFrame.reason` bounded at 256 chars + defensive slice at
the reflection site.

R8 fourth-pass review (HIGH): tasks.py:626 reflected the
destination agent's `ack.reason` verbatim into the calling
agent's Ack. The `AckFrame.reason` field was unbounded — a
malicious / buggy destination could return a 1 MB reason string,
which the router faithfully forwarded to the caller, flooding
their WS outbox and amplifying the attack.

R8 fix (two layers):
  1. Protocol layer: `AckFrame.reason` is now
     `Field(default=None, max_length=256)`. Any inbound frame
     with a longer reason fails Pydantic validation at
     `parse_frame` time → can't even enter the dispatch layer.
  2. Defensive layer: `tasks.py:_handle_new_task` slices to 240
     chars before constructing the reflected AdmitError. This
     covers the case where an older client somehow has a longer
     reason (unlikely post-R8 but cheap belt-and-braces).
"""

from __future__ import annotations

import inspect

import pytest


def test_ack_frame_reason_max_length_at_protocol() -> None:
    """Pydantic-level: an AckFrame with reason > 256 chars
    fails validation at construction."""
    pytest.importorskip("pydantic")
    from pydantic import ValidationError

    from bp_protocol.frames import AckFrame

    # Boundary: 256 chars accepted.
    ok = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="c1",
        accepted=False,
        reason="x" * 256,
    )
    assert ok.reason and len(ok.reason) == 256

    # One over: rejected.
    with pytest.raises(ValidationError):
        AckFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
            ref_correlation_id="c1",
            accepted=False,
            reason="x" * 257,
        )


def test_ack_frame_reason_none_passes() -> None:
    pytest.importorskip("pydantic")
    from bp_protocol.frames import AckFrame

    ok = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="c1",
        accepted=True,
        reason=None,
    )
    assert ok.reason is None


def test_ack_frame_reason_empty_string_passes() -> None:
    """Empty string is distinct from None and should pass the
    length check."""
    pytest.importorskip("pydantic")
    from bp_protocol.frames import AckFrame

    ok = AckFrame(
        agent_id="router",
        trace_id="0" * 32,
        span_id="0" * 16,
        ref_correlation_id="c1",
        accepted=False,
        reason="",
    )
    assert ok.reason == ""


def test_admit_task_slices_reflection() -> None:
    """Source pin: `admit_task` slices the destination's
    `ack.reason` to 240 chars before reflecting it into the
    caller's AdmitError / Ack. Pre-R8 it passed the unbounded
    string straight through."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    src = inspect.getsource(tasks.admit_task)
    # The bound is explicit.
    assert "[:240]" in src
    assert "bounded_reason" in src


def test_ack_frame_max_length_value_pinned() -> None:
    """Source pin: `max_length=256` on AckFrame.reason. R8
    chose 256 to match the R2 `safe_validator_message` bound for
    consistency across the wire-frame error-message surface. A
    future refactor that loosens this without a security review
    is a regression."""
    pytest.importorskip("pydantic")
    from bp_protocol.frames import AckFrame

    # Pydantic v2 stores the constraint as the field's metadata.
    field = AckFrame.model_fields["reason"]
    # Walk the metadata for a length constraint.
    max_len = None
    for meta in getattr(field, "metadata", []):
        if hasattr(meta, "max_length"):
            max_len = meta.max_length
            break
    assert max_len == 256
