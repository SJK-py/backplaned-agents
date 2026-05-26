"""Tests for the SDK image_part helper and the Gemini adapter's
neutral → native part translation. Pure unit tests; no Postgres or
network."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from bp_router.llm.providers.gemini import _convert_part
from bp_router.llm.service import Message
from bp_sdk.llm import image_part

# ---------------------------------------------------------------------------
# image_part — input handling
# ---------------------------------------------------------------------------


def test_image_part_from_bytes() -> None:
    raw = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    part = image_part(raw, mime_type="image/png")
    assert part == {
        "image": {
            "mime_type": "image/png",
            "data": base64.b64encode(raw).decode("ascii"),
        }
    }


def test_image_part_bytes_requires_mime_type() -> None:
    with pytest.raises(ValueError, match="mime_type is required"):
        image_part(b"\x00\x01")


def test_image_part_from_path_infers_mime(tmp_path: Path) -> None:
    p = tmp_path / "shot.jpg"
    payload = b"\xff\xd8\xff\xe0fake-jpeg"
    p.write_bytes(payload)
    part = image_part(p)
    assert part["image"]["mime_type"] == "image/jpeg"
    assert base64.b64decode(part["image"]["data"]) == payload


def test_image_part_from_str_path(tmp_path: Path) -> None:
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    part = image_part(str(p))
    assert part["image"]["mime_type"] == "image/png"


def test_image_part_explicit_mime_overrides_inference(tmp_path: Path) -> None:
    p = tmp_path / "blob.dat"
    p.write_bytes(b"\x00")
    part = image_part(p, mime_type="image/webp")
    assert part["image"]["mime_type"] == "image/webp"


def test_image_part_path_unknown_extension_requires_mime(tmp_path: Path) -> None:
    # Pick a name with no extension at all — `mimetypes.guess_type` is
    # surprisingly liberal about made-up extensions (`.xyz` returns
    # `chemical/x-xyz` on some systems).
    p = tmp_path / "blob"
    p.write_bytes(b"\x00")
    with pytest.raises(ValueError, match="could not infer mime_type"):
        image_part(p)


def test_image_part_rejects_bad_source_type() -> None:
    with pytest.raises(TypeError):
        image_part(12345, mime_type="image/png")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gemini adapter — neutral → native part translation
# ---------------------------------------------------------------------------


def test_convert_part_image_neutral_to_inline_data() -> None:
    neutral = image_part(b"hello", mime_type="image/png")
    native = _convert_part(neutral)
    assert native == {
        "inline_data": {
            "mime_type": "image/png",
            "data": base64.b64encode(b"hello").decode("ascii"),
        }
    }


def test_convert_part_text_passes_through() -> None:
    part = {"text": "describe this"}
    assert _convert_part(part) is part


def test_convert_part_native_inline_data_passes_through() -> None:
    """Agent code that already builds Gemini-native parts shouldn't be
    rewritten by the adapter."""
    part = {"inline_data": {"mime_type": "image/jpeg", "data": "..."}}
    assert _convert_part(part) is part


def test_convert_part_file_data_passes_through() -> None:
    """Gemini File API references — opaque to the SDK, agent's
    responsibility — also pass through unchanged."""
    part = {"file_data": {"file_uri": "files/abc", "mime_type": "video/mp4"}}
    assert _convert_part(part) is part


def test_convert_part_drops_anthropic_thinking_blocks() -> None:
    """Cross-provider portability: when the SDK round-trips an
    Anthropic response through the Gemini adapter, the Anthropic-only
    `{"type": "thinking"}` and `{"type": "redacted_thinking"}` parts
    must be dropped — Gemini doesn't recognise them and would 400."""
    assert _convert_part({"type": "thinking", "thinking": "...", "signature": "..."}) == {}
    assert _convert_part({"type": "redacted_thinking", "data": "..."}) == {}


def test_convert_part_image_missing_fields_uses_defaults() -> None:
    """Defensive: don't crash on a malformed image part — emit native
    shape with safe fallbacks so the upstream provider gives the real
    error."""
    part = _convert_part({"image": {}})
    assert part == {
        "inline_data": {"mime_type": "application/octet-stream", "data": ""}
    }


# ---------------------------------------------------------------------------
# End-to-end shape — Message → adapter → Gemini contents
# ---------------------------------------------------------------------------


def test_multipart_message_translates_through_adapter() -> None:
    """A user message with text + image_part should produce a Gemini
    contents entry with two parts, the second being inline_data."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    adapter = GeminiAdapter(concrete_model="gemini-2.5-flash", api_key="x")
    msg = Message(
        role="user",
        content=[
            {"text": "what is this?"},
            image_part(b"\x00\x01\x02", mime_type="image/png"),
        ],
    )
    contents, system = adapter._convert_messages([msg])
    assert system is None
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "what is this?"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"\x00\x01\x02"
