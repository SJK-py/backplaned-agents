"""Tests for the admin UI's preset form translator + the
form-to-payload normalisation. The page handlers themselves can't
run in this sandbox (FastAPI / starlette aren't installed), but the
pure helpers are unit-testable.
"""

from __future__ import annotations

import json

import pytest

# bp_admin imports FastAPI / Starlette transitively; skip the whole
# file when those aren't available (CI sandboxes that don't ship them).
fastapi = pytest.importorskip("fastapi")  # noqa: F841

from bp_admin.pages.llm_presets import (  # noqa: E402
    _form_for_create,
    _form_from_preset,
    _form_to_payload,
)


def test_form_to_payload_minimal_required_fields() -> None:
    payload, errors = _form_to_payload(
        description="",
        provider="gemini",
        concrete_model="gemini-2.5-flash",
        api_key_ref="env://GEMINI_API_KEY",
        min_user_level="*",
        default_temperature="",
        default_max_tokens="",
        default_provider_options="",
    )
    assert errors == []
    # Required-fields payload always includes the unconditional
    # columns the form drives (fallback_preset / max_retries / base_url
    # — empty strings / 0 by default), so the admin API can null /
    # default them on the next PATCH without ambiguity.
    assert payload == {
        "provider": "gemini",
        "concrete_model": "gemini-2.5-flash",
        "api_key_ref": "env://GEMINI_API_KEY",
        "min_user_level": "*",
        "fallback_preset": "",
        "max_retries": 0,
        "base_url": "",
    }


def test_form_to_payload_with_all_optional_fields() -> None:
    payload, errors = _form_to_payload(
        description="Quick chat",
        provider="anthropic",
        concrete_model="claude-haiku-4-5",
        api_key_ref="env://ANTHROPIC_API_KEY",
        min_user_level="tier2",
        default_temperature="0.7",
        default_max_tokens="1024",
        default_provider_options='{"thinking": {"type": "adaptive"}}',
    )
    assert errors == []
    assert payload["description"] == "Quick chat"
    assert payload["default_temperature"] == 0.7
    assert payload["default_max_tokens"] == 1024
    assert payload["default_provider_options"] == {
        "thinking": {"type": "adaptive"}
    }


def test_form_to_payload_invalid_temperature_collected() -> None:
    payload, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="2.5",   # out of [0, 2]
        default_max_tokens="",
        default_provider_options="",
    )
    # Bounds are rendered via `:g` so float-valued constants
    # (0.0, 2.0) print as the cleaner "0" and "2" forms.
    assert errors == ["Temperature must be between 0 and 2."]
    # Field with the bad value isn't included in payload.
    assert "default_temperature" not in payload


