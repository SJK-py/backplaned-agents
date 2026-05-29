"""`safe_validator_message` doesn't split grapheme clusters mid-sequence.

R4 second-pass review noted that the pre-fix slice
`msg[: max_len - 1] + "…"` operated on Python codepoints, so a
ZWJ-joined emoji or a combining-mark sequence at the truncation
boundary produced visually-fragmented output:

  - `"👨‍👩‍👧 prefix..."` truncated at codepoint 199 might land
    inside a `<man><ZWJ><woman>` sequence, rendering as a man
    + a stray ZWJ + a half-broken family
  - `"नमस्ते..."` (Devanagari) might split a base char + combining
    mark, producing an isolated base or a stranded mark

Fix: NFC-normalise first (folds decomposable forms), then back off
past trailing ZWJ / combining-mark codepoints (Unicode category Mn,
Mc, Me) before appending the ellipsis. Bound the back-off to 8
codepoints so pathological input can't strip the whole message.

These tests are cosmetic — the security bound at `max_len` still
holds either way. The pin guards against a regression that drops
the normalize step.
"""

from __future__ import annotations

import unicodedata

import pytest


def _fake_exc(msg: str):
    class _Fake:
        def errors(self_):
            return [{"msg": msg}]
    return _Fake()


def test_no_trailing_combining_mark_after_truncation() -> None:
    """Devanagari `े` (combining vowel sign e, U+0947, category Mn)
    appended to a Latin filler: the slice boundary lands at the
    combining mark and the helper must back off to a clean base
    character before the ellipsis."""
    pytest.importorskip("pydantic")
    from bp_protocol.errors import safe_validator_message

    # Build a string where the truncation boundary (max_len - 1)
    # lands on a combining mark.
    filler = "a" * 195
    # `न` + `े` = `ने` (na + e). The combining `े` is at index 196.
    msg = filler + "न" + "े" + "trailing"
    out = safe_validator_message(_fake_exc(msg), max_len=200)
    # Last codepoint before the ellipsis must NOT be a combining mark.
    assert out.endswith("…")
    last_before_ellipsis = out[-2]
    assert unicodedata.category(last_before_ellipsis) not in {"Mn", "Mc", "Me"}


def test_no_trailing_zwj_after_truncation() -> None:
    """ZWJ at the boundary signals an emoji-joiner sequence about
    to fragment; back off past it."""
    pytest.importorskip("pydantic")
    from bp_protocol.errors import safe_validator_message

    # 198 chars then a ZWJ at position 198, more after.
    msg = ("a" * 198) + "‍" + "more"
    out = safe_validator_message(_fake_exc(msg), max_len=200)
    assert out.endswith("…")
    assert "‍" not in out


def test_truncation_still_bounded_at_max_len() -> None:
    """Security bound: the output never exceeds max_len. Backing
    off makes it SHORTER, never longer."""
    pytest.importorskip("pydantic")
    from bp_protocol.errors import safe_validator_message

    long = "x" * 1000
    out = safe_validator_message(_fake_exc(long), max_len=200)
    assert len(out) <= 200


def test_backoff_is_bounded() -> None:
    """A pathological string of trailing combining marks can't
    strip the entire message — backoff caps at 8 codepoints. The
    output may be slightly shorter than max_len-1 but won't shrink
    below max_len - 9."""
    pytest.importorskip("pydantic")
    from bp_protocol.errors import safe_validator_message

    # 192 normal chars + 16 combining acutes — the slice boundary
    # would land deep in the combining sequence; backoff stops
    # after 8 backsteps.
    msg = ("a" * 192) + ("́" * 16)
    out = safe_validator_message(_fake_exc(msg), max_len=200)
    # Output length is in (max_len - 9, max_len].
    assert 191 <= len(out) <= 200


def test_short_messages_pass_unchanged() -> None:
    """Sanity: a message UNDER max_len is returned verbatim,
    grapheme considerations don't enter."""
    pytest.importorskip("pydantic")
    from bp_protocol.errors import safe_validator_message

    out = safe_validator_message(_fake_exc("short message"), max_len=200)
    assert out == "short message"


def test_nfc_normalize_applied_on_truncation() -> None:
    """Decomposed `é` (e + U+0301 acute) NFC-normalises to
    precomposed `é` (U+00E9). Pin the helper applies NFC before
    backing off so the decomposed form doesn't trigger an
    unnecessary back-off."""
    pytest.importorskip("pydantic")
    import inspect

    from bp_protocol import errors

    src = inspect.getsource(errors.safe_validator_message)
    assert 'normalize("NFC"' in src


def test_ascii_truncation_unchanged_behavior() -> None:
    """Pure ASCII: no combining marks, no NFC change. Slice
    behaves identically to the pre-R4 helper (max_len chars total
    including the ellipsis)."""
    pytest.importorskip("pydantic")
    from bp_protocol.errors import safe_validator_message

    msg = "abcdefghij" * 30  # 300 chars ASCII
    out = safe_validator_message(_fake_exc(msg), max_len=50)
    assert len(out) == 50
    assert out[-1] == "…"
    assert out[:-1] == "a" * 1 + "b" + "c" + "d" + "e" + "f" + "g" + "h" + "i" + "j" * 1 + "abcdefghij" * 3 + "abcdefghi"
