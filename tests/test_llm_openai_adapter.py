"""Tests for the OpenAI Responses provider adapter — neutral →
Responses translation, tool definitions, tool_choice mapping, output
parsing including reasoning round-trip, count_tokens, and the
flat-input-items shape that's structurally different from
Gemini / Anthropic.

Pure unit tests; the `openai` SDK is NOT required at import time
(the adapter defers it). We exercise `_build_create_kwargs` and the
pure helpers with stub objects standing in for SDK Response /
output items.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from bp_router.llm.providers.openai import (
    _DEFAULT_INCLUDES,
    OpenAIAdapter,
    _build_create_kwargs,
    _convert_messages,
    _convert_response,
    _convert_tool_choice,
    _convert_tools,
    _convert_user_part,
    _derive_finish_reason,
    _reasoning_item_to_dict,
)
from bp_router.llm.service import LlmService, Message, ToolSpec

# ---------------------------------------------------------------------------
# User content-part translation
# ---------------------------------------------------------------------------


def test_user_part_text_neutral_to_input_text() -> None:
    assert _convert_user_part({"text": "hi"}) == {"type": "input_text", "text": "hi"}


def test_user_part_native_input_text_passthrough() -> None:
    part = {"type": "input_text", "text": "hi"}
    assert _convert_user_part(part) == part


def test_user_part_image_neutral_to_input_image_data_url() -> None:
    """Neutral `{"image": {...}}` → OpenAI's `input_image` with
    data: URL. mime_type maps to the data URL prefix."""
    out = _convert_user_part({
        "image": {"mime_type": "image/png", "data": "BASE64DATA"},
    })
    assert out == {
        "type": "input_image",
        "image_url": "data:image/png;base64,BASE64DATA",
    }


def test_user_part_image_default_mime_when_missing() -> None:
    out = _convert_user_part({"image": {"data": "X"}})
    assert out == {"type": "input_image", "image_url": "data:image/jpeg;base64,X"}


def test_user_part_native_input_image_url_passthrough() -> None:
    """When the agent supplies a native `input_image` with a remote
    URL or file_id, pass through unchanged."""
    part = {"type": "input_image", "image_url": "https://example.com/x.jpg"}
    assert _convert_user_part(part) == part
    file_part = {"type": "input_image", "file_id": "file-abc"}
    assert _convert_user_part(file_part) == file_part


def test_user_part_drops_foreign_thinking_blocks() -> None:
    """Anthropic-only `{"type": "thinking"}` parts dropped."""
    assert _convert_user_part({"type": "thinking", "thinking": "...", "signature": "s"}) is None
    assert _convert_user_part({"type": "redacted_thinking", "data": "x"}) is None


# ---------------------------------------------------------------------------
# Message conversion: system extraction
# ---------------------------------------------------------------------------


def test_system_extracted_to_instructions() -> None:
    """System messages don't appear in input items; their content
    folds into the top-level `instructions` kwarg."""
    msgs = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Hello"),
    ]
    items, instructions = _convert_messages(msgs)
    assert instructions == "You are helpful."
    assert items == [{"role": "user", "content": "Hello"}]


def test_multiple_system_messages_concatenate() -> None:
    msgs = [
        Message(role="system", content="Be concise."),
        Message(role="system", content="Use plain English."),
        Message(role="user", content="Hi"),
    ]
    _, instructions = _convert_messages(msgs)
    assert instructions == "Be concise.\nUse plain English."


# ---------------------------------------------------------------------------
# Message conversion: tool messages → function_call_output items
# ---------------------------------------------------------------------------


def test_tool_message_becomes_function_call_output() -> None:
    """`role="tool"` messages translate to top-level
    `function_call_output` items keyed by `call_id` (NOT a separate
    user-with-tool_result like Anthropic)."""
    msgs = [
        Message(role="tool", tool_call_id="call_abc", name="get_weather",
                content="Paris: 15C, sunny"),
    ]
    items, _ = _convert_messages(msgs)
    assert items == [{
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": "Paris: 15C, sunny",
    }]


def test_tool_message_dict_content_serialized_to_json() -> None:
    """OpenAI's `function_call_output.output` is a string; encode dicts."""
    msgs = [
        Message(role="tool", tool_call_id="call_abc", name="get_weather",
                content={"temp_c": 15, "condition": "sunny"}),
    ]
    items, _ = _convert_messages(msgs)
    assert json.loads(items[0]["output"]) == {"temp_c": 15, "condition": "sunny"}


def test_tool_message_omits_call_id_emits_empty() -> None:
    """A tool message without tool_call_id still produces a
    function_call_output item — empty call_id is the upstream's
    problem to surface."""
    msgs = [Message(role="tool", name="f", content="ok")]
    items, _ = _convert_messages(msgs)
    assert items[0]["call_id"] == ""


# ---------------------------------------------------------------------------
# Message conversion: assistant turn flattening
# ---------------------------------------------------------------------------


def test_assistant_string_content_passes_through() -> None:
    """Assistant text is emitted in the canonical Responses shape —
    a `message` item with `output_text` content blocks — NOT the
    easy bare-string form. The structured form preserves the
    reasoning↔message pairing in stateless mode (R8)."""
    msgs = [Message(role="assistant", content="OK")]
    items, _ = _convert_messages(msgs)
    assert items == [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "OK"}],
    }]


def test_assistant_empty_string_content_dropped() -> None:
    """No empty assistant message — keeps the input array tidy."""
    msgs = [Message(role="assistant", content="")]
    items, _ = _convert_messages(msgs)
    assert items == []


