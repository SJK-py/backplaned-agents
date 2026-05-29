"""R8 MEDIUM: LLM provider translation correctness (5 batched).

Fresh-eyes R8 MEDIUM pass over the provider layer. Five
provider-translation correctness gaps:

#6 openai_compatible dropped neutral `{"function_call": {...}}`
   assistant parts (the shape `Message.assistant_from_response`
   emits) — only `type=="tool_use"` was recognised. An SDK-helper-
   built or cross-provider assistant turn carrying tool calls was
   dropped whole, so the following `role="tool"` message
   referenced a call the model never saw.

#7 openai_compatible streaming flushed truncated tool-call buffers
   as `ToolCall(args={"_raw": "<partial>"})` even when the stream
   was cut off (`finish_reason=length`) — the agent then executed
   a tool with a bogus payload.

#1 Anthropic streaming `message_delta.usage` zeros input/cache
   tokens (only output grows); a consumer reading the last usage
   delta saw `input_tokens=0`, corrupting cost accounting.

#3 `_block_to_dict`'s fallback called `block.model_dump()` —
   directly contradicting its own docstring (SDK metadata the API
   rejects on round-trip) — a latent 400 for any future reasoning
   block subtype.

#9 A Gemini-shaped `tool_choice` reaching the Anthropic adapter
   was forwarded verbatim → guaranteed opaque 400.
"""
from __future__ import annotations

import inspect

import pytest

from bp_router.llm.service import Message

# ===========================================================================
# #6 openai_compatible: neutral function_call assistant turn preserved
# ===========================================================================


def test_compat_neutral_function_call_becomes_tool_call() -> None:
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "call_1", "name": "search",
                               "args": {"q": "weather"}}},
        ]),
    ]
    out = _convert_messages(msgs)
    assert len(out) == 1
    asst = out[0]
    assert asst["role"] == "assistant"
    # content=null (chat-completions tool-call shape), tool_calls set.
    assert asst["content"] is None
    assert len(asst["tool_calls"]) == 1
    tc = asst["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "search"
    import json
    assert json.loads(tc["function"]["arguments"]) == {"q": "weather"}


def test_compat_neutral_function_call_input_key_fallback() -> None:
    """`input` is the alternate args key (Anthropic-flavoured
    neutral). Must still translate."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "c2", "name": "f", "input": {"x": 1}}},
        ]),
    ]
    out = _convert_messages(msgs)
    import json
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}


def test_compat_function_call_string_args_passthrough() -> None:
    """Pre-serialized string args must not be double-encoded."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"function_call": {"id": "c", "name": "f", "args": '{"a":1}'}},
        ]),
    ]
    out = _convert_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"a":1}'


def test_compat_text_then_function_call_both_preserved() -> None:
    """A turn with text AND a neutral function_call keeps both —
    text in content, call in tool_calls. Pre-R8 the call vanished."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"text": "Let me look."},
            {"function_call": {"id": "c", "name": "f", "args": {}}},
        ]),
    ]
    out = _convert_messages(msgs)
    assert out[0]["content"] == "Let me look."
    assert out[0]["tool_calls"][0]["id"] == "c"


def test_compat_anthropic_tool_use_still_works() -> None:
    """Sanity: the original tool_use shape is unaffected."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "tu", "name": "f", "input": {"y": 2}},
        ]),
    ]
    out = _convert_messages(msgs)
    assert out[0]["tool_calls"][0]["id"] == "tu"


# ===========================================================================
# #7 openai_compatible streaming: drop truncated tool buffers
# ===========================================================================


def _stub_stream(chunks: list, finish_reason: str):  # type: ignore[no-untyped-def]
    """Build an async-iterable of stub chat-completion stream chunks
    plus a final finish chunk. Each chunk is a SimpleNamespace
    mirroring the openai SDK delta shape the adapter reads."""
    from types import SimpleNamespace

    async def _gen():  # type: ignore[no-untyped-def]
        for c in chunks:
            yield c
        # Terminal chunk carrying the finish_reason.
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=finish_reason,
            )],
            usage=None,
        )

    return _gen()


def test_compat_stream_drops_truncated_tool_call_on_length() -> None:
    """Source pin + behavioural intent: on a `length`/`content_filter`
    /`error` finish, an unparseable (truncated) tool buffer is
    dropped, NOT emitted as {"_raw": ...}."""
    from bp_router.llm.providers import openai_compatible as mod

    src = inspect.getsource(mod.OpenAICompatibleAdapter._generate_stream)
    assert "openai_compat_dropped_truncated_tool_call" in src
    # The drop is gated on a truncated finish, not unconditional.
    assert 'mapped_finish in ("length", "content_filter", "error")' in src
    # The non-truncated path still surfaces {"_raw": ...} (visible
    # malformed server response on a claimed-complete stream).
    assert '{"_raw": raw_args}' in src
    # And the drop path `continue`s past the yield.
    drop_idx = src.index("openai_compat_dropped_truncated_tool_call")
    raw_idx = src.index('{"_raw": raw_args}')
    # The truncated-drop branch precedes the _raw fallback.
    assert drop_idx < raw_idx


def test_compat_finish_reason_mapping_unchanged() -> None:
    from bp_router.llm.providers.openai_compatible import _finish_reason

    assert _finish_reason("length") == "length"
    assert _finish_reason("stop") == "stop"
    assert _finish_reason("abort") == "error"
    assert _finish_reason(None) == "stop"


