"""Tests for multimodal tool-result rendering across provider adapters.

The neutral envelope produced by `bp_sdk.image_part()` is symmetric:
it works for both user-message input AND tool-response output. Each
provider adapter renders the parts natively:

  * Gemini  → `function_response.parts[].inline_data`
  * Anthropic → `tool_result.content[]` with `image` blocks
  * OpenAI Responses → `function_call_output` text stub + synthesized
    follow-up `user` message with `input_image` data-URLs
  * OpenAI-compatible → `tool` message text stub + synthesized
    follow-up `user` message with `image_url` data-URLs
"""

from __future__ import annotations

import base64
import json

import pytest

_PNG_BASE64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")


def _neutral_image_part(*, display_name: str | None = None) -> dict:
    img = {"mime_type": "image/png", "data": _PNG_BASE64}
    if display_name is not None:
        img["display_name"] = display_name
    return {"image": img}


# ---------------------------------------------------------------------------
# image_part() — display_name plumbed through to envelope
# ---------------------------------------------------------------------------


def test_image_part_with_explicit_display_name() -> None:
    from bp_sdk.llm import image_part

    part = image_part(b"\x89PNG\r\n", mime_type="image/png", display_name="hero.png")
    assert part["image"]["display_name"] == "hero.png"


def test_image_part_defaults_display_name_to_basename(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bp_sdk.llm import image_part

    p = tmp_path / "diagram.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    part = image_part(p)
    assert part["image"]["display_name"] == "diagram.jpg"


def test_image_part_bytes_omits_display_name_when_not_set() -> None:
    from bp_sdk.llm import image_part

    part = image_part(b"\x89PNG\r\n", mime_type="image/png")
    assert "display_name" not in part["image"]


# ---------------------------------------------------------------------------
# Message.tool_response widened to accept list[dict]
# ---------------------------------------------------------------------------


def test_tool_response_accepts_list_for_multimodal() -> None:
    from bp_sdk.llm import Message

    msg = Message.tool_response(
        tool_call_id="c1",
        name="screenshot",
        response=[{"text": "ok"}, _neutral_image_part(display_name="s.png")],
    )
    assert msg.role == "tool"
    assert isinstance(msg.content, list)
    assert msg.content[1]["image"]["display_name"] == "s.png"


# ---------------------------------------------------------------------------
# Anthropic — tool_result.content carries native image blocks
# ---------------------------------------------------------------------------


def test_anthropic_tool_result_converts_neutral_image_to_native() -> None:
    from bp_router.llm.providers.anthropic import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "Here's the chart."},
            _neutral_image_part(display_name="chart.png"),
        ],
        tool_call_id="toolu_1",
        name="render_chart",
    )
    out, _system = _convert_messages([msg])
    # tool messages are folded into a user-role message carrying
    # tool_result blocks.
    assert len(out) == 1
    assert out[0]["role"] == "user"
    blocks = out[0]["content"]
    assert len(blocks) == 1
    tr = blocks[0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "toolu_1"
    content = tr["content"]
    assert content[0] == {"type": "text", "text": "Here's the chart."}
    image_block = content[1]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == _PNG_BASE64


def test_anthropic_tool_result_string_path_unchanged() -> None:
    from bp_router.llm.providers.anthropic import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(role="tool", content="OK", tool_call_id="t1", name="echo")
    out, _ = _convert_messages([msg])
    tr = out[0]["content"][0]
    assert tr["content"] == "OK"


# ---------------------------------------------------------------------------
# Gemini — function_response.parts carries inline_data
# ---------------------------------------------------------------------------


def test_gemini_tool_result_emits_inline_data_part() -> None:
    from bp_router.llm.providers.gemini import GeminiAdapter
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "rendered"},
            _neutral_image_part(display_name="chart.png"),
        ],
        tool_call_id="call_1",
        name="render",
    )
    adapter = GeminiAdapter(concrete_model="gemini-3-flash-preview", api_key="x")
    contents, _system = adapter._convert_messages([msg])
    # Tool message becomes a single contents entry whose parts holds
    # a function_response.
    tool_entry = contents[-1]
    assert tool_entry["role"] == "tool"
    fr_part = tool_entry["parts"][0]
    fr = fr_part["function_response"]
    assert fr["name"] == "render"
    assert fr["id"] == "call_1"
    # Native parts array carries the inline_data.
    parts = fr["parts"]
    assert parts[0] == {"text": "rendered"}
    image_part_native = parts[1]
    assert image_part_native["inline_data"]["mime_type"] == "image/png"
    assert image_part_native["inline_data"]["data"] == _PNG_BASE64
    assert image_part_native["inline_data"]["display_name"] == "chart.png"
    # Response field carries a $ref keyed on display_name.
    assert fr["response"]["result"] == {"chart.png": {"$ref": "chart.png"}}


