"""Tests for the Anthropic provider adapter — neutral → Anthropic
content-block translation, tool definitions, tool_choice mapping,
parallel-tool-result merging, and stop_reason coercion.

Pure unit tests; the `anthropic` SDK is NOT required at import time
(the adapter defers it). We exercise `_build_create_kwargs` and the
pure helpers with stub objects standing in for SDK responses.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from bp_router.llm.providers.anthropic import (
    _DEFAULT_MAX_TOKENS,
    AnthropicAdapter,
    _block_to_dict,
    _build_create_kwargs,
    _convert_messages,
    _convert_part,
    _convert_response,
    _convert_tool_choice,
    _convert_tools,
    _is_thinking_enabled,
    _map_stop_reason,
)
from bp_router.llm.service import LlmDelta, LlmService, Message, ToolSpec

# ---------------------------------------------------------------------------
# Part translation
# ---------------------------------------------------------------------------


def test_convert_part_text_neutral_to_block() -> None:
    assert _convert_part({"text": "hi"}) == {"type": "text", "text": "hi"}


def test_convert_part_native_text_passthrough() -> None:
    part = {"type": "text", "text": "hi"}
    assert _convert_part(part) == part


def test_convert_part_image_neutral_to_anthropic_source() -> None:
    """Neutral `{"image": {...}}` (from `bp_sdk.llm.image_part`) →
    Anthropic's `{"type": "image", "source": {"type": "base64", ...}}`."""
    raw = b"\x89PNG\r\n"
    data_b64 = base64.b64encode(raw).decode("ascii")
    out = _convert_part({"image": {"mime_type": "image/png", "data": data_b64}})
    assert out == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",   # NOT mime_type — Anthropic spelling
            "data": data_b64,
        },
    }


def test_convert_part_image_with_url_source_passthrough() -> None:
    """Anthropic-native URL-source images pass through unchanged."""
    part = {"type": "image", "source": {"type": "url", "url": "https://x/y.jpg"}}
    assert _convert_part(part) == part


def test_convert_part_function_call_neutral_to_tool_use() -> None:
    """Gemini-flavoured `function_call` part (from
    `Message.assistant_from_response`) → Anthropic `tool_use` block.
    Note `args` → `input` rename."""
    part = {
        "function_call": {
            "id": "toolu_01ABC", "name": "get_weather",
            "args": {"location": "SF"},
        },
    }
    assert _convert_part(part) == {
        "type": "tool_use",
        "id": "toolu_01ABC",
        "name": "get_weather",
        "input": {"location": "SF"},
    }


def test_convert_part_function_call_accepts_input_alias() -> None:
    """If the agent built the part with Anthropic's spelling already
    (`input` instead of `args`), don't lose data."""
    part = {
        "function_call": {
            "id": "toolu_01", "name": "f", "input": {"k": "v"},
        },
    }
    assert _convert_part(part)["input"] == {"k": "v"}


def test_convert_part_drops_thought_signature() -> None:
    """`thought_signature` is Gemini-only; Anthropic rejects unknown
    fields on text/tool_use blocks. Drop it during translation so
    cross-provider round-trips don't 400."""
    part = {"type": "text", "text": "hi", "thought_signature": "sig-X"}
    assert "thought_signature" not in _convert_part(part)


def test_convert_part_tool_use_native_passthrough() -> None:
    part = {
        "type": "tool_use", "id": "toolu_01", "name": "f", "input": {"k": "v"},
    }
    assert _convert_part(part) == part


# ---------------------------------------------------------------------------
# Message conversion: system extraction
# ---------------------------------------------------------------------------


def test_system_extracted_to_top_level() -> None:
    """`role="system"` messages don't appear in `messages`; their
    content concatenates into the top-level `system` kwarg."""
    msgs = [
        Message(role="system", content="You are a cat. Your name is Neko."),
        Message(role="user", content="Hello"),
    ]
    converted, system = _convert_messages(msgs)
    assert system == "You are a cat. Your name is Neko."
    assert converted == [{"role": "user", "content": "Hello"}]


def test_multiple_system_messages_concatenate() -> None:
    msgs = [
        Message(role="system", content="Be concise."),
        Message(role="system", content="Use British English."),
        Message(role="user", content="Hi"),
    ]
    _, system = _convert_messages(msgs)
    assert system == "Be concise.\nUse British English."


def test_no_system_returns_none() -> None:
    msgs = [Message(role="user", content="Hi")]
    _, system = _convert_messages(msgs)
    assert system is None


# ---------------------------------------------------------------------------
# Message conversion: parallel tool result merging
# ---------------------------------------------------------------------------


def test_consecutive_tool_messages_merge_into_single_user_message() -> None:
    """Per Anthropic docs, parallel tool results MUST be in a SINGLE
    user message — separate messages disable parallel tool use on
    subsequent turns."""
    msgs = [
        Message(role="user", content="Check Paris and London."),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "tu_1", "name": "weather",
             "input": {"city": "Paris"}},
            {"type": "tool_use", "id": "tu_2", "name": "weather",
             "input": {"city": "London"}},
        ]),
        Message(role="tool", tool_call_id="tu_1", name="weather", content="15C"),
        Message(role="tool", tool_call_id="tu_2", name="weather", content="12C"),
    ]
    converted, _ = _convert_messages(msgs)

    # Two tool messages → one user message with two tool_result blocks.
    assert len(converted) == 3  # user, assistant, user (merged tool results)
    last = converted[-1]
    assert last["role"] == "user"
    assert len(last["content"]) == 2
    assert last["content"][0] == {
        "type": "tool_result", "tool_use_id": "tu_1", "content": "15C",
    }
    assert last["content"][1] == {
        "type": "tool_result", "tool_use_id": "tu_2", "content": "12C",
    }


