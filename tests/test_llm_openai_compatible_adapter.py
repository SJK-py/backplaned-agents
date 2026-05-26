"""Tests for the OpenAI-compatible Chat Completions adapter used to
talk to local LLM servers (vLLM, LM Studio, llama.cpp-server, etc.).

The `openai` SDK is NOT imported at test time — the adapter defers
it. We exercise the pure translation helpers and the SDK-shape
contracts directly, plus a stub-driven walk through `generate` /
streaming / `embed`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from bp_router.llm.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    OpenAICompatibleEmbeddingsAdapter,
    _build_create_kwargs,
    _convert_messages,
    _convert_response,
    _convert_tool_choice,
    _convert_tools,
    _convert_user_part,
    _finish_reason,
    _usage_from_chat,
)
from bp_router.llm.service import LlmDelta, LlmResponse, Message, ToolSpec

# ---------------------------------------------------------------------------
# User content-part translation
# ---------------------------------------------------------------------------


def test_user_part_text_neutral_to_native() -> None:
    assert _convert_user_part({"text": "hi"}) == {"type": "text", "text": "hi"}


def test_user_part_native_text_passthrough() -> None:
    native = {"type": "text", "text": "hi"}
    assert _convert_user_part(native) == native


def test_user_part_native_image_url_passthrough() -> None:
    native = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
    assert _convert_user_part(native) == native


def test_user_part_neutral_image_to_data_url() -> None:
    out = _convert_user_part(
        {"image": {"mime_type": "image/jpeg", "data": "BASE64HERE"}}
    )
    assert out == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,BASE64HERE"},
    }


def test_user_part_drops_foreign_reasoning_blocks() -> None:
    """Anthropic / OpenAI reasoning blocks have no place in Chat
    Completions content — drop silently rather than 400ing the local
    server with an unknown type."""
    assert _convert_user_part({"type": "thinking", "thinking": "..."}) is None
    assert _convert_user_part({"type": "redacted_thinking", "data": "..."}) is None
    assert _convert_user_part({"type": "reasoning", "summary": []}) is None


# ---------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------


def test_string_user_message_kept_as_string() -> None:
    out = _convert_messages([Message(role="user", content="hello")])
    assert out == [{"role": "user", "content": "hello"}]


def test_text_only_list_collapses_to_flat_string() -> None:
    """Some local servers reject content as a list when only text is
    present. We compact to a string when no non-text parts remain."""
    msgs = [Message(role="user", content=[{"text": "a"}, {"text": "b"}])]
    out = _convert_messages(msgs)
    assert out == [{"role": "user", "content": "ab"}]


def test_mixed_text_image_keeps_list_form() -> None:
    msgs = [Message(
        role="user",
        content=[
            {"text": "describe this:"},
            {"image": {"mime_type": "image/png", "data": "X"}},
        ],
    )]
    out = _convert_messages(msgs)
    assert out[0]["role"] == "user"
    assert out[0]["content"] == [
        {"type": "text", "text": "describe this:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,X"}},
    ]


def test_developer_role_remapped_to_system() -> None:
    """Chat Completions doesn't have a `developer` role — remap to
    `system` so prompts authored against newer OpenAI Responses still
    work against local servers."""
    out = _convert_messages([Message(role="developer", content="rules")])
    assert out == [{"role": "system", "content": "rules"}]


def test_tool_result_message_uses_tool_role() -> None:
    msg = Message(role="tool", content="OK", tool_call_id="call_abc")
    out = _convert_messages([msg])
    assert out == [{"role": "tool", "tool_call_id": "call_abc", "content": "OK"}]


def test_tool_result_dict_content_serialized_to_json() -> None:
    # A *dict* tool result becomes a JSON-encoded string — the
    # chat-completions tool-role content field is text-only for
    # structured payloads. List content is the multimodal path; it's
    # exercised by the multimodal tool-result tests.
    msg = Message(role="tool", content={"k": "v"}, tool_call_id="c1")
    out = _convert_messages([msg])
    assert out[0]["content"] == json.dumps({"k": "v"})


def test_assistant_with_tool_use_split_into_tool_calls_array() -> None:
    msg = Message(
        role="assistant",
        content=[
            {"text": "calling weather"},
            {"type": "tool_use", "id": "c1", "name": "get_weather",
             "input": {"city": "Paris"}},
        ],
    )
    out = _convert_messages([msg])
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "calling weather"
    assert out[0]["tool_calls"] == [{
        "id": "c1",
        "type": "function",
        "function": {
            "name": "get_weather",
            "arguments": json.dumps({"city": "Paris"}),
        },
    }]


def test_assistant_only_tool_call_uses_null_content() -> None:
    """Chat Completions accepts content=null when tool_calls is set;
    a local server may otherwise complain about empty string content."""
    msg = Message(
        role="assistant",
        content=[
            {"type": "tool_use", "id": "c1", "name": "f", "input": {}},
        ],
    )
    out = _convert_messages([msg])
    assert out[0]["content"] is None
    assert len(out[0]["tool_calls"]) == 1


# ---------------------------------------------------------------------------
# Tool definitions + tool_choice
# ---------------------------------------------------------------------------


def test_tools_use_function_type() -> None:
    out = _convert_tools([
        ToolSpec(name="lookup", description="look up", parameters={"type": "object"})
    ])
    assert out == [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "look up",
            "parameters": {"type": "object"},
        },
    }]


def test_tools_none_returns_none() -> None:
    assert _convert_tools(None) is None
    assert _convert_tools([]) is None


@pytest.mark.parametrize("mode", ["auto", "none", "required"])
def test_tool_choice_string_modes(mode: str) -> None:
    assert _convert_tool_choice(mode) == mode


def test_tool_choice_dict_passthrough() -> None:
    native = {"type": "function", "function": {"name": "f"}}
    assert _convert_tool_choice(native) == native


def test_tool_choice_unknown_string_treated_as_function_name() -> None:
    out = _convert_tool_choice("get_weather")
    assert out == {"type": "function", "function": {"name": "get_weather"}}


# ---------------------------------------------------------------------------
# Build kwargs
# ---------------------------------------------------------------------------


def test_build_kwargs_passthroughs_from_provider_options() -> None:
    kwargs = _build_create_kwargs(
        concrete_model="qwen2.5",
        messages=[Message(role="user", content="hi")],
        tools=None,
        tool_choice=None,
        temperature=0.5,
        max_tokens=128,
        provider_options={
            "top_p": 0.9,
            "stop": ["\n"],
            "seed": 42,
            "extra_body": {"top_k": 50, "repetition_penalty": 1.05},
            # Unknown key is silently dropped (not in the passthrough list).
            "foo": "bar",
        },
    )
    assert kwargs["model"] == "qwen2.5"
    assert kwargs["temperature"] == 0.5
    assert kwargs["max_tokens"] == 128
    assert kwargs["top_p"] == 0.9
    assert kwargs["stop"] == ["\n"]
    assert kwargs["seed"] == 42
    # extra_body forwarded raw — vLLM uses this for sampling extensions.
    assert kwargs["extra_body"] == {"top_k": 50, "repetition_penalty": 1.05}
    assert "foo" not in kwargs


def test_build_kwargs_no_provider_options_no_passthroughs() -> None:
    kwargs = _build_create_kwargs(
        concrete_model="m",
        messages=[Message(role="user", content="hi")],
        tools=None,
        tool_choice=None,
        temperature=None,
        max_tokens=None,
        provider_options=None,
    )
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs
    assert "extra_body" not in kwargs


# ---------------------------------------------------------------------------
# Finish reason / usage parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("stop", "stop"),
    ("length", "length"),
    ("tool_calls", "tool_calls"),
    ("content_filter", "content_filter"),
    ("function_call", "tool_calls"),  # legacy — map to tool_calls
    ("abort", "error"),                # vLLM cancel
    (None, "stop"),                    # missing → stop
])
def test_finish_reason_mapping(raw: Any, expected: str) -> None:
    assert _finish_reason(raw) == expected


def test_usage_extraction() -> None:
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
    out = _usage_from_chat(usage)
    assert out.input_tokens == 10
    assert out.output_tokens == 20


def test_usage_extraction_handles_none() -> None:
    out = _usage_from_chat(None)
    assert out.input_tokens == 0
    assert out.output_tokens == 0


# ---------------------------------------------------------------------------
# Response translation (non-streaming)
# ---------------------------------------------------------------------------


def _stub_resp(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: Any = None,
) -> SimpleNamespace:
    msg_kwargs: dict[str, Any] = {"content": content}
    if tool_calls is not None:
        msg_kwargs["tool_calls"] = [
            SimpleNamespace(
                id=tc["id"],
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=tc["arguments"],
                ),
            )
            for tc in tool_calls
        ]
    else:
        msg_kwargs["tool_calls"] = None
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(**msg_kwargs),
            finish_reason=finish_reason,
        )],
        usage=usage,
    )


def test_convert_response_text_only() -> None:
    resp = _stub_resp(content="hello world")
    out = _convert_response(resp)
    assert isinstance(out, LlmResponse)
    assert out.text == "hello world"
    assert out.tool_calls == []
    assert out.finish_reason == "stop"


def test_convert_response_tool_call_extracted() -> None:
    resp = _stub_resp(
        content="",
        tool_calls=[{
            "id": "call_1",
            "name": "get_weather",
            "arguments": json.dumps({"city": "Tokyo"}),
        }],
        finish_reason="tool_calls",
    )
    out = _convert_response(resp)
    assert out.finish_reason == "tool_calls"
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "get_weather"
    assert tc.args == {"city": "Tokyo"}


def test_convert_response_malformed_tool_args_preserved_as_raw() -> None:
    """A small local model occasionally emits not-quite-JSON. Don't
    crash — surface the raw string so the agent can repair / log."""
    resp = _stub_resp(
        content="",
        tool_calls=[{"id": "c", "name": "f", "arguments": "{not json"}],
        finish_reason="tool_calls",
    )
    out = _convert_response(resp)
    assert out.tool_calls[0].args == {"_raw": "{not json"}


def test_convert_response_empty_choices_returns_empty_response() -> None:
    resp = SimpleNamespace(choices=[], usage=None)
    out = _convert_response(resp)
    assert out.text == ""
    assert out.finish_reason == "stop"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class _AsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _StubChat:
    """Stub for `client.chat.completions.create(stream=True)`. Returns
    a pre-staged async iterator of chunks."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.received_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.received_kwargs = kwargs
        if kwargs.get("stream"):
            return _AsyncIter(self._chunks)
        # Non-streaming path (return pre-staged single response).
        return self._chunks[0]