def test_gemini_tool_result_string_path_unchanged() -> None:
    from bp_router.llm.providers.gemini import GeminiAdapter
    from bp_router.llm.service import Message

    msg = Message(role="tool", content="OK", tool_call_id="c", name="t")
    adapter = GeminiAdapter(concrete_model="gemini-3-flash-preview", api_key="x")
    contents, _ = adapter._convert_messages([msg])
    fr = contents[-1]["parts"][0]["function_response"]
    assert fr["response"] == {"result": "OK"}
    assert "parts" not in fr


# ---------------------------------------------------------------------------
# OpenAI Responses — function_call_output stub + follow-up user message
# ---------------------------------------------------------------------------


def test_openai_responses_tool_result_synthesises_user_image_message() -> None:
    from bp_router.llm.providers.openai import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "see image"},
            _neutral_image_part(),
        ],
        tool_call_id="call_xyz",
        name="screenshot",
    )
    items, _ = _convert_messages([msg])
    # Two items: function_call_output (text-only) + synthesised user.
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_xyz"
    assert "binary payload from screenshot" in items[0]["output"]
    follow_up = items[1]
    assert follow_up["role"] == "user"
    parts = follow_up["content"]
    assert parts[0] == {"type": "input_text", "text": "see image"}
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")


def test_openai_responses_tool_result_string_path_unchanged() -> None:
    from bp_router.llm.providers.openai import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(role="tool", content="ok", tool_call_id="c", name="t")
    items, _ = _convert_messages([msg])
    assert len(items) == 1
    assert items[0]["type"] == "function_call_output"
    assert items[0]["output"] == "ok"


# ---------------------------------------------------------------------------
# OpenAI-compat — tool message text stub + follow-up user message
# ---------------------------------------------------------------------------


def test_openai_compat_tool_result_synthesises_user_image_message() -> None:
    from bp_router.llm.providers.openai_compatible import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "see image"},
            _neutral_image_part(),
        ],
        tool_call_id="call_xyz",
        name="snap",
    )
    out = _convert_messages([msg])
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "call_xyz"
    assert "binary payload from snap" in out[0]["content"]
    follow_up = out[1]
    assert follow_up["role"] == "user"
    parts = follow_up["content"]
    assert parts[0] == {"type": "text", "text": "see image"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_compat_tool_result_string_path_unchanged() -> None:
    from bp_router.llm.providers.openai_compatible import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(role="tool", content="ok", tool_call_id="c", name="t")
    out = _convert_messages([msg])
    assert out == [{"role": "tool", "tool_call_id": "c", "content": "ok"}]


# ===========================================================================
# document_part() — sibling helper, document round-trips
# ===========================================================================
#
# Documents (PDF, plain text) follow the same neutral-envelope path as
# images but use a distinct discriminator key. Each adapter renders
# natively:
#
#   * Gemini  → `inline_data` (same shape as image; MIME drives interp)
#   * Anthropic → `type: document` block (distinct from `type: image`)
#   * OpenAI Responses → `input_file` with base64 data URL
#   * OpenAI-compatible → `type: file` content part with base64 data URL


_PDF_BYTES = b"%PDF-1.4\n%fake\n"
_PDF_BASE64 = base64.b64encode(_PDF_BYTES).decode("ascii")


def _neutral_document_part(*, display_name: str | None = None) -> dict:
    doc = {"mime_type": "application/pdf", "data": _PDF_BASE64}
    if display_name is not None:
        doc["display_name"] = display_name
    return {"document": doc}


# ---------------------------------------------------------------------------
# document_part() — envelope construction
# ---------------------------------------------------------------------------


def test_document_part_with_explicit_display_name() -> None:
    from bp_sdk.llm import document_part

    part = document_part(_PDF_BYTES, mime_type="application/pdf", display_name="contract.pdf")
    assert part == {
        "document": {
            "mime_type": "application/pdf",
            "data": _PDF_BASE64,
            "display_name": "contract.pdf",
        }
    }


def test_document_part_defaults_display_name_to_basename(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bp_sdk.llm import document_part

    path = tmp_path / "report.pdf"
    path.write_bytes(_PDF_BYTES)
    part = document_part(path)
    assert part["document"]["display_name"] == "report.pdf"
    assert part["document"]["mime_type"] == "application/pdf"
    assert part["document"]["data"] == _PDF_BASE64


def test_document_part_bytes_omits_display_name_when_not_set() -> None:
    from bp_sdk.llm import document_part

    part = document_part(_PDF_BYTES, mime_type="application/pdf")
    assert "display_name" not in part["document"]


def test_document_part_requires_mime_type_for_bytes() -> None:
    from bp_sdk.llm import document_part

    with pytest.raises(ValueError, match="mime_type is required"):
        document_part(_PDF_BYTES)


def test_document_part_guesses_text_plain_mime_for_txt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Pin the secondary supported MIME — `text/plain` via .txt
    extension — so the SDK doesn't quietly stop recognising it."""
    from bp_sdk.llm import document_part

    path = tmp_path / "note.txt"
    path.write_bytes(b"hello\n")
    part = document_part(path)
    assert part["document"]["mime_type"] == "text/plain"


# ---------------------------------------------------------------------------
# Adapter: Anthropic — distinct `type: document` block
# ---------------------------------------------------------------------------


def test_anthropic_tool_result_converts_neutral_document_to_native() -> None:
    """Anthropic gates PDF input on the `document` block type — NOT
    on `image`. Pin that the adapter emits the correct block."""
    from bp_router.llm.providers.anthropic import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "Here's the report."},
            _neutral_document_part(display_name="report.pdf"),
        ],
        tool_call_id="toolu_2",
        name="fetch_report",
    )
    out, _system = _convert_messages([msg])
    assert len(out) == 1
    assert out[0]["role"] == "user"
    blocks = out[0]["content"]
    assert len(blocks) == 1
    tr = blocks[0]
    assert tr["type"] == "tool_result"
    content = tr["content"]
    assert content[0] == {"type": "text", "text": "Here's the report."}
    doc_block = content[1]
    assert doc_block["type"] == "document"
    assert doc_block["source"]["type"] == "base64"
    assert doc_block["source"]["media_type"] == "application/pdf"
    assert doc_block["source"]["data"] == _PDF_BASE64