def test_tool_result_then_user_text_merges_into_one_message() -> None:
    """When the agent follows a tool_result with a fresh user text
    turn, Anthropic requires the tool_result to come FIRST in a
    single user message."""
    msgs = [
        Message(role="tool", tool_call_id="tu_1", name="f", content="ok"),
        Message(role="user", content="What does that mean?"),
    ]
    converted, _ = _convert_messages(msgs)
    assert len(converted) == 1
    assert converted[0]["role"] == "user"
    blocks = converted[0]["content"]
    assert blocks[0]["type"] == "tool_result"  # first
    assert blocks[1] == {"type": "text", "text": "What does that mean?"}  # after


def test_trailing_tool_messages_flush_as_user_message() -> None:
    """A conversation ending in tool_results (waiting for next call)
    flushes them as their own user message."""
    msgs = [Message(role="tool", tool_call_id="tu_1", name="f", content="ok")]
    converted, _ = _convert_messages(msgs)
    assert converted == [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}],
    }]


def test_tool_result_dict_content_serialized_to_json() -> None:
    """Anthropic's `tool_result.content` accepts a string; we encode
    dicts to JSON (preserving structure for the model to parse)."""
    msgs = [
        Message(
            role="tool", tool_call_id="tu_1", name="f",
            content={"status": "delayed", "departure_time": "12 PM"},
        ),
    ]
    converted, _ = _convert_messages(msgs)
    block = converted[0]["content"][0]
    assert block["type"] == "tool_result"
    assert json.loads(block["content"]) == {
        "status": "delayed", "departure_time": "12 PM",
    }


def test_tool_result_list_content_passthrough() -> None:
    """If the agent supplies a list of content blocks (e.g., text +
    image for a multimodal tool result), pass through unchanged."""
    blocks = [
        {"type": "text", "text": "see image"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
    ]
    msgs = [Message(role="tool", tool_call_id="tu_1", name="f", content=blocks)]
    converted, _ = _convert_messages(msgs)
    assert converted[0]["content"][0]["content"] == blocks


# ---------------------------------------------------------------------------
# Message conversion: assistant turns
# ---------------------------------------------------------------------------


def test_assistant_string_content_passes_through() -> None:
    msgs = [Message(role="assistant", content="OK")]
    converted, _ = _convert_messages(msgs)
    assert converted == [{"role": "assistant", "content": "OK"}]


def test_assistant_neutral_round_trip_translates() -> None:
    """`Message.assistant_from_response` produces Gemini-flavoured
    parts (`{"text": ...}`, `{"function_call": {...}}`); the Anthropic
    adapter rewrites them to typed blocks."""
    msgs = [
        Message(role="assistant", content=[
            {"text": "Let me check."},
            {"function_call": {"id": "tu_1", "name": "weather",
                               "args": {"city": "Paris"}}},
        ]),
    ]
    converted, _ = _convert_messages(msgs)
    blocks = converted[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Let me check."}
    assert blocks[1] == {
        "type": "tool_use", "id": "tu_1", "name": "weather",
        "input": {"city": "Paris"},
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def test_neutral_tools_renamed_to_input_schema() -> None:
    """Anthropic uses `input_schema` (not Gemini's `parameters`)."""
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = _convert_tools(
        tools=[ToolSpec(name="f", description="d", parameters=schema)],
        provider_options=None,
    )
    assert out == [{"name": "f", "description": "d", "input_schema": schema}]


def test_native_tool_blocks_appended_via_provider_options() -> None:
    """Native server-side tools (web_search, code_execution) live in
    `provider_options["tools"]` and append after function tools."""
    out = _convert_tools(
        tools=[ToolSpec(name="f", description="d", parameters={"type": "object"})],
        provider_options={"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
    )
    assert len(out) == 2
    assert out[0]["name"] == "f"
    assert out[1] == {"type": "web_search_20250305", "name": "web_search"}


# ---------------------------------------------------------------------------
# tool_choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("neutral,wire", [
    ("auto", {"type": "auto"}),
    ("required", {"type": "any"}),
    ("none", {"type": "none"}),
])
def test_tool_choice_string_modes(neutral, wire) -> None:
    assert _convert_tool_choice(neutral) == wire


def test_tool_choice_dict_passthrough() -> None:
    """Specific-tool selection — caller passes the Anthropic-shaped
    dict directly."""
    custom = {"type": "tool", "name": "get_weather"}
    assert _convert_tool_choice(custom) == custom


def test_tool_choice_disable_parallel_passes_through() -> None:
    custom = {"type": "auto", "disable_parallel_tool_use": True}
    assert _convert_tool_choice(custom) == custom


def test_tool_choice_none_returns_none_when_unset() -> None:
    assert _convert_tool_choice(None) is None


# ---------------------------------------------------------------------------
# stop_reason mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("anthropic,neutral", [
    ("end_turn", "stop"),
    ("stop_sequence", "stop"),
    ("max_tokens", "length"),
    ("tool_use", "tool_calls"),
    ("pause_turn", "stop"),
    ("refusal", "content_filter"),
    ("model_context_window_exceeded", "length"),
    (None, "stop"),
    ("unknown_future_value", "stop"),
])
def test_stop_reason_mapping(anthropic, neutral) -> None:
    assert _map_stop_reason(anthropic) == neutral


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass
class _StubBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    # Thinking-block fields. The Anthropic SDK exposes these as
    # attributes on `ThinkingBlock` / `RedactedThinkingBlock`; the
    # adapter reads them via getattr.
    thinking: str = ""
    signature: str = ""
    data: str = ""


