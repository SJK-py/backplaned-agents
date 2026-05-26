"""R8 HIGH: skip cross-provider fallback hops when tool_call_ids are
in the message history.

Tool-call IDs are provider-specific:
  - Anthropic emits `toolu_<hex>`
  - OpenAI Responses emits `call_<hex>`
  - Gemini emits an integer in `function_call.id`

When an agent calls `LlmService.generate(messages, preset="claude")`
on turn N+1, and `messages` already carries `tool_call_id="toolu_x"`
from turn N, falling back to a different-provider preset (say
`gpt-4o`) sends the stale Anthropic-flavoured ID to OpenAI. The
destination provider 400s with "unknown call_id" / "no such
tool_use_id in conversation", OR silently misroutes the result —
either way the conversation is corrupted.

Fix: `_call_with_fallback` accepts `has_tool_call_history`; when
True, fallback targets whose `provider` differs from the root
preset's `provider` get skipped (with a metric +
`llm_fallback_skipped_provider` log line). Same-provider fallback
(e.g. `gpt-4o` → `gpt-4o-mini` within OpenAI) is still permitted.

`generate` populates the flag from the messages list via the new
`_messages_have_tool_call_ids(messages)` helper, which inspects:
  - `m.tool_call_id` on `role="tool"` messages
  - `tool_use`, `function_call`, `function_call_output` blocks
    embedded in assistant content (round-tripped from a prior turn)
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helper detection
# ---------------------------------------------------------------------------


def test_detect_tool_call_id_on_tool_message() -> None:
    from bp_router.llm.service import Message, _messages_have_tool_call_ids

    msgs = [
        Message(role="user", content="hi"),
        Message(role="tool", tool_call_id="toolu_abc", content="result"),
    ]
    assert _messages_have_tool_call_ids(msgs) is True


def test_detect_no_tool_calls_returns_false() -> None:
    from bp_router.llm.service import Message, _messages_have_tool_call_ids

    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="bye"),
    ]
    assert _messages_have_tool_call_ids(msgs) is False


def test_detect_anthropic_tool_use_block_in_assistant_content() -> None:
    """A round-tripped Anthropic assistant turn carries
    `{"type": "tool_use", "id": "toolu_x"}` blocks. Detection
    catches those even if no role=tool message has been seen yet."""
    from bp_router.llm.service import Message, _messages_have_tool_call_ids

    msgs = [
        Message(role="user", content="search"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "toolu_abc", "name": "search",
             "input": {"q": "x"}},
        ]),
    ]
    assert _messages_have_tool_call_ids(msgs) is True


def test_detect_neutral_function_call_block() -> None:
    """The neutral assistant-round-trip shape carries
    `{"function_call": {"id": "...", "name": "...", "args": ...}}`."""
    from bp_router.llm.service import Message, _messages_have_tool_call_ids

    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content=[
            {"function_call": {"id": "call_x", "name": "f", "args": {}}},
        ]),
    ]
    assert _messages_have_tool_call_ids(msgs) is True


def test_detect_openai_function_call_item() -> None:
    """OpenAI Responses native function_call items have a `call_id`
    field (not `id`). The detection branch checks `call_id` so this
    shape is also caught."""
    from bp_router.llm.service import Message, _messages_have_tool_call_ids

    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", content=[
            {"type": "function_call", "call_id": "call_x",
             "name": "f", "arguments": "{}"},
        ]),
    ]
    assert _messages_have_tool_call_ids(msgs) is True


def test_detect_anthropic_tool_result_reference() -> None:
    """A pending `tool_result` block referring to a `tool_use_id`
    still counts — the conversation is mid-tool-use."""
    from bp_router.llm.service import Message, _messages_have_tool_call_ids

    msgs = [
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "toolu_x",
             "content": "result"},
        ]),
    ]
    assert _messages_have_tool_call_ids(msgs) is True


# ---------------------------------------------------------------------------
# Fallback orchestration
# ---------------------------------------------------------------------------


def test_fallback_skips_cross_provider_when_tool_calls_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root preset is Anthropic, fallback is OpenAI. Messages carry
    tool_call_ids → fallback skipped → chain exhausts → last error
    surfaces."""
    pytest.importorskip("pydantic")
    from bp_router.llm.service import LlmUpstreamError
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    root = make_preset("claude-primary", provider="anthropic", fallback_preset="gpt-fallback")
    fb = make_preset("gpt-fallback", provider="openai")
    svc._register_preset_for_test(root)
    svc._register_preset_for_test(fb)
    root_stub = wire_stub_adapter(svc, root)
    fb_stub = wire_stub_adapter(svc, fb)
    root_stub.push(RuntimeError("primary failed"))
    # Even though OpenAI fallback is wired and would succeed,
    # we should skip it.
    fb_stub.push("OK_FROM_OPENAI")

    async def _attempt(preset):
        if preset.name == root.name:
            return root_stub, await root_stub.generate()
        return fb_stub, await fb_stub.generate()

    with pytest.raises(LlmUpstreamError):
        asyncio.run(svc._call_with_fallback(
            preset_name="claude-primary",
            user_level="admin",
            attempt=_attempt,
            has_tool_call_history=True,
        ))

    # The OpenAI fallback adapter should NOT have been called.
    assert fb_stub.calls == 0
    # Root was called once and failed.
    assert root_stub.calls == 1