class _StubClient:
    def __init__(self, chunks: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=_StubChat(chunks))


def _delta_chunk(
    *,
    text: str | None = None,
    tool_call: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
) -> SimpleNamespace:
    delta_kwargs: dict[str, Any] = {}
    if text is not None:
        delta_kwargs["content"] = text
    if tool_call is not None:
        idx = tool_call.get("index", 0)
        fn_kwargs: dict[str, Any] = {}
        if "name" in tool_call:
            fn_kwargs["name"] = tool_call["name"]
        if "arguments" in tool_call:
            fn_kwargs["arguments"] = tool_call["arguments"]
        delta_kwargs["tool_calls"] = [SimpleNamespace(
            index=idx,
            id=tool_call.get("id"),
            function=SimpleNamespace(**fn_kwargs) if fn_kwargs else None,
        )]
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(**delta_kwargs),
            finish_reason=finish_reason,
        )],
        usage=usage,
    )


def test_streaming_text_chunks_yield_text_deltas() -> None:
    chunks = [
        _delta_chunk(text="Hel"),
        _delta_chunk(text="lo"),
        _delta_chunk(finish_reason="stop"),
    ]
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    adapter._client = _StubClient(chunks)

    async def run() -> list[LlmDelta]:
        deltas: list[LlmDelta] = []
        async for d in await adapter.generate(
            [Message(role="user", content="hi")], stream=True
        ):
            deltas.append(d)
        return deltas

    out = asyncio.run(run())
    texts = [d.text for d in out if d.text]
    assert texts == ["Hel", "lo"]
    # Final delta carries finish_reason.
    assert out[-1].finish_reason == "stop"