def test_assistant_function_call_extracted_to_top_level_item() -> None:
    """Neutral `{"function_call": {...}}` part inside an assistant
    message becomes a top-level `function_call` item, NOT a block
    inside the assistant message. Critical for OpenAI's flat-input
    shape — round-trip mapping breaks otherwise."""
    msgs = [
        Message(role="assistant", content=[
            {"text": "I'll check."},
            {"function_call": {"id": "call_abc", "name": "get_weather",
                               "args": {"location": "Paris"}}},
        ]),
    ]
    items, _ = _convert_messages(msgs)

    # Expect: assistant text message (canonical Responses shape),
    # then standalone function_call item.
    assert len(items) == 2
    assert items[0] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "I'll check."}],
    }
    assert items[1] == {
        "type": "function_call",
        "call_id": "call_abc",
        "name": "get_weather",
        "arguments": '{"location": "Paris"}',
    }


def test_assistant_parallel_function_calls_each_top_level() -> None:
    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "call_1", "name": "f", "args": {"x": 1}}},
            {"function_call": {"id": "call_2", "name": "f", "args": {"x": 2}}},
        ]),
    ]
    items, _ = _convert_messages(msgs)
    assert len(items) == 2
    assert [it["call_id"] for it in items] == ["call_1", "call_2"]


def test_assistant_native_function_call_item_normalised() -> None:
    """If the agent supplies a native function_call item with `args`
    as a dict, re-encode `arguments` as JSON string."""
    msgs = [
        Message(role="assistant", content=[
            {"type": "function_call", "call_id": "c", "name": "f",
             "arguments": {"k": "v"}},  # dict, not string
        ]),
    ]
    items, _ = _convert_messages(msgs)
    assert items[0]["arguments"] == '{"k": "v"}'


def test_assistant_function_call_args_already_string_preserved() -> None:
    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "c", "name": "f",
                               "args": '{"k":"v"}'}},  # already JSON string
        ]),
    ]
    items, _ = _convert_messages(msgs)
    assert items[0]["arguments"] == '{"k":"v"}'


def test_assistant_reasoning_block_extracted_to_top_level() -> None:
    """Round-tripped reasoning blocks (from `LlmResponse.reasoning_blocks`)
    must appear as standalone `reasoning` items in the input array,
    in the same order they were emitted. Per docs: pass back
    encrypted reasoning items between the user message and the
    function_call_output."""
    msgs = [
        Message(role="assistant", content=[
            {
                "type": "reasoning",
                "id": "rs_abc",
                "summary": [{"type": "summary_text", "text": "thinking..."}],
                "encrypted_content": "<opaque-blob>",
            },
            {"text": "Let me check."},
            {"function_call": {"id": "call_1", "name": "weather",
                               "args": {"city": "Paris"}}},
        ]),
    ]
    items, _ = _convert_messages(msgs)

    # Reasoning first, then assistant text, then function call.
    assert len(items) == 3
    assert items[0]["type"] == "reasoning"
    assert items[0]["id"] == "rs_abc"
    assert items[0]["encrypted_content"] == "<opaque-blob>"
    assert items[1]["role"] == "assistant"
    assert items[2]["type"] == "function_call"


def test_assistant_anthropic_thinking_blocks_dropped() -> None:
    """Cross-provider portability: Anthropic's `thinking` /
    `redacted_thinking` blocks have no analogue in OpenAI Responses.
    Drop them rather than 400."""
    msgs = [
        Message(role="assistant", content=[
            {"type": "thinking", "thinking": "...", "signature": "s"},
            {"type": "redacted_thinking", "data": "x"},
            {"text": "OK"},
        ]),
    ]
    items, _ = _convert_messages(msgs)
    # Only the text message survives — in canonical Responses shape.
    assert items == [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "OK"}],
    }]


def test_user_message_with_image_part_translated() -> None:
    msgs = [
        Message(role="user", content=[
            {"text": "Describe this:"},
            {"image": {"mime_type": "image/png", "data": "ABC"}},
        ]),
    ]
    items, _ = _convert_messages(msgs)
    assert items[0]["role"] == "user"
    assert items[0]["content"] == [
        {"type": "input_text", "text": "Describe this:"},
        {"type": "input_image", "image_url": "data:image/png;base64,ABC"},
    ]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def test_neutral_tools_use_function_type_with_parameters() -> None:
    """OpenAI uses `type: function` with `name`, `description`,
    `parameters` (NOT `input_schema` like Anthropic, NOT
    `function_declarations` like Gemini)."""
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = _convert_tools(
        tools=[ToolSpec(name="f", description="d", parameters=schema)],
        provider_options=None,
    )
    assert out == [{
        "type": "function",
        "name": "f",
        "description": "d",
        "parameters": schema,
    }]


def test_strict_omitted_by_default() -> None:
    """Per docs: omitting `strict` lets Responses normalize to
    strict mode automatically. We don't override that default."""
    out = _convert_tools(
        tools=[ToolSpec(name="f", description="d", parameters={"type": "object"})],
        provider_options=None,
    )
    assert "strict" not in out[0]


def test_native_tools_appended_via_provider_options() -> None:
    """Native server tools (web_search, code_interpreter, etc.) come
    via provider_options.tools and append after function tools."""
    out = _convert_tools(
        tools=[ToolSpec(name="f", description="d", parameters={"type": "object"})],
        provider_options={"tools": [{"type": "web_search"}]},
    )
    assert len(out) == 2
    assert out[0]["name"] == "f"
    assert out[1] == {"type": "web_search"}