@dataclass
class _StubUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _StubResponse:
    content: list[_StubBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Any = None

    def model_dump(self) -> dict[str, Any]:
        return {}


def test_response_text_only() -> None:
    resp = _StubResponse(
        content=[_StubBlock(type="text", text="Hello!")],
        stop_reason="end_turn",
        usage=_StubUsage(input_tokens=12, output_tokens=6),
    )
    out = _convert_response(resp)
    assert out.text == "Hello!"
    assert out.tool_calls == []
    assert out.finish_reason == "stop"
    assert out.usage.input_tokens == 12
    assert out.usage.output_tokens == 6


def test_response_text_concatenates_multiple_text_blocks() -> None:
    """Anthropic may emit multiple text blocks (e.g. text → tool_use →
    text). We concatenate text content into a single string."""
    resp = _StubResponse(content=[
        _StubBlock(type="text", text="I'll check the weather. "),
        _StubBlock(type="tool_use", id="tu_1", name="weather", input={"city": "SF"}),
        _StubBlock(type="text", text="One moment."),
    ])
    out = _convert_response(resp)
    assert out.text == "I'll check the weather. One moment."


def test_response_with_tool_use_block() -> None:
    """tool_use blocks come back with `id`, `name`, `input` (Anthropic
    spelling). We preserve `id` verbatim and rename `input` → `args`
    in our neutral ToolCall."""
    resp = _StubResponse(
        content=[
            _StubBlock(type="text", text="Checking..."),
            _StubBlock(
                type="tool_use", id="toolu_01XYZ", name="get_weather",
                input={"location": "Paris"},
            ),
        ],
        stop_reason="tool_use",
    )
    out = _convert_response(resp)
    assert out.text == "Checking..."
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.id == "toolu_01XYZ"
    assert tc.name == "get_weather"
    assert tc.args == {"location": "Paris"}
    assert out.finish_reason == "tool_calls"


def test_response_parallel_tool_use() -> None:
    resp = _StubResponse(
        content=[
            _StubBlock(type="tool_use", id="tu_1", name="weather", input={"city": "Paris"}),
            _StubBlock(type="tool_use", id="tu_2", name="weather", input={"city": "London"}),
        ],
        stop_reason="tool_use",
    )
    out = _convert_response(resp)
    assert [tc.id for tc in out.tool_calls] == ["tu_1", "tu_2"]
    assert [tc.args["city"] for tc in out.tool_calls] == ["Paris", "London"]


def test_response_cache_tokens_surfaced() -> None:
    """Anthropic reports cache hits / writes separately — capture them
    in our usage struct."""
    resp = _StubResponse(
        content=[_StubBlock(type="text", text="ok")],
        usage=_StubUsage(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=200, cache_creation_input_tokens=50,
        ),
    )
    out = _convert_response(resp)
    assert out.usage.cache_read_tokens == 200
    assert out.usage.cache_write_tokens == 50


# ---------------------------------------------------------------------------
# End-to-end: _build_create_kwargs
# ---------------------------------------------------------------------------


def test_build_create_kwargs_sets_default_max_tokens() -> None:
    """Anthropic REQUIRES max_tokens — agents who don't set one
    shouldn't get a 400 from the upstream."""
    out = _build_create_kwargs(
        concrete_model="claude-opus-4-7",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None,
        temperature=None, max_tokens=None,
        provider_options=None,
    )
    assert out["max_tokens"] == _DEFAULT_MAX_TOKENS


def test_build_create_kwargs_full_request_shape() -> None:
    out = _build_create_kwargs(
        concrete_model="claude-sonnet-4-6",
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
        provider_options={"stop_sequences": ["END"]},
    )
    assert out["model"] == "claude-sonnet-4-6"
    assert out["max_tokens"] == 512
    assert out["system"] == "Be concise."
    assert out["temperature"] == 0.5
    assert out["tool_choice"] == {"type": "auto"}
    assert out["tools"][0]["input_schema"] == {
        "type": "object", "properties": {"city": {"type": "string"}}
    }
    assert out["stop_sequences"] == ["END"]
    assert out["messages"] == [{"role": "user", "content": "Weather in Paris?"}]


def test_build_create_kwargs_extended_thinking_passthrough() -> None:
    """`thinking` is passed through verbatim until the extended-
    thinking docs land and we wire round-trip handling. Agents who
    know Anthropic's shape can use it via provider_options today."""
    out = _build_create_kwargs(
        concrete_model="claude-opus-4-7",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None, temperature=None, max_tokens=None,
        provider_options={"thinking": {"type": "enabled", "budget_tokens": 5000}},
    )
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 5000}


# ---------------------------------------------------------------------------
# Default alias map exposes Claude family
# ---------------------------------------------------------------------------


