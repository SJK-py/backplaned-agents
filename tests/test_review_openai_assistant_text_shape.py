"""R8 HIGH: OpenAI Responses assistant text uses canonical shape.

The Responses API emits an assistant turn as:

    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "..."}]}

When round-tripping that turn back into the next request's `input`
array, the API accepts the easy bare-string form
(`{"role": "assistant", "content": "<str>"}`) ONLY for a
standalone assistant message. In **stateless mode** (no
`previous_response_id` — the mode this adapter is built for, see
the module docstring), the Responses API pairs each `reasoning`
item with the `message` item that immediately follows it. A
bare-string assistant message breaks that pairing:

  - the API 400s ("reasoning item must be followed by a message
    item"), OR
  - it silently drops the reasoning context, degrading multi-turn
    tool-use quality (the model loses its own chain of thought
    across turns).

Fix: `_assistant_text_item()` emits the canonical
`type: message` + `output_text` shape — mirroring exactly what
the API produced — for both the string-content and
list-content assistant paths.
"""
from __future__ import annotations

import inspect

from bp_router.llm.providers.openai import _convert_messages
from bp_router.llm.service import Message


def test_assistant_string_uses_message_output_text_shape() -> None:
    items, _ = _convert_messages([Message(role="assistant", content="hello")])
    assert items == [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello"}],
    }]


def test_assistant_list_text_uses_message_output_text_shape() -> None:
    items, _ = _convert_messages([
        Message(role="assistant", content=[{"text": "part one "}, {"text": "part two"}]),
    ])
    assert len(items) == 1
    assert items[0] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "part one part two"}],
    }


def test_reasoning_then_text_emits_reasoning_then_message_item() -> None:
    """The exact failure mode: a reasoning block round-tripped from a
    prior response, followed by the assistant's answer text. The
    `reasoning` item must be immediately followed by a `message`
    item (not a bare-string assistant) so the API can pair them in
    stateless mode."""
    items, _ = _convert_messages([
        Message(role="assistant", content=[
            {"type": "reasoning", "id": "rs_1",
             "encrypted_content": "<blob>", "summary": []},
            {"text": "The answer is 42."},
        ]),
    ])
    assert len(items) == 2
    assert items[0]["type"] == "reasoning"
    assert items[0]["id"] == "rs_1"
    # The message item must carry the canonical structured content
    # so the reasoning↔message pairing survives.
    assert items[1] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "The answer is 42."}],
    }


def test_text_then_function_call_keeps_message_shape_and_order() -> None:
    """text → function_call: the text becomes a structured message
    item BEFORE the standalone function_call item (positional order
    is meaningful to the Responses API)."""
    items, _ = _convert_messages([
        Message(role="assistant", content=[
            {"text": "Let me look that up."},
            {"function_call": {"id": "call_x", "name": "search",
                               "args": {"q": "weather"}}},
        ]),
    ])
    assert len(items) == 2
    assert items[0] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Let me look that up."}],
    }
    assert items[1]["type"] == "function_call"
    assert items[1]["call_id"] == "call_x"


def test_empty_assistant_string_still_dropped() -> None:
    """The empty-content skip (separate R8 fix) must still hold —
    the canonical-shape change must not resurrect empty turns."""
    items, _ = _convert_messages([Message(role="assistant", content="")])
    assert items == []


def test_assistant_list_all_empty_text_dropped() -> None:
    """A list of empty text parts collapses to nothing — no empty
    `message` item with an empty `output_text` block."""
    items, _ = _convert_messages([
        Message(role="assistant", content=[{"text": ""}, {"text": ""}]),
    ])
    assert items == []


def test_interleaved_text_call_text_emits_two_message_items() -> None:
    """text → function_call → text produces: message, function_call,
    message — each text span its own structured message item, order
    preserved."""
    items, _ = _convert_messages([
        Message(role="assistant", content=[
            {"text": "First, "},
            {"function_call": {"id": "c1", "name": "f", "args": {}}},
            {"text": "now the result."},
        ]),
    ])
    assert len(items) == 3
    assert items[0] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "First, "}],
    }
    assert items[1]["type"] == "function_call"
    assert items[2] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "now the result."}],
    }


def test_user_content_unaffected_by_assistant_shape_change() -> None:
    """Sanity: user messages still use the easy/list form — the
    `output_text` shape is assistant-only (user uses input_text)."""
    items, _ = _convert_messages([Message(role="user", content="hi")])
    assert items == [{"role": "user", "content": "hi"}]


def test_convert_uses_assistant_text_item_helper() -> None:
    """Source pin: both assistant-text emission paths go through the
    single `_assistant_text_item` helper so the canonical shape
    can't drift between the str-content and list-content branches."""
    from bp_router.llm.providers import openai as mod

    src = inspect.getsource(mod._convert_messages)
    # Both the string branch and the _flush_text helper must call it.
    assert src.count("_assistant_text_item(") >= 2
    # And the helper itself emits the canonical triplet.
    helper_src = inspect.getsource(mod._assistant_text_item)
    assert '"type": "message"' in helper_src
    assert '"type": "output_text"' in helper_src
