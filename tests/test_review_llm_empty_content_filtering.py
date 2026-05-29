"""R8 HIGH: LLM providers drop messages with empty content.

A `Message` with `content=""` (string) or `content=[]` (list) is a
no-information turn. The upstream APIs handle this inconsistently:

  - Anthropic: 400 ("messages[N].content: ... at least one block")
    for either empty string or empty list, on user OR assistant.
  - OpenAI Responses: 400 on empty input items
    ("messages: input items must have non-empty content").
  - Gemini: 400 on `{role: "model", parts: []}` — already filtered
    (see `gemini.py:673`).
  - openai_compatible (chat completions): strict servers (vLLM,
    llama.cpp's HTTP server) reject empty content; lenient ones
    accept but the turn is wasted tokens.

The empty case most commonly arises from
`Message.assistant_from_response(resp)` when `resp` was empty
(content_filter / length / error finish reasons) — the helper
emits `Message(role="assistant", content="")` (see
`bp_sdk/llm.py:114-115`). Without filtering, the very next API
call dies with a 400 that's hard to attribute to the previous
empty turn.

Fix: each provider's `_convert_messages` skips messages whose
content collapses to `""` / `[]`. Tool messages (function results)
are exempt because they always carry a `tool_use_id`-bound block
the API needs.
"""
from __future__ import annotations

from bp_router.llm.service import Message

# ===========================================================================
# Anthropic
# ===========================================================================


def test_anthropic_drops_empty_assistant_string_content() -> None:
    """An assistant message with `content=""` (as emitted by
    `assistant_from_response` when the response had no text / tools)
    is filtered out — sending it would 400 the next turn."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content=""),
    ]
    converted, _ = _convert_messages(msgs)
    assert len(converted) == 1
    assert converted[0]["role"] == "user"


def test_anthropic_drops_empty_assistant_list_content() -> None:
    """A list-shaped assistant message whose parts all got dropped
    by `_convert_part` (e.g. all foreign reasoning blocks from
    another provider) → empty list → filtered."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="user", content="hi"),
        # All parts will be dropped/converted (no-op text-empty +
        # nothing else); the assistant content is effectively empty.
        Message(role="assistant", content=[]),
    ]
    converted, _ = _convert_messages(msgs)
    assert len(converted) == 1
    assert converted[0]["role"] == "user"


def test_anthropic_drops_empty_user_string_content() -> None:
    """An isolated user message with `content=""` (no pending
    tool_results to merge with) is filtered."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="user", content=""),
        Message(role="assistant", content="ok"),
    ]
    converted, _ = _convert_messages(msgs)
    assert len(converted) == 1
    assert converted[0]["role"] == "assistant"


def test_anthropic_drops_empty_user_list_content() -> None:
    """An isolated user message whose content list is empty (every
    part filtered) is dropped."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="user", content=[]),
        Message(role="assistant", content="ok"),
    ]
    converted, _ = _convert_messages(msgs)
    assert len(converted) == 1
    assert converted[0]["role"] == "assistant"


def test_anthropic_keeps_user_message_with_pending_tool_results() -> None:
    """A user message with `content=""` IS retained when pending
    tool_results exist — the merged message ends up with the tool_results
    as the content, which is non-empty. Pin that the empty-content
    skip doesn't accidentally drop tool_result deliveries."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "x"}},
        ]),
        Message(role="tool", tool_call_id="tu_1", content="result text"),
        # Empty-content user "kick" — only meaningful because the
        # pending tool_result rides along.
        Message(role="user", content=""),
    ]
    converted, _ = _convert_messages(msgs)
    # The empty user content should still produce a merged user
    # message containing the tool_result block (not drop it).
    user_msgs = [m for m in converted if m["role"] == "user"]
    assert len(user_msgs) == 1
    content = user_msgs[0]["content"]
    assert isinstance(content, list)
    assert any(b.get("type") == "tool_result" for b in content)


def test_anthropic_keeps_non_empty_messages() -> None:
    """Sanity: the filter doesn't eat legitimate non-empty content."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="user", content="real question"),
        Message(role="assistant", content="real answer"),
    ]
    converted, _ = _convert_messages(msgs)
    assert len(converted) == 2
    assert converted[0]["content"] == "real question"
    assert converted[1]["content"] == "real answer"