def test_default_aliases_include_claude() -> None:
    class _StubSettings:
        pass

    svc = LlmService(_StubSettings())  # type: ignore[arg-type]

    def _resolve(alias):
        b = svc._presets[alias]
        return b.provider, b.concrete_model

    assert _resolve("claude") == ("anthropic", "claude-sonnet-4-6")
    assert _resolve("claude-opus") == ("anthropic", "claude-opus-4-7")
    assert _resolve("claude-opus-4-7") == ("anthropic", "claude-opus-4-7")
    assert _resolve("claude-sonnet") == ("anthropic", "claude-sonnet-4-6")
    assert _resolve("claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")
    assert _resolve("claude-haiku") == ("anthropic", "claude-haiku-4-5")
    assert _resolve("claude-haiku-4-5") == ("anthropic", "claude-haiku-4-5")


# ---------------------------------------------------------------------------
# Stubs / unsupported surfaces
# ---------------------------------------------------------------------------


def test_embed_raises() -> None:
    """Anthropic doesn't ship a first-party embeddings API; the docs
    point at Voyage AI for that surface."""
    adapter = AnthropicAdapter(concrete_model="claude-opus-4-7", api_key="x")
    import asyncio

    with pytest.raises(NotImplementedError, match="embeddings"):
        asyncio.run(adapter.embed("hi"))


# ---------------------------------------------------------------------------
# Extended / adaptive thinking — extraction
# ---------------------------------------------------------------------------


def test_thinking_block_extracted_to_reasoning_blocks() -> None:
    """`thinking` blocks land in reasoning_blocks verbatim — the
    signature is what the next turn's API call decrypts to
    reconstruct context."""
    resp = _StubResponse(
        content=[
            _StubBlock(
                type="thinking",
                thinking="Let me reason step by step...",
                signature="EosnCkYICxIMMb3LzNrMu...",
            ),
            _StubBlock(type="text", text="The answer is 42."),
        ],
        stop_reason="end_turn",
    )
    out = _convert_response(resp)
    assert out.text == "The answer is 42."
    assert len(out.reasoning_blocks) == 1
    assert out.reasoning_blocks[0] == {
        "type": "thinking",
        "thinking": "Let me reason step by step...",
        "signature": "EosnCkYICxIMMb3LzNrMu...",
    }


def test_thought_summary_aggregates_visible_thinking_text() -> None:
    """When `display="summarized"` (default on Sonnet 4.6), thinking
    blocks carry visible text. Concatenate into thought_summary."""
    resp = _StubResponse(
        content=[
            _StubBlock(type="thinking", thinking="First, ", signature="s1"),
            _StubBlock(type="thinking", thinking="then second.", signature="s2"),
            _StubBlock(type="text", text="Done."),
        ],
    )
    out = _convert_response(resp)
    assert out.thought_summary == "First, then second."
    # Both blocks preserved for round-trip.
    assert len(out.reasoning_blocks) == 2


def test_redacted_thinking_block_extracted() -> None:
    """The CRITICAL gotcha from the docs: `redacted_thinking` blocks
    must round-trip too. Filtering by `block.type == "thinking"`
    alone silently drops them."""
    resp = _StubResponse(
        content=[
            _StubBlock(type="redacted_thinking", data="<encrypted-blob>"),
            _StubBlock(type="text", text="Continuing."),
        ],
    )
    out = _convert_response(resp)
    assert out.reasoning_blocks == [
        {"type": "redacted_thinking", "data": "<encrypted-blob>"},
    ]
    # Redacted blocks have no visible text — thought_summary stays None.
    assert out.thought_summary is None


def test_omitted_thinking_block_extracted_with_empty_text() -> None:
    """`display="omitted"` (default on Opus 4.7): thinking blocks
    arrive with empty `thinking` field but the signature is still
    populated. Round-trip must preserve the signature."""
    resp = _StubResponse(
        content=[
            _StubBlock(type="thinking", thinking="", signature="opaque-sig"),
            _StubBlock(type="text", text="Done."),
        ],
    )
    out = _convert_response(resp)
    assert out.reasoning_blocks[0] == {
        "type": "thinking", "thinking": "", "signature": "opaque-sig",
    }
    assert out.thought_summary is None


def test_mixed_thinking_redacted_and_text_extraction() -> None:
    resp = _StubResponse(
        content=[
            _StubBlock(type="thinking", thinking="visible reasoning",
                       signature="sig-1"),
            _StubBlock(type="redacted_thinking", data="<blob>"),
            _StubBlock(type="thinking", thinking="more reasoning",
                       signature="sig-2"),
            _StubBlock(type="text", text="Result."),
            _StubBlock(type="tool_use", id="tu_1", name="f", input={"k": "v"}),
        ],
        stop_reason="tool_use",
    )
    out = _convert_response(resp)
    assert out.text == "Result."
    assert len(out.tool_calls) == 1
    assert out.thought_summary == "visible reasoningmore reasoning"
    assert [b["type"] for b in out.reasoning_blocks] == [
        "thinking", "redacted_thinking", "thinking",
    ]


def test_block_to_dict_preserves_thinking_fields() -> None:
    block = _StubBlock(type="thinking", thinking="hi", signature="sig")
    assert _block_to_dict(block) == {
        "type": "thinking", "thinking": "hi", "signature": "sig",
    }


def test_block_to_dict_preserves_redacted_thinking() -> None:
    block = _StubBlock(type="redacted_thinking", data="<blob>")
    assert _block_to_dict(block) == {
        "type": "redacted_thinking", "data": "<blob>",
    }


# ---------------------------------------------------------------------------
# Extended / adaptive thinking — round-trip
# ---------------------------------------------------------------------------


