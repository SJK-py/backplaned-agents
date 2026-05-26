"""Shared helpers for formatting validation errors on the wire.

Pydantic `ValidationError` messages CAN echo input fragments
verbatim ("String should match pattern '...'", or custom validator
messages like `Value error, 'BIG STRING...'`). A misbehaving sender
that pumps an oversized payload into a wire-frame validator
would otherwise see that fragment reflected back in the
Ack/Error.reason, giving them an oracle.

`safe_validator_message` returns a bounded message — capped at
200 chars by default — so a hostile sender can't squeeze more
out of the validator than necessary for legitimate debugging.
"""

from __future__ import annotations

import unicodedata

from pydantic import ValidationError

_DEFAULT_MAX_LEN = 200


def safe_validator_message(
    exc: ValidationError, *, max_len: int = _DEFAULT_MAX_LEN
) -> str:
    """Format the first validator error message, bounded by max_len.

    Returns "validation failed" if the ValidationError has no
    surfaceable errors[0] entry (shouldn't happen with normal
    Pydantic v2 usage, but defends against custom-validator
    constructions that don't follow the standard shape).

    Truncation backs off past trailing combining marks and
    ZWJ (`\\u200d`) characters so the visible output isn't
    mid-cluster garbage. This matters when a custom validator
    formats user input containing emoji ZWJ sequences or
    combining-mark scripts (Devanagari, Thai, ...) — the security
    bound still holds at max_len, but the rendering is clean.
    """
    try:
        msg = str(exc.errors()[0]["msg"])
    except (IndexError, KeyError, TypeError):
        return "validation failed"
    if len(msg) <= max_len:
        return msg
    # NFC normalise first: precomposes decomposable combining
    # forms (`é` = `e` + combining acute) into single codepoints.
    # Reduces the number of cases where the slice would land
    # mid-cluster.
    msg = unicodedata.normalize("NFC", msg)
    # Slice budget: max_len - 1 to leave room for the ellipsis.
    end = max_len - 1
    # Back off past trailing ZWJ (`‍`) or combining-mark
    # codepoints (Unicode category `Mn` / `Mc` / `Me`). A ZWJ
    # at the slice boundary signals an emoji-joiner sequence;
    # combining marks at the boundary signal a decomposed
    # grapheme. Both are visually fragmented if truncated mid-
    # sequence. Bound the back-off so a pathological string of
    # combining marks can't loop the entire message away.
    backoff_cap = min(8, end)
    while end > 0 and backoff_cap > 0:
        ch = msg[end - 1]
        if ch == "‍" or unicodedata.category(ch) in {"Mn", "Mc", "Me"}:
            end -= 1
            backoff_cap -= 1
            continue
        break
    return msg[:end] + "…"
