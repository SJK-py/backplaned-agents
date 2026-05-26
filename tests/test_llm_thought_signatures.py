"""Tests for thought signatures, function-call ids, and thought
summaries — the round-trip layer that makes Gemini 3 multi-turn
function calling work.

Pure unit tests against the Gemini adapter's pure helpers and the
`Message.assistant_from_response` SDK helper. No google-genai
required — we use stub objects with `getattr`-friendly attributes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from bp_router.llm.providers.gemini import (
    GeminiAdapter,
    _decode_signature,
    _encode_signature,
)
from bp_router.llm.service import Message
from bp_sdk.llm import LlmResponse as SdkLlmResponse
from bp_sdk.llm import Message as SdkMessage
from bp_sdk.llm import ToolCall as SdkToolCall

# ---------------------------------------------------------------------------
# Stub classes for fake Gemini SDK responses
# ---------------------------------------------------------------------------


@dataclass
class _StubFunctionCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class _StubPart:
    text: str = ""
    thought: bool = False
    thought_signature: Any = None  # bytes or None
    function_call: Any = None  # _StubFunctionCall or None


@dataclass
class _StubContent:
    parts: list[_StubPart] = field(default_factory=list)


@dataclass
class _StubCandidate:
    content: _StubContent = field(default_factory=_StubContent)
    finish_reason: str = "STOP"


@dataclass
class _StubUsageMeta:
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0


@dataclass
class _StubResponse:
    candidates: list[_StubCandidate] = field(default_factory=list)
    usage_metadata: Any = None

    def model_dump(self) -> dict[str, Any]:
        return {}


def _adapter() -> GeminiAdapter:
    return GeminiAdapter(concrete_model="gemini-3-flash-preview", api_key="x")


# ---------------------------------------------------------------------------
# Signature codec
# ---------------------------------------------------------------------------


def test_encode_signature_bytes_to_base64() -> None:
    raw = b"\x00\x01\x02opaque-encrypted-blob"
    encoded = _encode_signature(raw)
    assert encoded == base64.b64encode(raw).decode("ascii")


def test_encode_signature_passes_string_through() -> None:
    """Already-encoded strings (e.g., round-tripped from a previous
    response) shouldn't be double-encoded."""
    assert _encode_signature("already-encoded") == "already-encoded"


def test_encode_signature_none() -> None:
    assert _encode_signature(None) is None


def test_decode_signature_round_trip() -> None:
    raw = b"\x10\x20\x30signature"
    encoded = _encode_signature(raw)
    assert _decode_signature(encoded) == raw


def test_decode_signature_invalid_base64() -> None:
    assert _decode_signature("not-valid-base64!!!") is None


# ---------------------------------------------------------------------------
# Response → neutral: signature + id extraction
# ---------------------------------------------------------------------------


def test_function_call_with_signature_extracted() -> None:
    """First (only) function call carries a signature; id preserved
    verbatim — no `or fc.name` fallback."""
    sig = b"sig-A"
    resp = _StubResponse(
        candidates=[
            _StubCandidate(content=_StubContent(parts=[
                _StubPart(
                    function_call=_StubFunctionCall(
                        name="check_flight",
                        args={"flight": "AA100"},
                        id="function-call-1",
                    ),
                    thought_signature=sig,
                ),
            ]))
        ],
        usage_metadata=_StubUsageMeta(prompt_token_count=10, candidates_token_count=2),
    )
    out = _adapter()._convert_response(resp)
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.id == "function-call-1"
    assert tc.name == "check_flight"
    assert tc.args == {"flight": "AA100"}
    assert tc.thought_signature == _encode_signature(sig)


def test_parallel_function_calls_signature_only_on_first() -> None:
    """Per Gemini docs: parallel function calls in one response carry
    the signature on the FIRST function call only."""
    sig = b"first-only"
    resp = _StubResponse(
        candidates=[
            _StubCandidate(content=_StubContent(parts=[
                _StubPart(
                    function_call=_StubFunctionCall(
                        name="get_temp", args={"city": "Paris"}, id="fc-paris",
                    ),
                    thought_signature=sig,
                ),
                _StubPart(
                    function_call=_StubFunctionCall(
                        name="get_temp", args={"city": "London"}, id="fc-london",
                    ),
                    thought_signature=None,
                ),
            ]))
        ],
    )
    out = _adapter()._convert_response(resp)
    assert len(out.tool_calls) == 2
    assert out.tool_calls[0].thought_signature == _encode_signature(sig)
    assert out.tool_calls[1].thought_signature is None