def test_assistant_message_with_thinking_block_passes_through() -> None:
    """When the agent rebuilds an assistant turn with reasoning blocks
    (via Message.assistant_from_response), the adapter passes them
    through unchanged. Critical for multi-turn extended-thinking +
    tool use loops."""
    msg = Message(
        role="assistant",
        content=[
            {"type": "thinking", "thinking": "Reasoning...",
             "signature": "<Sig_A>"},
            {"type": "text", "text": "I'll check the weather."},
            {"type": "tool_use", "id": "tu_1", "name": "weather",
             "input": {"city": "Paris"}},
        ],
    )
    contents, _ = _convert_messages([msg])
    parts = contents[0]["content"]
    assert parts[0] == {
        "type": "thinking", "thinking": "Reasoning...",
        "signature": "<Sig_A>",
    }
    assert parts[1]["type"] == "text"
    assert parts[2]["type"] == "tool_use"


def test_redacted_thinking_block_round_trip() -> None:
    msg = Message(
        role="assistant",
        content=[
            {"type": "redacted_thinking", "data": "<encrypted>"},
            {"type": "text", "text": "Done."},
        ],
    )
    contents, _ = _convert_messages([msg])
    assert contents[0]["content"][0] == {
        "type": "redacted_thinking", "data": "<encrypted>",
    }


# ---------------------------------------------------------------------------
# Extended / adaptive thinking — provider_options + tool_choice guard
# ---------------------------------------------------------------------------


def test_thinking_config_passes_through_provider_options() -> None:
    out = _build_create_kwargs(
        concrete_model="claude-sonnet-4-6",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None,
        temperature=None, max_tokens=None,
        provider_options={"thinking": {"type": "enabled", "budget_tokens": 4096}},
    )
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_adaptive_thinking_with_display_and_effort() -> None:
    """Adaptive thinking + `output_config.effort` is the recommended
    surface for Opus 4.7 / Opus 4.6 / Sonnet 4.6."""
    out = _build_create_kwargs(
        concrete_model="claude-opus-4-7",
        messages=[Message(role="user", content="hi")],
        tools=None, tool_choice=None,
        temperature=None, max_tokens=None,
        provider_options={
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": "medium"},
        },
    )
    assert out["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert out["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("config,enabled", [
    ({"type": "enabled", "budget_tokens": 1024}, True),
    ({"type": "adaptive"}, True),
    ({"type": "adaptive", "display": "omitted"}, True),
    ({"type": "disabled"}, False),
    (None, False),
])
def test_is_thinking_enabled(config, enabled) -> None:
    options = {"thinking": config} if config is not None else None
    assert _is_thinking_enabled(options) is enabled


def test_is_thinking_enabled_no_provider_options() -> None:
    assert _is_thinking_enabled(None) is False
    assert _is_thinking_enabled({}) is False


def test_tool_choice_required_rejects_when_thinking_enabled() -> None:
    """Per Anthropic docs: tool_choice 'any' / 'tool' aren't compatible
    with extended thinking. Raise locally instead of sending a
    guaranteed-400 payload."""
    with pytest.raises(ValueError, match="not compatible with"):
        _convert_tool_choice("required", thinking_enabled=True)


def test_tool_choice_dict_tool_rejects_when_thinking_enabled() -> None:
    with pytest.raises(ValueError, match="not compatible with"):
        _convert_tool_choice(
            {"type": "tool", "name": "f"}, thinking_enabled=True,
        )


def test_tool_choice_dict_any_rejects_when_thinking_enabled() -> None:
    with pytest.raises(ValueError, match="not compatible with"):
        _convert_tool_choice({"type": "any"}, thinking_enabled=True)


def test_tool_choice_auto_allowed_with_thinking() -> None:
    assert _convert_tool_choice("auto", thinking_enabled=True) == {"type": "auto"}


def test_tool_choice_none_allowed_with_thinking() -> None:
    assert _convert_tool_choice("none", thinking_enabled=True) == {"type": "none"}


def test_tool_choice_required_allowed_without_thinking() -> None:
    """Without thinking, 'required' still maps cleanly."""
    assert _convert_tool_choice("required") == {"type": "any"}


def test_build_create_kwargs_rejects_required_with_thinking() -> None:
    """End-to-end: tool_choice='required' + thinking config in
    provider_options bubbles up the ValueError."""
    with pytest.raises(ValueError, match="not compatible with"):
        _build_create_kwargs(
            concrete_model="claude-sonnet-4-6",
            messages=[Message(role="user", content="hi")],
            tools=None,
            tool_choice="required",
            temperature=None,
            max_tokens=None,
            provider_options={"thinking": {"type": "enabled", "budget_tokens": 1024}},
        )


# ---------------------------------------------------------------------------
# End-to-end: full multi-turn extended-thinking + tool use round trip
# ---------------------------------------------------------------------------


def test_full_extended_thinking_tool_use_round_trip() -> None:
    """Walk the path that breaks with a 400 if any link drops a
    thinking block:

    1. Receive response with [thinking, text, tool_use].
    2. Build assistant message via SDK's assistant_from_response.
    3. Build tool response.
    4. Pass both back to the adapter.
    5. Verify thinking block is FIRST in the rebuilt assistant turn,
       signature preserved verbatim.
    """
    from bp_sdk.llm import LlmResponse as SdkLlmResponse
    from bp_sdk.llm import Message as SdkMessage
    from bp_sdk.llm import ToolCall as SdkToolCall

    # Step 1: simulated response from Anthropic with extended thinking.
    resp = SdkLlmResponse(
        text="I'll check the weather for you.",
        tool_calls=[
            SdkToolCall(id="toolu_01ABC", name="get_weather",
                        args={"location": "Paris"}),
        ],
        thought_summary="The user wants Paris weather...",
        reasoning_blocks=[
            {
                "type": "thinking",
                "thinking": "The user wants Paris weather...",
                "signature": "<Signature_A>",
            },
        ],
    )

    # Step 2: agent rebuilds assistant turn — helper prepends thinking.
    assistant = SdkMessage.assistant_from_response(resp)
    assert isinstance(assistant.content, list)
    assert assistant.content[0]["type"] == "thinking"
    assert assistant.content[0]["signature"] == "<Signature_A>"

    # Step 3: agent crafts function response.
    tool = SdkMessage.tool_response(
        tool_call_id="toolu_01ABC",
        name="get_weather",
        response={"temp_f": 72},
    )

    # Step 4: convert to router-side Messages and run the adapter.
    router_assistant = Message(role="assistant", content=assistant.content)
    router_tool = Message(
        role="tool", name=tool.name, tool_call_id=tool.tool_call_id,
        content=tool.content,
    )
    contents, _ = _convert_messages([router_assistant, router_tool])

    # Step 5: thinking block is present, first, signature intact.
    assistant_parts = contents[0]["content"]
    assert assistant_parts[0] == {
        "type": "thinking",
        "thinking": "The user wants Paris weather...",
        "signature": "<Signature_A>",
    }
    assert assistant_parts[1]["type"] == "text"
    assert assistant_parts[2]["type"] == "tool_use"

    # tool_result paired by tool_use_id.
    tool_part = contents[1]["content"][0]
    assert tool_part["type"] == "tool_result"
    assert tool_part["tool_use_id"] == "toolu_01ABC"


# ---------------------------------------------------------------------------
# Streaming — SSE event translation
# ---------------------------------------------------------------------------


def _ev(type: str, **kw) -> SimpleNamespace:
    """Build a stub SSE event with attribute access."""
    obj = SimpleNamespace(type=type)
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


def _delta(type: str, **kw) -> SimpleNamespace:
    """Stub `event.delta` payload."""
    return SimpleNamespace(type=type, **kw)


def _block(type: str, **kw) -> SimpleNamespace:
    """Stub `event.content_block` payload."""
    return SimpleNamespace(type=type, **kw)


class _StubStream:
    """Stand-in for `client.messages.stream(...).__aenter__()`.

    Yields a pre-baked event sequence, mirroring what the Anthropic
    SDK exposes: `async with stream as s: async for event in s: ...`.
    """

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def __aenter__(self) -> _StubStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> _StubStream:
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self.events):
            raise StopAsyncIteration
        ev = self.events[self._i]
        self._i += 1
        return ev


