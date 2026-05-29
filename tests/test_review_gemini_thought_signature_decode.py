"""Gemini `thought_signature` round-trips correctly.

R8 fourth-pass review (CRITICAL): `_decode_signature` was defined
in the module but never called. `_convert_part` passed the
base64 STRING form (`_encode_signature` output) straight back to
the Gemini SDK on multi-turn function-call sequences. The SDK's
part-field for `thought_signature` expects raw bytes; passing a
string either silently drops the field or coerces it into a
malformed encrypted blob. Either way, Gemini 3 multi-turn tool
use breaks: the second turn 400s because the upstream model
can't verify the (corrupted / missing) thought continuity.

R8 fix: in `_convert_part`, when a part carries
`thought_signature`, decode it via the (now-actually-called)
`_decode_signature` helper before handing back to the SDK.
"""

from __future__ import annotations

import base64
import inspect


def test_convert_part_decodes_string_signature_to_bytes() -> None:
    """The bug: a `thought_signature` arriving as a base64 string
    must be decoded to bytes before being passed to the SDK."""
    
    from bp_router.llm.providers.gemini import _convert_part

    raw_bytes = b"\x00\x01\x02\x03\xff\xfe"
    encoded = base64.b64encode(raw_bytes).decode("ascii")

    part = {
        "function_call": {"name": "tool_x", "args": {}},
        "thought_signature": encoded,
    }
    out = _convert_part(part)
    assert isinstance(out["thought_signature"], bytes)
    assert out["thought_signature"] == raw_bytes


def test_convert_part_passes_bytes_signature_unchanged() -> None:
    """Idempotent: a part whose `thought_signature` is already
    bytes (e.g. came directly from the SDK, never went through
    wire encoding) passes through unchanged."""
    
    from bp_router.llm.providers.gemini import _convert_part

    raw_bytes = b"unchanged"
    part = {
        "function_call": {"name": "tool_x", "args": {}},
        "thought_signature": raw_bytes,
    }
    out = _convert_part(part)
    assert out["thought_signature"] == raw_bytes


def test_convert_part_drops_unparseable_signature() -> None:
    """Malformed base64 → drop the field rather than pass None.
    The SDK treats `thought_signature=None` and missing-key
    differently in some versions; missing is the safer null
    state."""
    
    from bp_router.llm.providers.gemini import _convert_part

    part = {
        "function_call": {"name": "tool_x", "args": {}},
        "thought_signature": "@@@ not base64 @@@",
    }
    out = _convert_part(part)
    assert "thought_signature" not in out


def test_convert_part_preserves_other_fields() -> None:
    """The signature decode must not disturb the rest of the
    part (function_call, args, etc.)."""
    
    from bp_router.llm.providers.gemini import _convert_part

    raw_bytes = b"\xab\xcd"
    encoded = base64.b64encode(raw_bytes).decode("ascii")

    part = {
        "function_call": {"name": "search_web", "args": {"q": "x"}},
        "thought_signature": encoded,
        "extra_field": "kept",
    }
    out = _convert_part(part)
    assert out["function_call"] == {"name": "search_web", "args": {"q": "x"}}
    assert out["extra_field"] == "kept"
    assert out["thought_signature"] == raw_bytes


def test_convert_part_text_part_without_signature_passes_through() -> None:
    """Sanity: a text part with no signature doesn't get a None
    signature injected by the new decode path."""
    
    from bp_router.llm.providers.gemini import _convert_part

    part = {"text": "hello"}
    out = _convert_part(part)
    assert out == {"text": "hello"}
    assert "thought_signature" not in out


def test_source_pin_decode_signature_is_called() -> None:
    """Source pin: `_convert_part` calls `_decode_signature`. The
    pre-R8 bug was specifically that the function was defined but
    never called — this test fails immediately on a regression
    that drops the call again."""
    
    from bp_router.llm.providers import gemini

    src = inspect.getsource(gemini._convert_part)
    assert "_decode_signature(" in src


def test_decode_signature_helper_round_trips() -> None:
    """Round-trip pin: encode(decode(x)) == x for bytes; ensures
    the wire shape we send + receive is symmetric."""
    
    from bp_router.llm.providers.gemini import (
        _decode_signature,
        _encode_signature,
    )

    raw = b"\x00\x01\xff\xfe arbitrary signature bytes \xde\xad\xbe\xef"
    encoded = _encode_signature(raw)
    assert isinstance(encoded, str)
    decoded = _decode_signature(encoded)
    assert decoded == raw