# ---------------------------------------------------------------------------
# tool_choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("neutral,wire", [
    ("auto", "auto"),
    ("required", "required"),
    ("none", "none"),
])
def test_tool_choice_string_modes(neutral, wire) -> None:
    """OpenAI accepts bare strings — no need to wrap in dicts."""
    assert _convert_tool_choice(neutral) == wire


def test_tool_choice_dict_force_specific_function() -> None:
    custom = {"type": "function", "name": "get_weather"}
    assert _convert_tool_choice(custom) == custom


def test_tool_choice_allowed_tools() -> None:
    custom = {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [{"type": "function", "name": "get_weather"}],
    }
    assert _convert_tool_choice(custom) == custom


def test_tool_choice_none_returns_none_when_unset() -> None:
    assert _convert_tool_choice(None) is None


# ---------------------------------------------------------------------------
# Stop reason / finish_reason derivation
# ---------------------------------------------------------------------------


def test_finish_reason_completed_status() -> None:
    resp = SimpleNamespace(status="completed", output=[])
    assert _derive_finish_reason(resp) == "stop"


def test_finish_reason_tool_calls_inferred_from_output() -> None:
    """Responses doesn't expose a single `stop_reason`. We infer
    `tool_calls` from the presence of a function_call item."""
    resp = SimpleNamespace(status="completed", output=[
        SimpleNamespace(type="message"),
        SimpleNamespace(type="function_call"),
    ])
    assert _derive_finish_reason(resp) == "tool_calls"


def test_finish_reason_max_output_tokens() -> None:
    resp = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[],
    )
    assert _derive_finish_reason(resp) == "length"


def test_finish_reason_content_filter() -> None:
    resp = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="content_filter"),
        output=[],
    )
    assert _derive_finish_reason(resp) == "content_filter"


def test_finish_reason_unknown_incomplete_reason_falls_back_to_stop() -> None:
    resp = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="unknown_future_value"),
        output=[],
    )
    assert _derive_finish_reason(resp) == "stop"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass
class _StubBlock:
    type: str
    text: str = ""


@dataclass
class _StubMessageItem:
    type: str = "message"
    content: list[_StubBlock] = field(default_factory=list)


@dataclass
class _StubFunctionCallItem:
    type: str = "function_call"
    id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _StubReasoningSummary:
    type: str = "summary_text"
    text: str = ""


@dataclass
class _StubReasoningItem:
    type: str = "reasoning"
    id: str = ""
    summary: list[_StubReasoningSummary] = field(default_factory=list)
    encrypted_content: str = ""


@dataclass
class _StubInputDetails:
    cached_tokens: int = 0


@dataclass
class _StubOutputDetails:
    reasoning_tokens: int = 0


@dataclass
class _StubUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: Any = field(default_factory=_StubInputDetails)
    output_tokens_details: Any = field(default_factory=_StubOutputDetails)


@dataclass
class _StubResponse:
    output: list[Any] = field(default_factory=list)
    status: str = "completed"
    usage: Any = None
    incomplete_details: Any = None

    def model_dump(self) -> dict[str, Any]:
        return {}


def test_response_text_only_message_item() -> None:
    resp = _StubResponse(
        output=[_StubMessageItem(content=[
            _StubBlock(type="output_text", text="Hello!"),
        ])],
        status="completed",
        usage=_StubUsage(input_tokens=12, output_tokens=6),
    )
    out = _convert_response(resp)
    assert out.text == "Hello!"
    assert out.tool_calls == []
    assert out.finish_reason == "stop"
    assert out.usage.input_tokens == 12
    assert out.usage.output_tokens == 6


def test_response_text_concatenates_multiple_message_items() -> None:
    """`output[]` can contain multiple message items (e.g., separated
    by tool calls). Concatenate text across all of them."""
    resp = _StubResponse(output=[
        _StubMessageItem(content=[_StubBlock(type="output_text", text="Part 1.")]),
        _StubFunctionCallItem(call_id="c", name="f", arguments="{}"),
        _StubMessageItem(content=[_StubBlock(type="output_text", text=" Part 2.")]),
    ])
    out = _convert_response(resp)
    assert out.text == "Part 1. Part 2."


def test_response_function_call_uses_call_id_not_item_id() -> None:
    """Per docs: `call_id` is the round-trip key for function calls,
    NOT the item's `id`. Confusing them breaks tool-result mapping."""
    resp = _StubResponse(
        output=[_StubFunctionCallItem(
            id="fc_internal_abc",      # item id — should NOT be used
            call_id="call_xyz",         # mapping key — what we want
            name="get_weather",
            arguments='{"location": "Paris"}',
        )],
        status="completed",
    )
    out = _convert_response(resp)
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id == "call_xyz"
    assert out.tool_calls[0].name == "get_weather"
    assert out.tool_calls[0].args == {"location": "Paris"}


def test_response_function_call_arguments_parsed_from_json_string() -> None:
    """Arguments arrive as a JSON-encoded string in OpenAI; parse to dict."""
    resp = _StubResponse(output=[_StubFunctionCallItem(
        call_id="c", name="f",
        arguments='{"x": 1, "y": "two"}',
    )])
    out = _convert_response(resp)
    assert out.tool_calls[0].args == {"x": 1, "y": "two"}


def test_response_function_call_malformed_arguments_yields_empty() -> None:
    """Defensive: malformed JSON → empty args dict, not a crash."""
    resp = _StubResponse(output=[_StubFunctionCallItem(
        call_id="c", name="f",
        arguments='{"unclosed',
    )])
    out = _convert_response(resp)
    assert out.tool_calls[0].args == {}