def test_streaming_tool_call_buffered_then_emitted() -> None:
    """Tool call arguments arrive piecewise per index. We buffer and
    emit a single `tool_call` delta when the stream finishes."""
    chunks = [
        _delta_chunk(tool_call={"index": 0, "id": "call_1", "name": "get_weather"}),
        _delta_chunk(tool_call={"index": 0, "arguments": '{"cit'}),
        _delta_chunk(tool_call={"index": 0, "arguments": 'y": "Paris"}'}),
        _delta_chunk(finish_reason="tool_calls"),
    ]
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    adapter._client = _StubClient(chunks)

    async def run() -> list[LlmDelta]:
        deltas: list[LlmDelta] = []
        async for d in await adapter.generate(
            [Message(role="user", content="hi")], stream=True
        ):
            deltas.append(d)
        return deltas

    out = asyncio.run(run())
    tool_deltas = [d for d in out if d.tool_call is not None]
    assert len(tool_deltas) == 1
    assert tool_deltas[0].tool_call.id == "call_1"
    assert tool_deltas[0].tool_call.name == "get_weather"
    assert tool_deltas[0].tool_call.args == {"city": "Paris"}


def test_streaming_parallel_tool_calls_emitted_in_index_order() -> None:
    chunks = [
        _delta_chunk(tool_call={"index": 0, "id": "a", "name": "f1"}),
        _delta_chunk(tool_call={"index": 1, "id": "b", "name": "f2"}),
        _delta_chunk(tool_call={"index": 0, "arguments": '{}'}),
        _delta_chunk(tool_call={"index": 1, "arguments": '{}'}),
        _delta_chunk(finish_reason="tool_calls"),
    ]
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    adapter._client = _StubClient(chunks)

    async def run() -> list[LlmDelta]:
        deltas: list[LlmDelta] = []
        async for d in await adapter.generate(
            [Message(role="user", content="hi")], stream=True
        ):
            deltas.append(d)
        return deltas

    out = asyncio.run(run())
    tool_ids = [d.tool_call.id for d in out if d.tool_call is not None]
    assert tool_ids == ["a", "b"]