def test_form_to_payload_temperature_message_uses_g_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if a future operator changes the bounds to a
    fractional value (say 0.1, 1.9), the message must still
    render naturally — `:g` keeps fractions while stripping
    trailing `.0`. Pre-R5 drift: the constants were promoted to
    floats but the f-string kept the integer format, producing
    'between 0.0 and 2.0'."""
    from bp_router.llm import presets

    monkeypatch.setattr(presets, "TEMPERATURE_MIN", 0.1)
    monkeypatch.setattr(presets, "TEMPERATURE_MAX", 1.9)

    # Force the bp_admin module to re-import the constants.
    # Inspecting the rendered message is enough — the helper
    # imports lazily.
    _, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="9.9",
        default_max_tokens="",
        default_provider_options="",
    )
    # Fractional bounds render naturally (no awkward trailing zeros).
    assert errors == ["Temperature must be between 0.1 and 1.9."]


def test_form_to_payload_non_numeric_temperature() -> None:
    _, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="warm",
        default_max_tokens="",
        default_provider_options="",
    )
    assert errors == ["Temperature must be a number."]


def test_form_to_payload_invalid_max_tokens() -> None:
    _, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="",
        default_max_tokens="0",  # must be positive
        default_provider_options="",
    )
    assert errors == ["Max tokens must be a positive integer."]


def test_form_to_payload_provider_options_must_be_object() -> None:
    """Bare arrays / scalars rejected — the API expects a JSON object."""
    _, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="",
        default_max_tokens="",
        default_provider_options="[1, 2, 3]",
    )
    assert errors == ["Default provider_options must be a JSON object."]


def test_form_to_payload_provider_options_malformed_json() -> None:
    payload, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="",
        default_max_tokens="",
        default_provider_options='{"unclosed: 1',
    )
    assert errors and "not valid JSON" in errors[0]
    assert "default_provider_options" not in payload


def test_form_to_payload_collects_multiple_errors() -> None:
    """Form re-render shows ALL validation errors at once, not just
    the first."""
    _, errors = _form_to_payload(
        description="", provider="gemini",
        concrete_model="g", api_key_ref="env://X",
        min_user_level="*",
        default_temperature="9",      # out of range
        default_max_tokens="-1",      # not positive
        default_provider_options="x", # malformed
    )
    assert len(errors) == 3


def test_form_for_create_initial_values() -> None:
    form = _form_for_create()
    # All keys present so the template doesn't raise on missing fields.
    assert set(form.keys()) == {
        "name", "description", "provider", "concrete_model", "api_key_ref",
        "api_key", "base_url",
        "min_user_level", "default_temperature", "default_max_tokens",
        "default_provider_options",
        "fallback_preset", "max_retries",
    }
    assert form["min_user_level"] == "*"
    assert form["provider"] == "gemini"
    # Local-server fields default empty / "0" — only revealed when the
    # admin picks an openai-compatible* provider in the dropdown.
    assert form["base_url"] == ""
    assert form["fallback_preset"] == ""
    assert form["max_retries"] == "0"
    assert form["api_key"] == ""


def test_form_to_payload_local_server_with_base_url() -> None:
    """openai-compatible presets carry the base_url + a blank
    api_key_ref. The admin API validates the cross-field rule, but
    the form helper happily emits the payload."""
    payload, errors = _form_to_payload(
        description="local vLLM",
        provider="openai-compatible",
        concrete_model="qwen2.5-32b",
        api_key_ref="",
        api_key="",
        base_url="http://vllm:8000/v1",
        min_user_level="*",
        default_temperature="",
        default_max_tokens="",
        default_provider_options="",
    )
    assert errors == []
    assert payload["provider"] == "openai-compatible"
    assert payload["base_url"] == "http://vllm:8000/v1"
    assert payload["api_key_ref"] == ""
    # Inline secret omitted when blank.
    assert "api_key" not in payload


def test_form_to_payload_strips_base_url_whitespace() -> None:
    payload, _ = _form_to_payload(
        description="", provider="openai-compatible",
        concrete_model="m", api_key_ref="",
        base_url="   http://x:8000/v1   ",
        min_user_level="*",
        default_temperature="", default_max_tokens="",
        default_provider_options="",
    )
    assert payload["base_url"] == "http://x:8000/v1"


def test_form_from_preset_includes_base_url_for_local_server() -> None:
    form = _form_from_preset({
        "name": "local",
        "description": None,
        "provider": "openai-compatible",
        "concrete_model": "qwen2.5",
        "api_key_ref": "",
        "base_url": "http://vllm:8000/v1",
        "min_user_level": "*",
        "default_temperature": None,
        "default_max_tokens": None,
        "default_provider_options": None,
        "fallback_preset": None,
        "max_retries": 0,
    })
    assert form["base_url"] == "http://vllm:8000/v1"
    # Inline api_key always blank on edit ("blank = leave unchanged").
    assert form["api_key"] == ""


def test_form_from_preset_serializes_provider_options_indented() -> None:
    """The edit form's textarea for provider_options shows pretty-
    printed JSON so admins can edit without fighting the formatter."""
    form = _form_from_preset({
        "name": "test",
        "description": "Test",
        "provider": "anthropic",
        "concrete_model": "claude-haiku-4-5",
        "api_key_ref": "env://X",
        "min_user_level": "tier1",
        "default_temperature": 0.5,
        "default_max_tokens": 1024,
        "default_provider_options": {"thinking": {"type": "adaptive"}},
    })
    # Round-trips through JSON.
    assert json.loads(form["default_provider_options"]) == {
        "thinking": {"type": "adaptive"}
    }
    # Pretty-printed (multi-line).
    assert "\n" in form["default_provider_options"]
    assert form["default_temperature"] == "0.5"
    assert form["default_max_tokens"] == "1024"
    assert form["min_user_level"] == "tier1"


def test_form_from_preset_handles_none_optionals() -> None:
    form = _form_from_preset({
        "name": "open",
        "description": None,
        "provider": "gemini",
        "concrete_model": "gemini-2.5-flash",
        "api_key_ref": "env://X",
        "min_user_level": "*",
        "default_temperature": None,
        "default_max_tokens": None,
        "default_provider_options": None,
    })
    # Nones become empty strings (textarea/input-friendly).
    assert form["description"] == ""
    assert form["default_temperature"] == ""
    assert form["default_max_tokens"] == ""
    assert form["default_provider_options"] == ""