def test_response_function_call_empty_arguments_yields_empty() -> None:
    resp = _StubResponse(output=[_StubFunctionCallItem(
        call_id="c", name="f", arguments="",
    )])
    out = _convert_response(resp)
    assert out.tool_calls[0].args == {}


def test_response_reasoning_item_extracted_to_reasoning_blocks() -> None:
    """Reasoning items become opaque `reasoning_blocks` entries for
    round-trip. encrypted_content preserved verbatim — the next turn
    needs it for context continuity in stateless mode."""
    resp = _StubResponse(
        output=[_StubReasoningItem(
            id="rs_abc",
            summary=[
                _StubReasoningSummary(type="summary_text", text="Thinking step 1. "),
                _StubReasoningSummary(type="summary_text", text="Thinking step 2."),
            ],
            encrypted_content="<opaque-encrypted-blob>",
        )],
        status="completed",
    )
    out = _convert_response(resp)
    assert len(out.reasoning_blocks) == 1
    block = out.reasoning_blocks[0]
    assert block["type"] == "reasoning"
    assert block["id"] == "rs_abc"
    assert block["encrypted_content"] == "<opaque-encrypted-blob>"
    assert block["summary"] == [
        {"type": "summary_text", "text": "Thinking step 1. "},
        {"type": "summary_text", "text": "Thinking step 2."},
    ]
    # thought_summary aggregates visible summary text.
    assert out.thought_summary == "Thinking step 1. Thinking step 2."


def test_response_reasoning_without_encrypted_content() -> None:
    """When the caller didn't request `reasoning.encrypted_content`,
    the field is absent. Block still round-trippable — just less
    useful in stateless mode."""
    resp = _StubResponse(output=[_StubReasoningItem(
        id="rs_abc",
        summary=[_StubReasoningSummary(text="hi")],
    )])
    out = _convert_response(resp)
    block = out.reasoning_blocks[0]
    assert "encrypted_content" not in block


def test_response_usage_maps_reasoning_tokens_to_thoughts() -> None:
    """Responses reports reasoning tokens via
    `output_tokens_details.reasoning_tokens`. Map onto our neutral
    `thoughts_tokens` field for cross-provider consistency."""
    resp = _StubResponse(
        output=[_StubMessageItem(content=[_StubBlock(type="output_text", text="ok")])],
        usage=_StubUsage(
            input_tokens=10, output_tokens=20,
            output_tokens_details=_StubOutputDetails(reasoning_tokens=8192),
        ),
    )
    out = _convert_response(resp)
    assert out.usage.input_tokens == 10
    assert out.usage.output_tokens == 20
    assert out.usage.thoughts_tokens == 8192


def test_response_usage_cached_tokens_surfaced() -> None:
    resp = _StubResponse(
        output=[_StubMessageItem(content=[_StubBlock(type="output_text", text="ok")])],
        usage=_StubUsage(
            input_tokens=100, output_tokens=10,
            input_tokens_details=_StubInputDetails(cached_tokens=80),
        ),
    )
    out = _convert_response(resp)
    assert out.usage.cache_read_tokens == 80


def test_reasoning_item_to_dict_preserves_fields() -> None:
    item = _StubReasoningItem(
        id="rs_x",
        summary=[_StubReasoningSummary(text="hi")],
        encrypted_content="blob",
    )
    out = _reasoning_item_to_dict(item)
    assert out == {
        "type": "reasoning",
        "id": "rs_x",
        "encrypted_content": "blob",
        "summary": [{"type": "summary_text", "text": "hi"}],
    }


# ---------------------------------------------------------------------------
# End-to-end: _build_create_kwargs
# ---------------------------------------------------------------------------


def test_build_create_kwargs_basic_request() -> None:
    out = _build_create_kwargs(
        concrete_model="gpt-5.5",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None,
        temperature=None, max_tokens=None,
        provider_options=None,
    )
    assert out["model"] == "gpt-5.5"
    assert out["input"] == [{"role": "user", "content": "hi"}]
    # `include` always carries reasoning.encrypted_content for
    # stateless round-trip support.
    assert out["include"] == list(_DEFAULT_INCLUDES)
    assert "instructions" not in out
    assert "max_output_tokens" not in out
    assert "tools" not in out


def test_build_create_kwargs_max_tokens_uses_max_output_tokens() -> None:
    """Responses uses `max_output_tokens`, not `max_tokens`."""
    out = _build_create_kwargs(
        concrete_model="gpt-5.5",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None,
        temperature=None, max_tokens=512,
        provider_options=None,
    )
    assert out["max_output_tokens"] == 512
    assert "max_tokens" not in out


def test_build_create_kwargs_full_request_shape() -> None:
    out = _build_create_kwargs(
        concrete_model="gpt-5.5",
        messages=[
            Message(role="system", content="Be concise."),
            Message(role="user", content="Weather in Paris?"),
        ],
        tools=[ToolSpec(
            name="get_weather", description="Get weather.",
            parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        )],
        tool_choice="auto",
        temperature=0.5,
        max_tokens=512,
        provider_options={
            "reasoning": {"effort": "low", "summary": "auto"},
            "metadata": {"user_id": "u123"},
        },
    )
    assert out["model"] == "gpt-5.5"
    assert out["instructions"] == "Be concise."
    assert out["input"] == [{"role": "user", "content": "Weather in Paris?"}]
    assert out["temperature"] == 0.5
    assert out["max_output_tokens"] == 512
    assert out["tool_choice"] == "auto"
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["name"] == "get_weather"
    assert out["reasoning"] == {"effort": "low", "summary": "auto"}
    assert out["metadata"] == {"user_id": "u123"}