def test_streaming_usage_chunk_carried_in_final_delta() -> None:
    chunks = [
        _delta_chunk(text="x"),
        _delta_chunk(
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        ),
    ]
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    adapter._client = _StubClient(chunks)

    async def run() -> list[LlmDelta]:
        deltas: list[LlmDelta] = []
        async for d in await adapter.generate(
            [Message(role="user", content="hi")], stream=True
        ):
            deltas.append(d)
        return deltas

    out = asyncio.run(run())
    final = out[-1]
    assert final.usage is not None
    assert final.usage.input_tokens == 5
    assert final.usage.output_tokens == 3


def test_streaming_kwargs_include_stream_options_for_usage() -> None:
    """We hint `stream_options.include_usage=True` so servers that
    support it (vLLM, OpenAI) emit a usage chunk on the final event.
    Servers that don't recognise the kwarg either ignore it or 400."""
    chunks = [_delta_chunk(finish_reason="stop")]
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    stub_client = _StubClient(chunks)
    adapter._client = stub_client

    async def run() -> None:
        async for _ in await adapter.generate(
            [Message(role="user", content="hi")], stream=True
        ):
            pass

    asyncio.run(run())
    assert stub_client.chat.completions.received_kwargs["stream_options"] == {
        "include_usage": True
    }