def test_function_call_id_preserved_when_empty() -> None:
    """A function_call without an id stays empty — no fallback to name.
    Falling back would corrupt round-trip mapping on Gemini 3."""
    resp = _StubResponse(
        candidates=[
            _StubCandidate(content=_StubContent(parts=[
                _StubPart(
                    function_call=_StubFunctionCall(
                        name="check_flight", args={"flight": "AA100"}, id="",
                    ),
                ),
            ]))
        ],
    )
    out = _adapter()._convert_response(resp)
    assert out.tool_calls[0].id == ""
    assert out.tool_calls[0].name == "check_flight"


def test_text_only_response_signature_on_last_part() -> None:
    """Gemini 3 may attach a signature to the final text part — we
    surface it via `thought_signature` on LlmResponse (recommended,
    not mandatory, to round-trip)."""
    resp = _StubResponse(
        candidates=[
            _StubCandidate(content=_StubContent(parts=[
                _StubPart(text="The answer is 42.", thought_signature=b"sig-text"),
            ]))
        ],
    )
    out = _adapter()._convert_response(resp)
    assert out.text == "The answer is 42."
    assert out.thought_signature == _encode_signature(b"sig-text")


def test_thought_summary_extraction() -> None:
    """`include_thoughts=True` yields parts with `part.thought=True`.
    Concatenate them into thought_summary; keep them out of `text`."""
    resp = _StubResponse(
        candidates=[
            _StubCandidate(content=_StubContent(parts=[
                _StubPart(text="Let me think...", thought=True),
                _StubPart(text=" Step by step.", thought=True),
                _StubPart(text="The answer is 42."),
            ]))
        ],
    )
    out = _adapter()._convert_response(resp)
    assert out.text == "The answer is 42."
    assert out.thought_summary == "Let me think... Step by step."


def test_thoughts_token_count_in_usage() -> None:
    resp = _StubResponse(
        candidates=[_StubCandidate(content=_StubContent(parts=[_StubPart(text="hi")]))],
        usage_metadata=_StubUsageMeta(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=8192,
        ),
    )
    out = _adapter()._convert_response(resp)
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 20
    assert out.usage.thoughts_tokens == 8192


# ---------------------------------------------------------------------------
# Neutral → Gemini: round-trip on the next turn
# ---------------------------------------------------------------------------


def test_assistant_message_with_function_call_signature_emitted() -> None:
    """When the agent rebuilds the assistant turn with a function_call
    part carrying a thought_signature, the adapter must decode it
    from the wire base64 form back into raw bytes so the Gemini SDK
    accepts it.

    R8 fix: pre-R8 the adapter passed the base64 string straight
    through to the SDK. The SDK either dropped the field or
    coerced it, breaking Gemini 3 multi-turn function calls. Now
    `_convert_part` decodes the signature; the byte-form is what
    the SDK expects.
    """
    import base64

    sig_bytes = b"\x01\x02\xff\xfe arbitrary signature bytes"
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
    msg = Message(
        role="assistant",
        content=[
            {
                "function_call": {
                    "id": "fc-1", "name": "check_flight", "args": {"flight": "AA100"},
                },
                "thought_signature": sig_b64,
            }
        ],
    )
    contents, _ = _adapter()._convert_messages([msg])
    assert contents[0]["role"] == "model"
    parts = contents[0]["parts"]
    assert parts[0]["function_call"]["id"] == "fc-1"
    # Signature decoded back to bytes — that's the SDK-accepted form.
    assert parts[0]["thought_signature"] == sig_bytes


def test_tool_message_includes_id_in_function_response() -> None:
    """Gemini 3 requires `id` on each function_response so the model
    can map results back to in-flight calls."""
    msg = Message(
        role="tool",
        name="check_flight",
        tool_call_id="fc-1",
        content={"status": "delayed"},
    )
    contents, _ = _adapter()._convert_messages([msg])
    fr = contents[0]["parts"][0]["function_response"]
    assert fr["id"] == "fc-1"
    assert fr["name"] == "check_flight"
    assert fr["response"] == {"status": "delayed"}