def test_build_create_kwargs_caller_include_merges_with_default() -> None:
    """Callers can add additional includes via provider_options.
    Defaults stay first; duplicates de-duplicated."""
    out = _build_create_kwargs(
        concrete_model="gpt-5",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None, temperature=None, max_tokens=None,
        provider_options={
            "include": ["file_search_call.results", "reasoning.encrypted_content"],
        },
    )
    assert out["include"] == [
        "reasoning.encrypted_content",     # default first
        "file_search_call.results",        # caller's addition
    ]


def test_build_create_kwargs_preserves_previous_response_id() -> None:
    """Stateful callers can chain responses via previous_response_id."""
    out = _build_create_kwargs(
        concrete_model="gpt-5",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None, temperature=None, max_tokens=None,
        provider_options={"previous_response_id": "resp_abc"},
    )
    assert out["previous_response_id"] == "resp_abc"


# ---------------------------------------------------------------------------
# End-to-end: full multi-turn extended-thinking + tool use round trip
# ---------------------------------------------------------------------------


def test_full_reasoning_tool_use_round_trip() -> None:
    """Walk the path that breaks if reasoning items aren't round-
    tripped:
    1. Receive response with reasoning + function_call.
    2. Build assistant message via SDK helper (prepends reasoning).
    3. Build tool response.
    4. Pass back to OpenAI adapter.
    5. Verify the input array shape matches what OpenAI expects:
       reasoning item, optional assistant text, function_call item,
       function_call_output item.
    """
    from bp_sdk.llm import LlmResponse as SdkLlmResponse
    from bp_sdk.llm import Message as SdkMessage
    from bp_sdk.llm import ToolCall as SdkToolCall

    # Step 1: simulated response with reasoning + tool call.
    resp = SdkLlmResponse(
        text="",
        tool_calls=[SdkToolCall(
            id="call_xyz", name="get_weather", args={"location": "Paris"},
        )],
        thought_summary="Decided to fetch weather.",
        reasoning_blocks=[{
            "type": "reasoning",
            "id": "rs_abc",
            "summary": [{"type": "summary_text", "text": "Decided to fetch weather."}],
            "encrypted_content": "<opaque-blob>",
        }],
    )

    # Step 2: helper rebuilds assistant turn with reasoning prepended.
    assistant = SdkMessage.assistant_from_response(resp)
    assert isinstance(assistant.content, list)
    assert assistant.content[0]["type"] == "reasoning"

    # Step 3: tool response.
    tool = SdkMessage.tool_response(
        tool_call_id="call_xyz",
        name="get_weather",
        response={"temp_c": 15},
    )

    # Step 4: convert to router-side Messages and translate.
    router_assistant = Message(role="assistant", content=assistant.content)
    router_tool = Message(
        role="tool", name=tool.name, tool_call_id=tool.tool_call_id,
        content=tool.content,
    )
    items, _ = _convert_messages([router_assistant, router_tool])

    # Step 5: input array shape matches docs:
    #   reasoning, function_call, function_call_output
    # No assistant message in between (resp.text was empty).
    assert len(items) == 3
    assert items[0]["type"] == "reasoning"
    assert items[0]["encrypted_content"] == "<opaque-blob>"
    assert items[1]["type"] == "function_call"
    assert items[1]["call_id"] == "call_xyz"
    assert items[1]["arguments"] == '{"location": "Paris"}'
    assert items[2]["type"] == "function_call_output"
    assert items[2]["call_id"] == "call_xyz"
    assert json.loads(items[2]["output"]) == {"temp_c": 15}


# ---------------------------------------------------------------------------
# Default alias map
# ---------------------------------------------------------------------------


def test_default_aliases_include_openai_family() -> None:
    class _StubSettings:
        pass

    svc = LlmService(_StubSettings())  # type: ignore[arg-type]

    def _resolve(alias):
        b = svc._presets[alias]
        return b.provider, b.concrete_model

    # Preset names use `-` instead of `.` (DB CHECK regex
    # disallows `.`); concrete_model keeps the dotted form
    # upstream providers expect (upstream-bug #2).
    assert _resolve("openai") == ("openai", "gpt-5.5")
    assert _resolve("gpt") == ("openai", "gpt-5.5")
    assert _resolve("gpt-5-5") == ("openai", "gpt-5.5")
    assert _resolve("gpt-5") == ("openai", "gpt-5")
    assert _resolve("gpt-5-4-mini") == ("openai", "gpt-5.4-mini")
    assert _resolve("gpt-5-4-nano") == ("openai", "gpt-5.4-nano")
    assert _resolve("gpt-5-nano") == ("openai", "gpt-5-nano")


# ---------------------------------------------------------------------------
# Stubs / unsupported surfaces
# ---------------------------------------------------------------------------


def test_chat_adapter_embed_routes_to_dedicated_adapter() -> None:
    """The OpenAIAdapter (Responses) is for chat — embeddings live on
    the separate `openai-embeddings` provider with a different concrete
    model namespace. Calling `embed` here points at the right place."""
    adapter = OpenAIAdapter(concrete_model="gpt-5.5", api_key="x")
    with pytest.raises(NotImplementedError, match="embeddings"):
        asyncio.run(adapter.embed("hi"))


# ---------------------------------------------------------------------------
# count_tokens with a stub client
# ---------------------------------------------------------------------------