# ---------------------------------------------------------------------------
# Adapter: Gemini — same `inline_data` shape as image
# ---------------------------------------------------------------------------


def test_gemini_tool_result_emits_document_as_inline_data() -> None:
    """Gemini uses a single inline_data shape for any modality; the
    MIME type tells the model how to interpret it. Pin that the
    document branch reaches the same code path as image."""
    from bp_router.llm.providers.gemini import GeminiAdapter
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "summary follows"},
            _neutral_document_part(display_name="contract.pdf"),
        ],
        tool_call_id="call_2",
        name="fetch_contract",
    )
    adapter = GeminiAdapter(concrete_model="gemini-3-flash-preview", api_key="x")
    contents, _system = adapter._convert_messages([msg])
    tool_entry = contents[-1]
    fr = tool_entry["parts"][0]["function_response"]
    assert fr["name"] == "fetch_contract"
    parts = fr["parts"]
    assert parts[0] == {"text": "summary follows"}
    doc_native = parts[1]
    assert doc_native["inline_data"]["mime_type"] == "application/pdf"
    assert doc_native["inline_data"]["data"] == _PDF_BASE64
    assert doc_native["inline_data"]["display_name"] == "contract.pdf"
    # `$ref` substitution mechanism wires up via display_name —
    # same as for images.
    assert fr["response"]["result"] == {"contract.pdf": {"$ref": "contract.pdf"}}


# ---------------------------------------------------------------------------
# Adapter: OpenAI Responses — input_file with base64 data URL
# ---------------------------------------------------------------------------


def test_openai_responses_tool_result_synthesises_user_input_file_message() -> None:
    """OpenAI Responses takes documents as `input_file` parts inside
    a synthesised follow-up user message (same indirection as
    images). The follow-up carries `filename` + `file_data` data URL."""
    from bp_router.llm.providers.openai import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "see attached"},
            _neutral_document_part(display_name="contract.pdf"),
        ],
        tool_call_id="call_doc",
        name="fetch_doc",
    )
    items, _ = _convert_messages([msg])
    assert items[0]["type"] == "function_call_output"
    follow_up = items[1]
    assert follow_up["role"] == "user"
    parts = follow_up["content"]
    assert parts[0] == {"type": "input_text", "text": "see attached"}
    file_part = parts[1]
    assert file_part["type"] == "input_file"
    assert file_part["filename"] == "contract.pdf"
    assert file_part["file_data"].startswith("data:application/pdf;base64,")


def test_openai_responses_document_uses_default_filename_when_no_display_name() -> None:
    """OpenAI requires `filename` on input_file. Source pin on the
    fallback so a caller-omitted display_name doesn't crash the
    adapter."""
    from bp_router.llm.providers.openai import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[_neutral_document_part()],  # no display_name
        tool_call_id="call_doc2",
        name="fetch_doc",
    )
    items, _ = _convert_messages([msg])
    follow_up = items[1]
    file_part = follow_up["content"][0]
    assert file_part["filename"] == "document.pdf"


# ---------------------------------------------------------------------------
# Adapter: OpenAI-compatible — `type: file` content part
# ---------------------------------------------------------------------------


def test_openai_compat_tool_result_synthesises_file_content_part() -> None:
    from bp_router.llm.providers.openai_compatible import _convert_messages
    from bp_router.llm.service import Message

    msg = Message(
        role="tool",
        content=[
            {"text": "see attached"},
            _neutral_document_part(display_name="manual.pdf"),
        ],
        tool_call_id="call_doc",
        name="fetch_doc",
    )
    out = _convert_messages([msg])
    assert out[0]["role"] == "tool"
    follow_up = out[1]
    assert follow_up["role"] == "user"
    parts = follow_up["content"]
    assert parts[0] == {"type": "text", "text": "see attached"}
    file_part = parts[1]
    assert file_part["type"] == "file"
    assert file_part["file"]["filename"] == "manual.pdf"
    assert file_part["file"]["file_data"].startswith("data:application/pdf;base64,")