# ===========================================================================
# OpenAI Responses
# ===========================================================================


def test_openai_responses_drops_empty_user_string_content() -> None:
    """OpenAI Responses' assistant already skips empty content
    (`openai.py:266`). This pins the matching user-side behaviour."""
    from bp_router.llm.providers.openai import _convert_messages

    msgs = [
        Message(role="user", content=""),
        Message(role="assistant", content="ok"),
    ]
    items, _ = _convert_messages(msgs)
    # Only the assistant message survives.
    user_items = [it for it in items if it.get("role") == "user"]
    assert user_items == []


def test_openai_responses_drops_empty_user_list_content() -> None:
    """When every part of a list-form user message gets filtered
    by `_convert_user_part` (foreign reasoning, unsupported types),
    the resulting empty list is dropped."""
    from bp_router.llm.providers.openai import _convert_messages

    msgs = [
        Message(role="user", content=[]),
        Message(role="user", content="real question"),
    ]
    items, _ = _convert_messages(msgs)
    user_items = [it for it in items if it.get("role") == "user"]
    assert len(user_items) == 1
    assert user_items[0]["content"] == "real question"


def test_openai_responses_keeps_non_empty_user_content() -> None:
    """Sanity: the filter doesn't drop legitimate user content."""
    from bp_router.llm.providers.openai import _convert_messages

    msgs = [Message(role="user", content="hello")]
    items, _ = _convert_messages(msgs)
    assert len(items) == 1
    assert items[0]["content"] == "hello"


# ===========================================================================
# OpenAI Compatible (chat completions)
# ===========================================================================


def test_openai_compat_drops_empty_user_content() -> None:
    """vLLM strict mode and llama.cpp's HTTP server reject
    `{"role": "user", "content": ""}` with a 400. Filter at the
    adapter layer so the request goes through."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="user", content=""),
        Message(role="user", content="real q"),
    ]
    out = _convert_messages(msgs)
    user_msgs = [m for m in out if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "real q"


def test_openai_compat_drops_empty_assistant_with_no_tool_calls() -> None:
    """Empty assistant content with NO tool_calls is a no-information
    turn — filter rather than emit. (Empty content WITH tool_calls
    is kept because chat completions allows `content=null` to carry
    the tool_calls array.)"""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=""),
        Message(role="user", content="next"),
    ]
    out = _convert_messages(msgs)
    asst = [m for m in out if m["role"] == "assistant"]
    assert asst == []


def test_openai_compat_keeps_empty_content_when_tool_calls_present() -> None:
    """The `content=null` shape is required to carry tool_calls.
    Pin that the empty-content filter doesn't strip these."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_1", "name": "f", "input": {"x": 1}},
        ]),
    ]
    out = _convert_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["id"] == "call_1"


def test_openai_compat_drops_user_list_with_all_empty_text() -> None:
    """List-of-text-parts that collapses to `""` after joining → drop."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="user", content=[{"text": ""}, {"text": ""}]),
        Message(role="user", content="real q"),
    ]
    out = _convert_messages(msgs)
    user_msgs = [m for m in out if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "real q"


def test_openai_compat_drops_user_list_with_all_filtered_parts() -> None:
    """If `_convert_user_part` filters every part (e.g. all foreign
    reasoning blocks), the remaining `parts` is empty → drop."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [
        Message(role="user", content=[
            {"type": "thinking", "thinking": "..."},
            {"type": "redacted_thinking", "data": "..."},
        ]),
        Message(role="user", content="real q"),
    ]
    out = _convert_messages(msgs)
    user_msgs = [m for m in out if m["role"] == "user"]
    assert len(user_msgs) == 1


def test_openai_compat_keeps_non_empty_user_text() -> None:
    """Sanity: legitimate text isn't filtered."""
    from bp_router.llm.providers.openai_compatible import _convert_messages

    msgs = [Message(role="user", content="hi")]
    out = _convert_messages(msgs)
    assert len(out) == 1
    assert out[0]["content"] == "hi"