class _StubInputTokens:
    def __init__(self, value: int = 42) -> None:
        self.captured: dict[str, Any] = {}
        self._value = value

    async def count(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        return SimpleNamespace(input_tokens=self._value)


class _StubResponses:
    def __init__(self, *, count_value: int = 42) -> None:
        self.input_tokens = _StubInputTokens(count_value)


class _StubOpenAIClient:
    def __init__(self, *, count_value: int = 42) -> None:
        self.responses = _StubResponses(count_value=count_value)


def test_count_tokens_returns_input_tokens() -> None:
    """`responses.input_tokens.count` accepts the same shape as
    `responses.create`. Returns the integer count."""
    adapter = OpenAIAdapter(concrete_model="gpt-5.5", api_key="x")
    adapter._client = _StubOpenAIClient(count_value=100)

    n = asyncio.run(adapter.count_tokens([
        Message(role="system", content="Be helpful."),
        Message(role="user", content="Hi"),
    ]))
    assert n == 100

    captured = adapter._client.responses.input_tokens.captured
    assert captured["model"] == "gpt-5.5"
    assert captured["instructions"] == "Be helpful."
    assert captured["input"] == [{"role": "user", "content": "Hi"}]


def test_count_tokens_omits_instructions_when_unset() -> None:
    adapter = OpenAIAdapter(concrete_model="gpt-5.5", api_key="x")
    adapter._client = _StubOpenAIClient()
    asyncio.run(adapter.count_tokens([Message(role="user", content="hi")]))
    captured = adapter._client.responses.input_tokens.captured
    assert "instructions" not in captured


# ---------------------------------------------------------------------------
# Streaming — SSE event translation
# ---------------------------------------------------------------------------


from bp_router.llm.providers.openai import OpenAIEmbeddingsAdapter
from bp_router.llm.service import LlmDelta


def _ev(type: str, **kw) -> SimpleNamespace:
    """Build a stub SSE event with attribute access."""
    obj = SimpleNamespace(type=type)
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


def _stream_item(type: str, **kw) -> SimpleNamespace:
    """Stub `event.item` payload for output_item.added/done events."""
    return SimpleNamespace(type=type, **kw)


class _StubResponseStream:
    """Stand-in for the async context returned by
    `client.responses.stream(...)`. Yields a pre-baked event sequence."""

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def __aenter__(self) -> _StubResponseStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> _StubResponseStream:
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self.events):
            raise StopAsyncIteration
        ev = self.events[self._i]
        self._i += 1
        return ev


class _StubResponsesAPIStreaming:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.stream_kwargs: Optional[dict[str, Any]] = None

    def stream(self, **kwargs: Any) -> _StubResponseStream:
        self.stream_kwargs = kwargs
        return _StubResponseStream(self._events)


class _StubOpenAIClientForStreaming:
    def __init__(self, events: list[Any]) -> None:
        self.responses = _StubResponsesAPIStreaming(events)


async def _drain_stream(adapter: OpenAIAdapter, events: list[Any]) -> list[LlmDelta]:
    """Run the adapter's _generate_stream and collect every yielded delta."""
    client = _StubOpenAIClientForStreaming(events)
    iterator = adapter._generate_stream(client, kwargs={"model": adapter.concrete_model})
    out: list[LlmDelta] = []
    async for delta in iterator:
        out.append(delta)
    return out


def _streaming_adapter() -> OpenAIAdapter:
    return OpenAIAdapter(concrete_model="gpt-5.5", api_key="x")


def test_streaming_text_only() -> None:
    """Basic text streaming. message_start equivalent → output_item
    .added → output_text.delta+ → output_item.done → completed."""
    events = [
        _ev("response.created"),
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="msg_1")),
        _ev("response.output_text.delta", output_index=0, delta="Hello"),
        _ev("response.output_text.delta", output_index=0, delta=", world!"),
        _ev("response.output_item.done", output_index=0,
            item=_stream_item("message", id="msg_1")),
        _ev("response.completed", response=SimpleNamespace(
            status="completed",
            output=[],
            usage=_StubUsage(input_tokens=12, output_tokens=4),
        )),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))

    text_deltas = [d for d in deltas if d.text and not d.thought]
    assert [d.text for d in text_deltas] == ["Hello", ", world!"]

    # Final delta carries finish_reason + usage.
    final = deltas[-1]
    assert final.finish_reason == "stop"
    assert final.usage is not None
    assert final.usage.input_tokens == 12
    assert final.usage.output_tokens == 4


def test_streaming_function_call_accumulates_partial_args() -> None:
    """function_call_arguments.delta partials accumulate; the
    finalised ToolCall arrives on .done with parsed args."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("function_call", id="fc_1",
                              call_id="call_xyz", name="get_weather")),
        _ev("response.function_call_arguments.delta",
            output_index=0, delta='{"loca'),
        _ev("response.function_call_arguments.delta",
            output_index=0, delta='tion": "Pa'),
        _ev("response.function_call_arguments.delta",
            output_index=0, delta='ris"}'),
        _ev("response.function_call_arguments.done",
            output_index=0, item_id="fc_1",
            item=_stream_item("function_call", id="fc_1",
                              call_id="call_xyz", name="get_weather",
                              arguments='{"location": "Paris"}')),
        _ev("response.output_item.done", output_index=0,
            item=_stream_item("function_call", id="fc_1",
                              call_id="call_xyz", name="get_weather")),
        _ev("response.completed", response=SimpleNamespace(
            status="completed",
            output=[SimpleNamespace(type="function_call")],
            usage=_StubUsage(input_tokens=10, output_tokens=15),
        )),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))

    tool_call_deltas = [d for d in deltas if d.tool_call is not None]
    assert len(tool_call_deltas) == 1
    tc = tool_call_deltas[0].tool_call
    assert tc.id == "call_xyz"        # call_id is the round-trip key
    assert tc.name == "get_weather"
    assert tc.args == {"location": "Paris"}

    # finish_reason from response.completed: tool_calls inferred from
    # the function_call item in resp.output.
    assert deltas[-1].finish_reason == "tool_calls"


def test_streaming_function_call_malformed_args_yields_empty() -> None:
    """Malformed partial JSON → empty args dict, not a crash."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("function_call", id="fc",
                              call_id="c", name="f")),
        _ev("response.function_call_arguments.delta",
            output_index=0, delta='{"unclosed'),
        _ev("response.function_call_arguments.done",
            output_index=0, item_id="fc"),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))
    tool_call_deltas = [d for d in deltas if d.tool_call is not None]
    assert tool_call_deltas[0].tool_call.args == {}