class _StubMessagesAPI:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.stream_kwargs: dict[str, Any] | None = None

    def stream(self, **kwargs: Any) -> _StubStream:
        self.stream_kwargs = kwargs
        return _StubStream(self._events)

    async def count_tokens(self, **kwargs: Any) -> Any:
        # Captured for assertion in tests.
        self.count_tokens_kwargs = kwargs
        return SimpleNamespace(input_tokens=42)


class _StubAnthropicClient:
    def __init__(self, events: list[Any]) -> None:
        self.messages = _StubMessagesAPI(events)


async def _drain_stream(adapter: AnthropicAdapter, events: list[Any]) -> list[LlmDelta]:
    """Run the adapter's _generate_stream and collect every yielded delta."""
    client = _StubAnthropicClient(events)
    iterator = adapter._generate_stream(client, kwargs={"model": adapter.concrete_model})
    out: list[LlmDelta] = []
    async for delta in iterator:
        out.append(delta)
    return out


def _adapter_for_streaming() -> AnthropicAdapter:
    return AnthropicAdapter(concrete_model="claude-opus-4-7", api_key="x")


def test_streaming_text_only() -> None:
    """Basic: message_start → text block → message_delta → message_stop.

    Mirrors the docs' first SSE example."""
    events = [
        _ev("message_start", message=SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=25, output_tokens=1,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )),
        _ev("content_block_start", index=0, content_block=_block("text", text="")),
        _ev("ping"),
        _ev("content_block_delta", index=0, delta=_delta("text_delta", text="Hello")),
        _ev("content_block_delta", index=0, delta=_delta("text_delta", text="!")),
        _ev("content_block_stop", index=0),
        _ev("message_delta",
            delta=SimpleNamespace(stop_reason="end_turn", stop_sequence=None),
            usage=SimpleNamespace(
                input_tokens=25, output_tokens=15,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            )),
        _ev("message_stop"),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))

    # First delta is the input-token usage from message_start.
    assert deltas[0].usage is not None
    assert deltas[0].usage.input_tokens == 25
    assert deltas[0].usage.output_tokens == 1

    # Two text deltas.
    text_deltas = [d for d in deltas if d.text and not d.thought]
    assert [d.text for d in text_deltas] == ["Hello", "!"]

    # Ping is silently consumed; no spurious deltas.
    assert all(d.text != "ping" for d in deltas)

    # Final delta carries finish_reason + cumulative usage.
    final = deltas[-1]
    assert final.finish_reason == "stop"
    assert final.usage is not None
    assert final.usage.output_tokens == 15