# ===========================================================================
# #1 Anthropic streaming usage self-consistency
# ===========================================================================


def test_anthropic_stream_carries_input_cache_into_message_delta() -> None:
    """Source pin: message_start input/cache figures are captured and
    backfilled into the message_delta usage so the terminal usage
    delta is self-consistent (not input_tokens=0)."""
    from bp_router.llm.providers import anthropic as mod

    src = inspect.getsource(mod.AnthropicAdapter._generate_stream)
    assert "start_input_tokens" in src
    assert "start_cache_read" in src
    assert "start_cache_write" in src
    # message_start captures them.
    assert "start_input_tokens = u.input_tokens" in src
    # message_delta backfills via `or start_*`.
    assert "usage.input_tokens or start_input_tokens" in src
    assert "usage.cache_read_tokens or start_cache_read" in src
    assert "usage.cache_write_tokens or start_cache_write" in src


def test_usage_from_anthropic_zeros_when_missing() -> None:
    """Baseline: a message_delta-like usage object with no
    input_tokens/cache fields yields zeros (the bug the backfill
    compensates for)."""
    from types import SimpleNamespace

    from bp_router.llm.providers.anthropic import _usage_from_anthropic

    u = _usage_from_anthropic(SimpleNamespace(output_tokens=99))
    assert u.input_tokens == 0
    assert u.output_tokens == 99
    assert u.cache_read_tokens == 0
    assert u.cache_write_tokens == 0


# ===========================================================================
# #3 _block_to_dict: no model_dump fallback
# ===========================================================================


def test_block_to_dict_no_model_dump_fallback() -> None:
    import ast
    import textwrap

    from bp_router.llm.providers import anthropic as mod

    src = inspect.getsource(mod._block_to_dict)
    # Robust: parse the function and assert NO call references the
    # name `model_dump` anywhere (comments/docstrings legitimately
    # mention it to explain why it's avoided, so a substring check
    # is too brittle). Catches `x.model_dump()`, `md()` where
    # `md = getattr(block, "model_dump")`, etc. — for the getattr
    # form we also assert the string literal isn't fetched.
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "model_dump", (
                "_block_to_dict still references .model_dump — the "
                "contract-violating fallback must be removed"
            )
        if isinstance(node, ast.Call):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == "model_dump":
                    raise AssertionError(
                        "_block_to_dict still does getattr(..., "
                        "'model_dump') — fallback not removed"
                    )
    # Unhandled types are logged for maintainer visibility.
    assert "anthropic_unhandled_reasoning_block_type" in src


def test_block_to_dict_thinking_unchanged() -> None:
    """Sanity: the explicit thinking/redacted branches still produce
    the exact round-trip shape (no regression from the fallback
    rewrite)."""
    from types import SimpleNamespace

    from bp_router.llm.providers.anthropic import _block_to_dict

    tb = SimpleNamespace(type="thinking", thinking="reasoning...",
                         signature="sig123")
    assert _block_to_dict(tb) == {
        "type": "thinking",
        "thinking": "reasoning...",
        "signature": "sig123",
    }
    rb = SimpleNamespace(type="redacted_thinking", data="opaque")
    assert _block_to_dict(rb) == {
        "type": "redacted_thinking",
        "data": "opaque",
    }


def test_block_to_dict_unknown_type_projects_whitelist_not_metadata() -> None:
    """An unknown reasoning subtype with a model_dump that WOULD
    leak rejected metadata: the fallback must project only the
    whitelisted opaque fields via getattr, never call model_dump."""
    class _FutureBlock:
        type = "thinking_v2"
        signature = "sig-future"

        def model_dump(self):
            # If the fallback ever calls this, the test fails — this
            # is exactly the rejected-metadata shape the docstring
            # warns about.
            return {"type": "thinking_v2", "signature": "sig-future",
                    "_sdk_internal": "REJECTED_BY_API"}

    from bp_router.llm.providers.anthropic import _block_to_dict

    out = _block_to_dict(_FutureBlock())
    assert out["type"] == "thinking_v2"
    assert out["signature"] == "sig-future"
    assert "_sdk_internal" not in out


# ===========================================================================
# #9 Anthropic tool_choice: reject foreign Gemini-shaped dict
# ===========================================================================


def test_anthropic_tool_choice_rejects_gemini_shape() -> None:
    from bp_router.llm.providers.anthropic import _convert_tool_choice

    with pytest.raises(ValueError, match="function_calling_config"):
        _convert_tool_choice({"function_calling_config": {"mode": "ANY"}})


def test_anthropic_tool_choice_passes_through_native_dict() -> None:
    """An Anthropic-shaped dict is still forwarded — only the
    unambiguous Gemini key is rejected."""
    from bp_router.llm.providers.anthropic import _convert_tool_choice

    native = {"type": "tool", "name": "search"}
    assert _convert_tool_choice(native) == native
    auto_par = {"type": "auto", "disable_parallel_tool_use": True}
    assert _convert_tool_choice(auto_par) == auto_par


def test_anthropic_tool_choice_string_forms_unchanged() -> None:
    from bp_router.llm.providers.anthropic import _convert_tool_choice

    assert _convert_tool_choice("auto") == {"type": "auto"}
    assert _convert_tool_choice("none") == {"type": "none"}
    assert _convert_tool_choice("required") == {"type": "any"}
    assert _convert_tool_choice(None) is None