def test_streaming_parallel_function_calls() -> None:
    """Two function calls at separate output_index slots — each gets
    its own ToolCall delta with the right call_id, no cross-bleed."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("function_call", id="fc1",
                              call_id="call_paris", name="weather")),
        _ev("response.output_item.added", output_index=1,
            item=_stream_item("function_call", id="fc2",
                              call_id="call_london", name="weather")),
        _ev("response.function_call_arguments.delta",
            output_index=0, delta='{"city": "Paris"}'),
        _ev("response.function_call_arguments.delta",
            output_index=1, delta='{"city": "London"}'),
        _ev("response.function_call_arguments.done", output_index=0),
        _ev("response.function_call_arguments.done", output_index=1),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))

    tool_call_deltas = [d for d in deltas if d.tool_call is not None]
    assert [tc.tool_call.id for tc in tool_call_deltas] == [
        "call_paris", "call_london",
    ]
    assert tool_call_deltas[0].tool_call.args == {"city": "Paris"}
    assert tool_call_deltas[1].tool_call.args == {"city": "London"}


def test_streaming_reasoning_item_emitted_on_done() -> None:
    """Reasoning items emit one consolidated reasoning_block delta on
    output_item.done — assembled state with id + summary +
    encrypted_content for round-trip."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("reasoning", id="rs_abc")),
        _ev("response.output_item.done", output_index=0,
            item=_stream_item(
                "reasoning",
                id="rs_abc",
                summary=[
                    SimpleNamespace(type="summary_text", text="Thinking..."),
                ],
                encrypted_content="<opaque-encrypted-blob>",
            )),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))

    rb_deltas = [d for d in deltas if d.reasoning_block]
    assert len(rb_deltas) == 1
    block = rb_deltas[0].reasoning_block
    assert block["type"] == "reasoning"
    assert block["id"] == "rs_abc"
    assert block["encrypted_content"] == "<opaque-encrypted-blob>"
    assert block["summary"] == [{"type": "summary_text", "text": "Thinking..."}]


def test_streaming_text_delta_under_reasoning_item_flagged_thought() -> None:
    """When response.output_text.delta arrives for an output_index
    whose parent item is `reasoning`, mark the delta with
    thought=True so consumers can render thinking and answer
    separately."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("reasoning", id="rs_x")),
        _ev("response.output_text.delta", output_index=0,
            delta="Step-by-step: "),
        _ev("response.output_text.delta", output_index=0,
            delta="first I'll consider..."),
        _ev("response.output_item.done", output_index=0,
            item=_stream_item("reasoning", id="rs_x",
                              summary=[], encrypted_content="sig")),
        _ev("response.output_item.added", output_index=1,
            item=_stream_item("message", id="msg_x")),
        _ev("response.output_text.delta", output_index=1,
            delta="The answer is 42."),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))

    thought_deltas = [d for d in deltas if d.text and d.thought]
    assert [d.text for d in thought_deltas] == [
        "Step-by-step: ", "first I'll consider...",
    ]

    answer_deltas = [d for d in deltas if d.text and not d.thought]
    assert [d.text for d in answer_deltas] == ["The answer is 42."]


def test_streaming_refusal_delta_surfaced_as_text() -> None:
    """Refusal text streams via response.refusal.delta. We surface it
    via the regular text channel — the agent can detect refusal via
    finish_reason if needed."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="msg")),
        _ev("response.refusal.delta", output_index=0,
            delta="I can't help with that."),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))
    text_deltas = [d for d in deltas if d.text]
    assert text_deltas[0].text == "I can't help with that."


def test_streaming_failed_event_logged_not_raised() -> None:
    """response.failed (e.g., generation cut off mid-stream) should
    log and let the stream end cleanly, not crash."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="m")),
        _ev("response.output_text.delta", output_index=0, delta="part"),
        _ev("response.failed", response=SimpleNamespace(
            error=SimpleNamespace(code="server_error", message="boom"),
        )),
    ]
    # Should not raise; the partial text still came through.
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))
    assert any(d.text == "part" for d in deltas)


def test_streaming_error_event_logged_not_raised() -> None:
    """`error` events arrive on overload / rate limit. Per docs we
    should handle unknown event types gracefully — log and let the
    stream end."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="m")),
        _ev("response.output_text.delta", output_index=0, delta="part"),
        _ev("error", error=SimpleNamespace(message="rate_limited")),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))
    assert any(d.text == "part" for d in deltas)


def test_streaming_max_output_tokens_finish_reason() -> None:
    """response.completed with status=incomplete, reason=max_output_tokens
    → finish_reason=length on the final delta."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="m")),
        _ev("response.output_text.delta", output_index=0, delta="trunc"),
        _ev("response.completed", response=SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output=[],
            usage=_StubUsage(input_tokens=5, output_tokens=300),
        )),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))
    assert deltas[-1].finish_reason == "length"


def test_streaming_unknown_event_type_ignored() -> None:
    """Per docs versioning policy: unknown events should be handled
    gracefully. Don't crash on a future event type."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="m")),
        _ev("response.output_text.delta", output_index=0, delta="ok"),
        _ev("response.future_event_2027", payload="..."),
        _ev("response.completed", response=SimpleNamespace(
            status="completed", output=[],
            usage=_StubUsage(input_tokens=1, output_tokens=1),
        )),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))
    assert any(d.text == "ok" for d in deltas)