def test_streaming_tool_use_accumulates_partial_json() -> None:
    """tool_use blocks emit one ToolCall delta on content_block_stop
    with the input JSON parsed from accumulated `partial_json` chunks
    (per docs: "deltas are partial JSON strings, whereas the final
    tool_use.input is always an object")."""
    events = [
        _ev("message_start", message=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))),
        # First a text block.
        _ev("content_block_start", index=0, content_block=_block("text", text="")),
        _ev("content_block_delta", index=0, delta=_delta("text_delta", text="checking...")),
        _ev("content_block_stop", index=0),
        # Then a tool_use block — partial_json chunked.
        _ev("content_block_start", index=1, content_block=_block(
            "tool_use", id="toolu_01T1x", name="get_weather", input={})),
        _ev("content_block_delta", index=1, delta=_delta("input_json_delta", partial_json="")),
        _ev("content_block_delta", index=1, delta=_delta(
            "input_json_delta", partial_json='{"location":')),
        _ev("content_block_delta", index=1, delta=_delta(
            "input_json_delta", partial_json=' "San Francisco, CA"')),
        _ev("content_block_delta", index=1, delta=_delta("input_json_delta", partial_json='}')),
        _ev("content_block_stop", index=1),
        _ev("message_delta",
            delta=SimpleNamespace(stop_reason="tool_use", stop_sequence=None),
            usage=SimpleNamespace(input_tokens=10, output_tokens=89,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)),
        _ev("message_stop"),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))

    # Text streamed.
    assert any(d.text == "checking..." and not d.thought for d in deltas)

    # ToolCall emitted exactly once with parsed JSON.
    tool_call_deltas = [d for d in deltas if d.tool_call is not None]
    assert len(tool_call_deltas) == 1
    tc = tool_call_deltas[0].tool_call
    assert tc.id == "toolu_01T1x"
    assert tc.name == "get_weather"
    assert tc.args == {"location": "San Francisco, CA"}

    # finish_reason maps tool_use → tool_calls.
    final = deltas[-1]
    assert final.finish_reason == "tool_calls"


def test_streaming_tool_use_handles_malformed_json_gracefully() -> None:
    """If the model somehow emits malformed partial_json, surface an
    empty args dict rather than crash the stream."""
    events = [
        _ev("content_block_start", index=0, content_block=_block(
            "tool_use", id="t1", name="f", input={})),
        _ev("content_block_delta", index=0, delta=_delta(
            "input_json_delta", partial_json='{"k": "unclosed')),
        _ev("content_block_stop", index=0),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))
    tool_call_deltas = [d for d in deltas if d.tool_call is not None]
    assert tool_call_deltas[0].tool_call.args == {}


def test_streaming_thinking_block_emits_text_and_reasoning_block() -> None:
    """Per docs: thinking blocks stream text via thinking_delta and
    the signature via signature_delta. We yield one LlmDelta per
    chunk (with thought=True) AND one final reasoning_block on
    content_block_stop with the assembled signature."""
    events = [
        _ev("content_block_start", index=0, content_block=_block(
            "thinking", thinking="", signature="")),
        _ev("content_block_delta", index=0, delta=_delta(
            "thinking_delta",
            thinking="I need to find the GCD of 1071 and 462...")),
        _ev("content_block_delta", index=0, delta=_delta(
            "thinking_delta", thinking="\n1071 = 2 * 462 + 147")),
        _ev("content_block_delta", index=0, delta=_delta(
            "signature_delta", signature="EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkz...")),
        _ev("content_block_stop", index=0),
        _ev("content_block_start", index=1, content_block=_block("text", text="")),
        _ev("content_block_delta", index=1, delta=_delta(
            "text_delta", text="The GCD is 21.")),
        _ev("content_block_stop", index=1),
        _ev("message_delta",
            delta=SimpleNamespace(stop_reason="end_turn", stop_sequence=None),
            usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))

    # Thought-flagged text deltas mirror the docs' thinking_delta text.
    thought_deltas = [d for d in deltas if d.thought]
    assert [d.text for d in thought_deltas] == [
        "I need to find the GCD of 1071 and 462...",
        "\n1071 = 2 * 462 + 147",
    ]

    # The reasoning_block surfaces the assembled thinking + signature.
    rb_deltas = [d for d in deltas if d.reasoning_block is not None]
    assert len(rb_deltas) == 1
    assert rb_deltas[0].reasoning_block == {
        "type": "thinking",
        "thinking": "I need to find the GCD of 1071 and 462..."
                    "\n1071 = 2 * 462 + 147",
        "signature": "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkz...",
    }

    # The final text block isn't thought-flagged.
    text_deltas = [d for d in deltas if d.text and not d.thought]
    assert any(d.text == "The GCD is 21." for d in text_deltas)


def test_streaming_omitted_thinking_emits_signature_only_block() -> None:
    """Per docs: with `display=omitted`, the thinking block opens,
    receives a single signature_delta, and closes — no thinking_delta
    events. The reasoning_block round-tripped should still carry the
    signature with empty text."""
    events = [
        _ev("content_block_start", index=0, content_block=_block(
            "thinking", thinking="", signature="")),
        _ev("content_block_delta", index=0, delta=_delta(
            "signature_delta", signature="EosnCkYICxIMMb3LzNrMu...")),
        _ev("content_block_stop", index=0),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))

    # No thought-text deltas were yielded (no thinking_delta events).
    assert not [d for d in deltas if d.thought]

    rb_deltas = [d for d in deltas if d.reasoning_block]
    assert len(rb_deltas) == 1
    assert rb_deltas[0].reasoning_block == {
        "type": "thinking",
        "thinking": "",
        "signature": "EosnCkYICxIMMb3LzNrMu...",
    }


