"""R8 HIGH: Anthropic system message preserves list-form blocks.

Anthropic's `system=` kwarg on `messages.create` accepts:
  - a plain string (no prompt caching), or
  - a list of `{"type": "text", "text": ..., "cache_control": ...}`
    blocks — the **only** way to enable Anthropic's prompt caching
    (5-min ephemeral cache, 90% input-token discount on cache hit).

Pre-R8 the adapter silently dropped list-form system messages:

```python
if m.role == "system":
    if isinstance(m.content, str):
        system = ...
    continue   # list-form ignored entirely
```

So an agent that built `Message(role="system", content=[{"type":
"text", "text": "...", "cache_control": {"type": "ephemeral"}}])`
to enable caching got **no caching at all** and **no system
prompt at all** — the message vanished. The 90% discount on a
typical 5k-token system block (= ~$0.014/turn at Sonnet 4 input
pricing) was silently lost on every call. Even worse, the bare
adapter would route those calls without a system prompt
whatsoever, producing different model behaviour than the agent
configured.

The fix: when any system message has list content, build a list
of system blocks; pass `cache_control` markers through verbatim.
"""
from __future__ import annotations

import pytest

from bp_router.llm.service import Message


def test_system_list_content_preserves_cache_control() -> None:
    """An agent provides a system message with a cache_control marker.
    The adapter must pass the block through to Anthropic verbatim so
    prompt caching kicks in."""
    from bp_router.llm.providers.anthropic import _convert_messages

    system_blocks = [
        {
            "type": "text",
            "text": "You are an expert reviewer." * 100,  # long enough to cache
            "cache_control": {"type": "ephemeral"},
        },
    ]
    msgs = [
        Message(role="system", content=system_blocks),
        Message(role="user", content="review this PR"),
    ]
    _, system = _convert_messages(msgs)

    # System should be a list (not str) so cache_control survives.
    assert isinstance(system, list)
    assert len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_system_string_content_stays_string_form() -> None:
    """Back-compat: plain string system messages stay string-form
    so callers that don't use caching see no shape change."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="system", content="be helpful"),
        Message(role="user", content="hi"),
    ]
    _, system = _convert_messages(msgs)

    assert isinstance(system, str)
    assert system == "be helpful"


def test_multiple_string_system_messages_concatenated() -> None:
    """Multiple string-form system messages still concatenate with
    newlines (legacy behaviour preserved)."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="system", content="rule 1"),
        Message(role="system", content="rule 2"),
        Message(role="user", content="go"),
    ]
    _, system = _convert_messages(msgs)

    assert isinstance(system, str)
    assert system == "rule 1\nrule 2"


def test_string_then_list_system_messages_upgrades_to_list() -> None:
    """If a string system message comes first and a list-form one
    follows, upgrade to list form so the cache_control on the
    second block survives. The earlier string becomes its own
    text block at the head of the list."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="system", content="be helpful"),
        Message(role="system", content=[
            {
                "type": "text",
                "text": "extra rules" * 50,
                "cache_control": {"type": "ephemeral"},
            },
        ]),
        Message(role="user", content="go"),
    ]
    _, system = _convert_messages(msgs)

    assert isinstance(system, list)
    assert len(system) == 2
    assert system[0] == {"type": "text", "text": "be helpful"}
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_list_then_string_system_messages_appended_to_list() -> None:
    """A string system message after a list-form one is appended
    as another text block (not merged into the prior block)."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="system", content=[
            {
                "type": "text",
                "text": "cached block",
                "cache_control": {"type": "ephemeral"},
            },
        ]),
        Message(role="system", content="trailing text"),
        Message(role="user", content="go"),
    ]
    _, system = _convert_messages(msgs)

    assert isinstance(system, list)
    assert len(system) == 2
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1] == {"type": "text", "text": "trailing text"}


def test_neutral_text_part_translated_with_cache_control() -> None:
    """The neutral `{"text": "..."}` shape (with no `type` tag)
    still translates correctly when `cache_control` rides along.
    Defends the cache marker propagation through the neutral-shape
    branch."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="system", content=[
            {"text": "neutral form text", "cache_control": {"type": "ephemeral"}},
        ]),
        Message(role="user", content="go"),
    ]
    _, system = _convert_messages(msgs)

    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "neutral form text"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_no_system_message_returns_none() -> None:
    """Sanity: with no system messages, system is None."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [Message(role="user", content="hi")]
    _, system = _convert_messages(msgs)

    assert system is None


def test_empty_list_system_blocks_after_upgrade_returns_none() -> None:
    """If the only system message is a list of all-invalid blocks
    (no `type`, no `text`), nothing makes it onto `system_blocks`;
    we should return None rather than `[]` (which Anthropic would
    400 on as 'system must be string or non-empty array')."""
    from bp_router.llm.providers.anthropic import _convert_messages

    msgs = [
        Message(role="system", content=[
            {"garbage": "yes"},
        ]),
        Message(role="user", content="hi"),
    ]
    _, system = _convert_messages(msgs)

    assert system is None


def test_create_kwargs_passes_system_list_through() -> None:
    """End-to-end: `_build_create_kwargs` puts the list-form system
    into the create kwargs so it actually reaches Anthropic."""
    from bp_router.llm.providers.anthropic import _build_create_kwargs

    msgs = [
        Message(role="system", content=[
            {
                "type": "text",
                "text": "cached",
                "cache_control": {"type": "ephemeral"},
            },
        ]),
        Message(role="user", content="hi"),
    ]
    kwargs = _build_create_kwargs(
        concrete_model="claude-sonnet-4-6",
        messages=msgs,
        tools=None,
        tool_choice=None,
        temperature=None,
        max_tokens=None,
        provider_options=None,
    )

    assert "system" in kwargs
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