def test_tool_message_omits_id_when_unset() -> None:
    """Backwards-compat: a Message without tool_call_id (e.g.,
    pre-Gemini-3 agent) still emits a valid function_response."""
    msg = Message(role="tool", name="check_flight", content={"status": "ok"})
    contents, _ = _adapter()._convert_messages([msg])
    fr = contents[0]["parts"][0]["function_response"]
    assert "id" not in fr


def test_tool_message_string_content_wrapped_in_result() -> None:
    """String content gets wrapped in `{result: ...}` for Gemini's
    function_response schema."""
    msg = Message(role="tool", name="check_flight", tool_call_id="x", content="ok")
    contents, _ = _adapter()._convert_messages([msg])
    fr = contents[0]["parts"][0]["function_response"]
    assert fr["response"] == {"result": "ok"}


# ---------------------------------------------------------------------------
# SDK: Message.assistant_from_response round-trip helper
# ---------------------------------------------------------------------------


def test_sdk_assistant_from_response_text_only() -> None:
    """Text-only response: signature lives on the text part; no
    function_call parts."""
    resp = SdkLlmResponse(
        text="The answer is 42.",
        thought_signature="sig-text",
    )
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.role == "assistant"
    assert msg.content == [{"text": "The answer is 42.", "thought_signature": "sig-text"}]


def test_sdk_assistant_from_response_text_no_signature() -> None:
    resp = SdkLlmResponse(text="Hello.")
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content == [{"text": "Hello."}]


def test_sdk_assistant_from_response_single_function_call() -> None:
    """Single function call carries the signature; text part (if any)
    does NOT carry it (signature ALREADY lives on the FC)."""
    resp = SdkLlmResponse(
        text="",
        tool_calls=[
            SdkToolCall(
                id="fc-1", name="check_flight", args={"flight": "AA100"},
                thought_signature="sig-A",
            ),
        ],
        thought_signature="this-is-redundant-shouldnt-leak-onto-text",
    )
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content == [
        {
            "function_call": {"id": "fc-1", "name": "check_flight", "args": {"flight": "AA100"}},
            "thought_signature": "sig-A",
        },
    ]


def test_sdk_assistant_from_response_parallel_function_calls() -> None:
    """Per Gemini docs: signature on FIRST function call only."""
    resp = SdkLlmResponse(
        text="",
        tool_calls=[
            SdkToolCall(id="fc-paris", name="get_temp", args={"city": "Paris"},
                        thought_signature="sig-A"),
            SdkToolCall(id="fc-london", name="get_temp", args={"city": "London"}),
        ],
    )
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content[0]["thought_signature"] == "sig-A"
    assert "thought_signature" not in msg.content[1]
    assert msg.content[1]["function_call"]["id"] == "fc-london"


def test_sdk_assistant_from_response_text_plus_function_call() -> None:
    """Text + function call: text part gets no signature (lives on FC).
    Common shape when model narrates before invoking a tool."""
    resp = SdkLlmResponse(
        text="Let me check that for you.",
        tool_calls=[
            SdkToolCall(id="fc-1", name="check_flight", args={"flight": "AA100"},
                        thought_signature="sig-A"),
        ],
    )
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content[0] == {"text": "Let me check that for you."}
    assert msg.content[1]["thought_signature"] == "sig-A"


def test_sdk_assistant_from_response_empty() -> None:
    resp = SdkLlmResponse(text="")
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content == ""


def test_sdk_assistant_from_response_prepends_reasoning_blocks() -> None:
    """Anthropic's `thinking` / `redacted_thinking` blocks are stored
    on `LlmResponse.reasoning_blocks` and MUST be re-emitted at the
    front of the assistant turn during tool use — otherwise the next
    request 400s. The helper prepends them automatically."""
    resp = SdkLlmResponse(
        text="I'll check.",
        tool_calls=[
            SdkToolCall(id="tu_1", name="weather", args={"city": "Paris"}),
        ],
        reasoning_blocks=[
            {"type": "thinking", "thinking": "Reasoning...", "signature": "sig-A"},
            {"type": "redacted_thinking", "data": "<encrypted>"},
        ],
    )
    msg = SdkMessage.assistant_from_response(resp)
    assert isinstance(msg.content, list)
    # Thinking blocks come FIRST in the rebuilt assistant turn.
    assert msg.content[0] == {
        "type": "thinking", "thinking": "Reasoning...", "signature": "sig-A",
    }
    assert msg.content[1] == {"type": "redacted_thinking", "data": "<encrypted>"}
    assert msg.content[2] == {"text": "I'll check."}
    assert msg.content[3]["function_call"]["id"] == "tu_1"