def test_streaming_skips_server_tool_progress_events() -> None:
    """Server-side tool activity (file_search_call.searching,
    code_interpreter.in_progress, etc.) doesn't surface through
    neutral deltas. The text the model says about them does."""
    events = [
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="m")),
        _ev("response.output_text.delta", output_index=0,
            delta="Searching the docs..."),
        _ev("response.file_search_call.in_progress"),
        _ev("response.file_search_call.searching"),
        _ev("response.file_search_call.completed"),
        _ev("response.code_interpreter.in_progress"),
        _ev("response.code_interpreter_call_code.delta", delta="x = 1"),
        _ev("response.code_interpreter_call_code.done"),
        _ev("response.code_interpreter.completed"),
        _ev("response.output_text.delta", output_index=0, delta=" Found it."),
    ]
    deltas = asyncio.run(_drain_stream(_streaming_adapter(), events))

    text_deltas = [d for d in deltas if d.text]
    # Only the model's own text streams; tool activity is silent.
    assert [d.text for d in text_deltas] == [
        "Searching the docs...", " Found it.",
    ]
    # No tool_call deltas (file_search is server-side; agent doesn't drive).
    assert not [d for d in deltas if d.tool_call is not None]


def test_streaming_generate_returns_iterator() -> None:
    """generate(stream=True) was previously NotImplementedError;
    confirm the path is wired and returns an async iterator."""
    adapter = _streaming_adapter()
    adapter._client = _StubOpenAIClientForStreaming([
        _ev("response.output_item.added", output_index=0,
            item=_stream_item("message", id="m")),
        _ev("response.output_text.delta", output_index=0, delta="hi"),
    ])

    async def _go() -> list[LlmDelta]:
        result = await adapter.generate(
            [Message(role="user", content="hello")],
            stream=True,
        )
        return [d async for d in result]

    deltas = asyncio.run(_go())
    assert any(d.text == "hi" for d in deltas)


# ---------------------------------------------------------------------------
# Embeddings adapter
# ---------------------------------------------------------------------------


@dataclass
class _StubEmbedding:
    embedding: list[float]


@dataclass
class _StubEmbeddingsResponse:
    data: list[_StubEmbedding]


class _StubEmbeddingsAPI:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.captured: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> _StubEmbeddingsResponse:
        self.captured = kwargs
        return _StubEmbeddingsResponse(
            data=[_StubEmbedding(embedding=v) for v in self._vectors],
        )


class _StubOpenAIEmbeddingsClient:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.embeddings = _StubEmbeddingsAPI(vectors)


def test_embeddings_adapter_string_input_wrapped_to_list() -> None:
    """String input wraps to a single-element list before calling
    `embeddings.create`. Single output vector returned."""
    adapter = OpenAIEmbeddingsAdapter(
        concrete_model="text-embedding-3-small", api_key="x",
    )
    adapter._client = _StubOpenAIEmbeddingsClient([[0.1, 0.2, 0.3]])
    out = asyncio.run(adapter.embed("hello"))

    assert out == [[0.1, 0.2, 0.3]]
    captured = adapter._client.embeddings.captured
    assert captured["model"] == "text-embedding-3-small"
    assert captured["input"] == ["hello"]


def test_embeddings_adapter_list_input_passes_through() -> None:
    adapter = OpenAIEmbeddingsAdapter(
        concrete_model="text-embedding-3-large", api_key="x",
    )
    vectors = [[0.1] * 4, [0.2] * 4, [0.3] * 4]
    adapter._client = _StubOpenAIEmbeddingsClient(vectors)
    out = asyncio.run(adapter.embed(["a", "b", "c"]))

    assert out == vectors
    captured = adapter._client.embeddings.captured
    assert captured["model"] == "text-embedding-3-large"
    assert captured["input"] == ["a", "b", "c"]


def test_embeddings_adapter_generate_unsupported() -> None:
    """The embeddings adapter doesn't implement chat/Responses —
    routes that to the regular OpenAIAdapter via the alias map."""
    adapter = OpenAIEmbeddingsAdapter(
        concrete_model="text-embedding-3-small", api_key="x",
    )
    with pytest.raises(NotImplementedError, match="generate"):
        asyncio.run(adapter.generate([Message(role="user", content="hi")]))


def test_embeddings_adapter_count_tokens_unsupported() -> None:
    adapter = OpenAIEmbeddingsAdapter(
        concrete_model="text-embedding-3-small", api_key="x",
    )
    with pytest.raises(NotImplementedError, match="tiktoken"):
        asyncio.run(adapter.count_tokens([Message(role="user", content="hi")]))


# ---------------------------------------------------------------------------
# Embedding aliases in the default map
# ---------------------------------------------------------------------------


def test_default_aliases_include_embedding_models() -> None:
    class _StubSettings:
        pass

    svc = LlmService(_StubSettings())  # type: ignore[arg-type]

    def _resolve(alias):
        b = svc._presets[alias]
        return b.provider, b.concrete_model

    assert _resolve("text-embedding-3-small") == (
        "openai-embeddings", "text-embedding-3-small",
    )
    assert _resolve("text-embedding-3-large") == (
        "openai-embeddings", "text-embedding-3-large",
    )