# ---------------------------------------------------------------------------
# embed / count_tokens guards
# ---------------------------------------------------------------------------


def test_chat_adapter_rejects_embed() -> None:
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.embed("hi"))


def test_chat_adapter_rejects_count_tokens() -> None:
    adapter = OpenAICompatibleAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.count_tokens([Message(role="user", content="hi")]))


def test_embeddings_adapter_rejects_generate() -> None:
    adapter = OpenAICompatibleEmbeddingsAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.generate([Message(role="user", content="hi")]))


# ---------------------------------------------------------------------------
# Embeddings translation
# ---------------------------------------------------------------------------


class _StubEmbeddings:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.received_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.received_kwargs = kwargs
        return self._response


def test_embeddings_returns_vectors_in_order() -> None:
    response = SimpleNamespace(data=[
        SimpleNamespace(embedding=[0.1, 0.2]),
        SimpleNamespace(embedding=[0.3, 0.4]),
    ])
    adapter = OpenAICompatibleEmbeddingsAdapter(
        concrete_model="bge-small", api_key="EMPTY", base_url="http://x/v1"
    )
    adapter._client = SimpleNamespace(embeddings=_StubEmbeddings(response))

    out = asyncio.run(adapter.embed(["a", "b"]))
    assert out == [[0.1, 0.2], [0.3, 0.4]]


def test_embeddings_string_input_wrapped_in_list() -> None:
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])])
    adapter = OpenAICompatibleEmbeddingsAdapter(
        concrete_model="m", api_key="EMPTY", base_url="http://x/v1"
    )
    stub_emb = _StubEmbeddings(response)
    adapter._client = SimpleNamespace(embeddings=stub_emb)

    out = asyncio.run(adapter.embed("hello"))
    assert out == [[1.0]]
    # Wire format must be `input=[...]`, not `input="..."`.
    assert stub_emb.received_kwargs["input"] == ["hello"]


# ---------------------------------------------------------------------------
# LlmService integration: base_url makes distinct cache slots
# ---------------------------------------------------------------------------


def test_base_url_is_part_of_adapter_cache_key() -> None:
    """Two presets pointing at different local servers must end up in
    distinct adapter cache slots. Otherwise rotating one server's URL
    would silently re-route the other preset's traffic."""
    from bp_router.llm.presets import Preset
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]

    a = Preset(
        name="vllm-a",
        provider="openai-compatible",
        concrete_model="qwen2.5",
        api_key_ref="",
        base_url="http://server-a:8000/v1",
    )
    b = Preset(
        name="vllm-b",
        provider="openai-compatible",
        concrete_model="qwen2.5",
        api_key_ref="",
        base_url="http://server-b:8000/v1",
    )
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    # Pre-populate both cache slots with stub adapters so we can
    # observe which one the resolver returns without building real
    # OpenAI clients.
    sentinel_a, sentinel_b = object(), object()
    svc._adapters[
        f"openai-compatible::qwen2.5::{a.base_url}::{a.api_key_ref}"
    ] = sentinel_a  # type: ignore[assignment]
    svc._adapters[
        f"openai-compatible::qwen2.5::{b.base_url}::{b.api_key_ref}"
    ] = sentinel_b  # type: ignore[assignment]

    _, got_a, _ = svc._resolve_one(
        preset=a, temperature=None, max_tokens=None, provider_options=None
    )
    _, got_b, _ = svc._resolve_one(
        preset=b, temperature=None, max_tokens=None, provider_options=None
    )
    assert got_a is sentinel_a
    assert got_b is sentinel_b
