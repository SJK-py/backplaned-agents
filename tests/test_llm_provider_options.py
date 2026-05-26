"""Tests for the Gemini provider's config-kwargs assembly.

Covers `provider_options` translation: thinking config (level / budget /
include_thoughts), media resolution (Gemini 3), provider-specific tool
blocks, the legacy `thinking_budget_tokens` alias, and the default
alias map. Pure unit tests — exercise the dict fallback path so
google-genai isn't required.
"""

from __future__ import annotations

import pytest

from bp_router.llm.providers.gemini import _build_config_kwargs
from bp_router.llm.service import LlmService, ToolSpec


def _build(provider_options=None, **kwargs):
    """Test helper: defaults + extracted dict pair."""
    cfg, thinking = _build_config_kwargs(
        tools=kwargs.pop("tools", None),
        tool_choice=kwargs.pop("tool_choice", None),
        temperature=kwargs.pop("temperature", None),
        max_tokens=kwargs.pop("max_tokens", None),
        provider_options=provider_options,
        system_instruction=kwargs.pop("system_instruction", None),
    )
    assert not kwargs, f"unexpected test kwargs: {kwargs}"
    return cfg, thinking


# ---------------------------------------------------------------------------
# Top-level kwargs
# ---------------------------------------------------------------------------


def test_build_config_empty() -> None:
    cfg, thinking = _build()
    assert cfg == {}
    assert thinking == {}


def test_temperature_and_max_tokens() -> None:
    cfg, _ = _build(temperature=0.7, max_tokens=1024)
    assert cfg["temperature"] == 0.7
    assert cfg["max_output_tokens"] == 1024


def test_system_instruction_passthrough() -> None:
    cfg, _ = _build(system_instruction="You are a cat. Your name is Neko.")
    assert cfg["system_instruction"] == "You are a cat. Your name is Neko."


# ---------------------------------------------------------------------------
# Thinking config
# ---------------------------------------------------------------------------


def test_thinking_level() -> None:
    cfg, thinking = _build(provider_options={"thinking_level": "low"})
    assert thinking == {"thinking_level": "low"}
    assert "thinking_level" not in cfg
    assert "thinking_config" not in cfg


def test_thinking_budget() -> None:
    cfg, thinking = _build(provider_options={"thinking_budget": 4096})
    assert thinking == {"thinking_budget": 4096}


def test_thinking_legacy_budget_tokens_aliased() -> None:
    """The old `thinking_budget_tokens` key (which previously got passed
    as a top-level GenerateContentConfig kwarg — broken in the SDK)
    now feeds `thinking_budget` inside ThinkingConfig."""
    cfg, thinking = _build(provider_options={"thinking_budget_tokens": 8192})
    assert thinking == {"thinking_budget": 8192}
    assert "thinking_budget_tokens" not in cfg


def test_thinking_explicit_takes_precedence_over_legacy() -> None:
    cfg, thinking = _build(provider_options={
        "thinking_budget": 1024,
        "thinking_budget_tokens": 8192,  # ignored when new key present
    })
    assert thinking == {"thinking_budget": 1024}


def test_thinking_level_plus_budget_combined() -> None:
    cfg, thinking = _build(provider_options={
        "thinking_level": "high",
        "thinking_budget": 16384,
        "include_thoughts": True,
    })
    assert thinking == {
        "thinking_level": "high",
        "thinking_budget": 16384,
        "include_thoughts": True,
    }


def test_disable_thinking_via_zero_budget() -> None:
    """Per Gemini 2.5 docs, thinking_budget=0 turns thinking off."""
    cfg, thinking = _build(provider_options={"thinking_budget": 0})
    assert thinking == {"thinking_budget": 0}


def test_dynamic_thinking_via_minus_one() -> None:
    """thinking_budget=-1 enables dynamic thinking on Gemini 2.5."""
    cfg, thinking = _build(provider_options={"thinking_budget": -1})
    assert thinking == {"thinking_budget": -1}


# ---------------------------------------------------------------------------
# Media resolution + other passthroughs (Gemini 3)
# ---------------------------------------------------------------------------


def test_media_resolution_passthrough() -> None:
    cfg, _ = _build(provider_options={"media_resolution": "high"})
    assert cfg["media_resolution"] == "high"


def test_media_resolution_with_other_passthroughs() -> None:
    cfg, _ = _build(provider_options={
        "media_resolution": "medium",
        "response_mime_type": "application/json",
        "response_schema": {"type": "object"},
    })
    assert cfg["media_resolution"] == "medium"
    assert cfg["response_mime_type"] == "application/json"
    assert cfg["response_schema"] == {"type": "object"}


# ---------------------------------------------------------------------------
# Provider-specific tools
# ---------------------------------------------------------------------------


def test_native_google_search_tool() -> None:
    cfg, _ = _build(provider_options={"tools": [{"google_search": {}}]})
    assert cfg["tools"] == [{"google_search": {}}]


def test_native_tools_combine_with_neutral_tools() -> None:
    """Neutral ToolSpec functions and provider-native tool blocks
    (google_search, code_execution) coexist in the same `tools` list —
    function declarations first, then native blocks."""
    cfg, _ = _build(
        tools=[ToolSpec(name="lookup", description="d", parameters={"type": "object"})],
        provider_options={"tools": [{"code_execution": {}}]},
    )
    blocks = cfg["tools"]
    assert blocks[0] == {
        "function_declarations": [
            {"name": "lookup", "description": "d", "parameters": {"type": "object"}}
        ]
    }
    assert blocks[1] == {"code_execution": {}}


# ---------------------------------------------------------------------------
# tool_choice mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("choice,mode", [
    ("auto", "AUTO"),
    ("required", "ANY"),
    ("none", "NONE"),
])
def test_tool_choice_mode(choice, mode) -> None:
    cfg, _ = _build(tool_choice=choice)
    assert cfg["tool_config"]["function_calling_config"]["mode"] == mode


def test_tool_choice_dict_passthrough() -> None:
    custom = {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["x"]}}
    cfg, _ = _build(tool_choice=custom)
    assert cfg["tool_config"] == custom


# ---------------------------------------------------------------------------
# Default alias map (Gemini 3)
# ---------------------------------------------------------------------------


def _resolve_concrete(svc, alias):
    binding = svc._presets[alias]
    return binding.provider, binding.concrete_model


def test_default_aliases_include_gemini_3() -> None:
    class _StubSettings:
        pass

    svc = LlmService(_StubSettings())  # type: ignore[arg-type]

    # Preset names use `-` (the DB CHECK constraint disallows `.`);
    # `concrete_model` keeps the dotted form upstream providers
    # expect on the wire (upstream-bug #2).
    assert _resolve_concrete(svc, "default") == ("gemini", "gemini-3.5-flash")
    assert _resolve_concrete(svc, "gemini-2-5-pro") == ("gemini", "gemini-2.5-pro")
    assert _resolve_concrete(svc, "gemini-3-5-flash") == ("gemini", "gemini-3.5-flash")
    assert _resolve_concrete(svc, "gemini-3-1-flash-lite") == (
        "gemini", "gemini-3.1-flash-lite"
    )
    assert _resolve_concrete(svc, "gemini-3-1-pro") == ("gemini", "gemini-3.1-pro-preview")
    assert _resolve_concrete(svc, "gemini-embedding-2") == ("gemini", "gemini-embedding-2")