def test_sdk_assistant_from_response_no_reasoning_blocks_unchanged() -> None:
    """Gemini path: empty reasoning_blocks → no behaviour change."""
    resp = SdkLlmResponse(text="hi")
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content == [{"text": "hi"}]


def test_sdk_assistant_from_response_reasoning_blocks_only() -> None:
    """Edge case: response has reasoning blocks but no text and no
    tool calls (e.g., `display="omitted"` with thinking on, single
    turn). Helper still emits the blocks."""
    resp = SdkLlmResponse(
        text="",
        reasoning_blocks=[
            {"type": "thinking", "thinking": "", "signature": "opaque-sig"},
        ],
    )
    msg = SdkMessage.assistant_from_response(resp)
    assert msg.content == [
        {"type": "thinking", "thinking": "", "signature": "opaque-sig"},
    ]


def test_sdk_tool_response_helper() -> None:
    msg = SdkMessage.tool_response(
        tool_call_id="fc-1", name="check_flight", response={"status": "delayed"},
    )
    assert msg.role == "tool"
    assert msg.name == "check_flight"
    assert msg.tool_call_id == "fc-1"
    assert msg.content == {"status": "delayed"}


# ---------------------------------------------------------------------------
# End-to-end: response → SDK helper → adapter (multi-turn round trip)
# ---------------------------------------------------------------------------


def test_full_round_trip_function_call_signature_preserved() -> None:
    """Walk the full Gemini-3 multi-turn loop:

    1. Receive response with a function_call carrying signature.
    2. Build assistant message via SDK helper.
    3. Build tool response message via SDK helper.
    4. Pass both to the adapter.
    5. Verify the signature lands on the assistant function_call part
       and the id lands on the function_response.

    This is the path that breaks with a 400 if any link drops the
    signature.
    """
    # Step 1: simulated response from the adapter on Gemini 3.
    # The signature carries a valid base64 string (the wire form
    # produced by `_encode_signature` from raw SDK bytes). R8 fix
    # ensures `_convert_part` decodes it back to bytes for the
    # outbound SDK call.
    import base64
    raw_sig = b"\xab\xcd\xef\x01\x02 some signature bytes"
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")
    resp = SdkLlmResponse(
        text="",
        tool_calls=[
            SdkToolCall(
                id="function-call-1",
                name="check_flight",
                args={"flight": "AA100"},
                thought_signature=sig_b64,
            ),
        ],
    )

    # Step 2: agent rebuilds the assistant turn.
    assistant = SdkMessage.assistant_from_response(resp)
    # Step 3: agent crafts the function response.
    tool = SdkMessage.tool_response(
        tool_call_id="function-call-1",
        name="check_flight",
        response={"status": "delayed", "departure_time": "12 PM"},
    )

    # Step 4: convert to router-side Messages (the SDK and router use
    # different dataclasses but their semantics are identical for the
    # adapter's purposes).
    router_assistant = Message(
        role="assistant", content=assistant.content,
    )
    router_tool = Message(
        role="tool", name=tool.name, tool_call_id=tool.tool_call_id,
        content=tool.content,
    )

    contents, _ = _adapter()._convert_messages([router_assistant, router_tool])

    # Step 5: verify Gemini-side shape.
    assistant_part = contents[0]["parts"][0]
    assert assistant_part["function_call"]["id"] == "function-call-1"
    # Decoded back to bytes for the SDK (R8 fix).
    assert assistant_part["thought_signature"] == raw_sig

    tool_part = contents[1]["parts"][0]["function_response"]
    assert tool_part["id"] == "function-call-1"
    assert tool_part["name"] == "check_flight"
    assert tool_part["response"] == {"status": "delayed", "departure_time": "12 PM"}