def test_fallback_allowed_when_no_tool_call_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same root/fallback combo, but no tool_call history. The
    cross-provider hop is allowed because there are no stale IDs."""
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    root = make_preset("claude-primary", provider="anthropic", fallback_preset="gpt-fallback")
    fb = make_preset("gpt-fallback", provider="openai")
    svc._register_preset_for_test(root)
    svc._register_preset_for_test(fb)
    root_stub = wire_stub_adapter(svc, root)
    fb_stub = wire_stub_adapter(svc, fb)
    root_stub.push(RuntimeError("primary failed"))
    fb_stub.push("OK_FROM_OPENAI")

    async def _attempt(preset):
        if preset.name == root.name:
            return root_stub, await root_stub.generate()
        return fb_stub, await fb_stub.generate()

    adapter, used_preset, result = asyncio.run(svc._call_with_fallback(
        preset_name="claude-primary",
        user_level="admin",
        attempt=_attempt,
        has_tool_call_history=False,
    ))

    assert used_preset == "gpt-fallback"
    assert result == "OK_FROM_OPENAI"
    assert fb_stub.calls == 1


def test_same_provider_fallback_allowed_even_with_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both root and fallback are OpenAI. Tool-call IDs from the
    root are safe on the fallback because the providers match.
    Pin that the safety check doesn't over-eagerly skip same-
    provider hops."""
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    root = make_preset("gpt-primary", provider="openai", fallback_preset="gpt-cheap")
    fb = make_preset("gpt-cheap", provider="openai")
    svc._register_preset_for_test(root)
    svc._register_preset_for_test(fb)
    root_stub = wire_stub_adapter(svc, root)
    fb_stub = wire_stub_adapter(svc, fb)
    root_stub.push(RuntimeError("primary failed"))
    fb_stub.push("OK_FROM_CHEAP")

    async def _attempt(preset):
        if preset.name == root.name:
            return root_stub, await root_stub.generate()
        return fb_stub, await fb_stub.generate()

    adapter, used_preset, result = asyncio.run(svc._call_with_fallback(
        preset_name="gpt-primary",
        user_level="admin",
        attempt=_attempt,
        has_tool_call_history=True,
    ))

    assert used_preset == "gpt-cheap"
    assert result == "OK_FROM_CHEAP"
    assert fb_stub.calls == 1


def test_fallback_chain_continues_past_skipped_cross_provider_to_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root=anthropic, fb1=openai (skipped due to tool_calls),
    fb2=anthropic (allowed, succeeds). The chain walks past the
    skipped link rather than treating it as a hard stop."""
    from tests.conftest import make_llm_service, make_preset, wire_stub_adapter

    svc = make_llm_service()
    root = make_preset("claude-a", provider="anthropic", fallback_preset="gpt-mid")
    fb1 = make_preset("gpt-mid", provider="openai", fallback_preset="claude-b")
    fb2 = make_preset("claude-b", provider="anthropic")
    svc._register_preset_for_test(root)
    svc._register_preset_for_test(fb1)
    svc._register_preset_for_test(fb2)
    root_stub = wire_stub_adapter(svc, root)
    fb1_stub = wire_stub_adapter(svc, fb1)
    fb2_stub = wire_stub_adapter(svc, fb2)
    root_stub.push(RuntimeError("root failed"))
    fb1_stub.push("UNREACHABLE")
    fb2_stub.push("OK_FROM_CLAUDE_B")

    async def _attempt(preset):
        if preset.name == root.name:
            return root_stub, await root_stub.generate()
        if preset.name == fb1.name:
            return fb1_stub, await fb1_stub.generate()
        return fb2_stub, await fb2_stub.generate()

    adapter, used_preset, result = asyncio.run(svc._call_with_fallback(
        preset_name="claude-a",
        user_level="admin",
        attempt=_attempt,
        has_tool_call_history=True,
    ))

    assert used_preset == "claude-b"
    assert result == "OK_FROM_CLAUDE_B"
    assert fb1_stub.calls == 0   # cross-provider skipped
    assert fb2_stub.calls == 1   # same-provider taken


# ---------------------------------------------------------------------------
# Source pin
# ---------------------------------------------------------------------------


def test_generate_passes_tool_call_flag_to_fallback() -> None:
    """Source-pin so a future refactor that drops the flag from
    `generate`'s call to `_call_with_fallback` regresses loudly."""
    import inspect

    from bp_router.llm import service as svc_mod

    src = inspect.getsource(svc_mod.LlmService.generate)
    assert "_messages_have_tool_call_ids(messages)" in src
    assert "has_tool_call_history=" in src