def test_streaming_redacted_thinking_round_trips_data_field() -> None:
    """`redacted_thinking` blocks have no thinking_delta events; the
    `data` field arrives in content_block_start. We capture it and
    surface as a reasoning_block on stop."""
    events = [
        _ev("content_block_start", index=0, content_block=_block(
            "redacted_thinking", data="<encrypted-blob>")),
        _ev("content_block_stop", index=0),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))
    rb_deltas = [d for d in deltas if d.reasoning_block]
    assert rb_deltas[0].reasoning_block == {
        "type": "redacted_thinking",
        "data": "<encrypted-blob>",
    }


def test_streaming_error_event_logged_not_raised() -> None:
    """`error` events arrive on overload / rate limit. Per docs we
    should "handle unknown event types gracefully" — log and let the
    stream end, don't crash."""
    events = [
        _ev("content_block_start", index=0, content_block=_block("text", text="")),
        _ev("content_block_delta", index=0, delta=_delta("text_delta", text="part")),
        _ev("error", error=SimpleNamespace(type="overloaded_error", message="Overloaded")),
    ]
    # Should not raise; the partial text still came through.
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))
    assert any(d.text == "part" for d in deltas)


def test_streaming_unknown_delta_type_ignored() -> None:
    """Per docs versioning policy: 'new event types may be added, and
    your code should handle unknown event types gracefully.'"""
    events = [
        _ev("content_block_start", index=0, content_block=_block("text", text="")),
        _ev("content_block_delta", index=0, delta=_delta("future_delta", payload="...")),
        _ev("content_block_delta", index=0, delta=_delta("text_delta", text="ok")),
        _ev("content_block_stop", index=0),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))
    # Unknown delta dropped; known text delta still flows.
    assert any(d.text == "ok" for d in deltas)
    assert all(d.text != "..." for d in deltas if d.text)


def test_streaming_skips_unknown_block_types_at_stop() -> None:
    """`server_tool_use` and `web_search_tool_result` are server-side
    blocks — agents don't drive them. We don't emit a tool_call or
    reasoning_block for them on stop; they're effectively a no-op."""
    events = [
        _ev("content_block_start", index=0, content_block=_block(
            "server_tool_use", id="srv_1", name="web_search", input={})),
        _ev("content_block_delta", index=0, delta=_delta(
            "input_json_delta", partial_json='{"query": "x"}')),
        _ev("content_block_stop", index=0),
        _ev("content_block_start", index=1, content_block=_block(
            "web_search_tool_result", tool_use_id="srv_1", content=[])),
        _ev("content_block_stop", index=1),
        _ev("content_block_start", index=2, content_block=_block("text", text="")),
        _ev("content_block_delta", index=2, delta=_delta("text_delta", text="answer")),
        _ev("content_block_stop", index=2),
    ]
    deltas = asyncio.run(_drain_stream(_adapter_for_streaming(), events))

    # No tool_call (it's a server-side block).
    assert not [d for d in deltas if d.tool_call is not None]
    # No reasoning_block.
    assert not [d for d in deltas if d.reasoning_block is not None]
    # Real text still streamed.
    assert any(d.text == "answer" and not d.thought for d in deltas)


# ---------------------------------------------------------------------------
# generate(stream=True) — top-level entrypoint no longer raises
# ---------------------------------------------------------------------------


def test_generate_stream_true_returns_iterator() -> None:
    """Streaming was previously NotImplementedError. Confirm the path
    is wired and returns an async iterator that yields LlmDeltas."""
    adapter = _adapter_for_streaming()

    # Prime the client stub directly so we don't hit the real SDK.
    adapter._client = _StubAnthropicClient([
        _ev("content_block_start", index=0, content_block=_block("text", text="")),
        _ev("content_block_delta", index=0, delta=_delta("text_delta", text="hi")),
        _ev("content_block_stop", index=0),
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
# count_tokens
# ---------------------------------------------------------------------------


def test_count_tokens_returns_input_tokens() -> None:
    """messages.count_tokens accepts the same `messages` + `system`
    shape as `messages.create`; the response is `{input_tokens: int}`."""
    adapter = _adapter_for_streaming()
    adapter._client = _StubAnthropicClient([])  # events list unused for count

    n = asyncio.run(adapter.count_tokens([
        Message(role="system", content="You are a scientist."),
        Message(role="user", content="Hello, Claude."),
    ]))
    assert n == 42  # stub returns input_tokens=42

    # Verify system was extracted top-level (not embedded in messages).
    captured = adapter._client.messages.count_tokens_kwargs
    assert captured["model"] == "claude-opus-4-7"
    assert captured["system"] == "You are a scientist."
    assert captured["messages"] == [{"role": "user", "content": "Hello, Claude."}]


def test_count_tokens_omits_system_when_unset() -> None:
    adapter = _adapter_for_streaming()
    adapter._client = _StubAnthropicClient([])
    asyncio.run(adapter.count_tokens([Message(role="user", content="hi")]))
    captured = adapter._client.messages.count_tokens_kwargs
    assert "system" not in captured
