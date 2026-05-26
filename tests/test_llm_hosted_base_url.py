"""Tests for the optional `base_url` argument on hosted-provider
adapters (Gemini, Anthropic, OpenAI, OpenAI-embeddings).

The real SDKs aren't installed in CI; we inject fake modules into
`sys.modules` and capture the kwargs the adapter passes to the SDK
constructor. That's enough to verify:

  - When `base_url` is None, the adapter does NOT pass it to the SDK
    (so the SDK's default endpoint is used unchanged).
  - When `base_url` is set, the adapter passes it through correctly,
    including the Gemini-specific `http_options=HttpOptions(base_url=...)`
    indirection.

Plus a couple of `LlmService` integration checks: the resolver passes
`resolved.base_url` into `_build_adapter` and the cache key segments
by URL so multi-region setups stay isolated.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake-SDK helpers
# ---------------------------------------------------------------------------


class _Capture:
    """Records constructor kwargs so the test can assert on them."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    """Inject a fake `openai` module with a kwargs-capturing
    `AsyncOpenAI` symbol."""
    capture = _Capture()

    class _AsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            capture.kwargs = kwargs

    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _AsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake)
    return capture


def _install_fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    capture = _Capture()

    class _AsyncAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            capture.kwargs = kwargs

    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = _AsyncAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return capture


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    """The google-genai SDK is layered: `from google import genai` for
    the package, plus `from google.genai import types` for the
    HttpOptions dataclass we pass `base_url` through."""
    capture = _Capture()

    class _HttpOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __repr__(self) -> str:
            return f"HttpOptions({self.kwargs})"

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            capture.kwargs = kwargs

    types_mod = types.ModuleType("google.genai.types")
    types_mod.HttpOptions = _HttpOptions  # type: ignore[attr-defined]

    genai_mod = types.ModuleType("google.genai")
    genai_mod.Client = _Client  # type: ignore[attr-defined]
    genai_mod.types = types_mod  # type: ignore[attr-defined]

    google_mod = types.ModuleType("google")
    google_mod.genai = genai_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    return capture


# ---------------------------------------------------------------------------
# OpenAI / OpenAIEmbeddings
# ---------------------------------------------------------------------------


def test_openai_adapter_passes_base_url_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _install_fake_openai(monkeypatch)
    from bp_router.llm.providers.openai import OpenAIAdapter

    adapter = OpenAIAdapter(
        concrete_model="gpt-5.5",
        api_key="sk-x",
        base_url="https://my-azure-proxy.example.com/v1",
    )
    adapter._get_client()
    assert cap.kwargs == {
        "api_key": "sk-x",
        "base_url": "https://my-azure-proxy.example.com/v1",
    }


def test_openai_adapter_omits_base_url_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No base_url → the SDK's default endpoint is used unchanged."""
    cap = _install_fake_openai(monkeypatch)
    from bp_router.llm.providers.openai import OpenAIAdapter

    adapter = OpenAIAdapter(concrete_model="gpt-5.5", api_key="sk-x")
    adapter._get_client()
    assert cap.kwargs == {"api_key": "sk-x"}
    assert "base_url" not in cap.kwargs


def test_openai_adapter_omits_base_url_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string is treated as 'unset' so a stray '' from form
    handling doesn't change the upstream endpoint."""
    cap = _install_fake_openai(monkeypatch)
    from bp_router.llm.providers.openai import OpenAIAdapter

    adapter = OpenAIAdapter(
        concrete_model="gpt-5.5", api_key="sk-x", base_url=""
    )
    adapter._get_client()
    assert "base_url" not in cap.kwargs


def test_openai_embeddings_adapter_passes_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _install_fake_openai(monkeypatch)
    from bp_router.llm.providers.openai import OpenAIEmbeddingsAdapter

    adapter = OpenAIEmbeddingsAdapter(
        concrete_model="text-embedding-3-small",
        api_key="sk-x",
        base_url="https://gateway.example.com/v1",
    )
    adapter._get_client()
    assert cap.kwargs["base_url"] == "https://gateway.example.com/v1"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_adapter_passes_base_url_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _install_fake_anthropic(monkeypatch)
    from bp_router.llm.providers.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(
        concrete_model="claude-haiku-4-5",
        api_key="sk-ant-x",
        base_url="https://bedrock-proxy.example.com",
    )
    adapter._get_client()
    assert cap.kwargs == {
        "api_key": "sk-ant-x",
        "base_url": "https://bedrock-proxy.example.com",
    }


def test_anthropic_adapter_omits_base_url_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _install_fake_anthropic(monkeypatch)
    from bp_router.llm.providers.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(
        concrete_model="claude-haiku-4-5", api_key="sk-ant-x"
    )
    adapter._get_client()
    assert "base_url" not in cap.kwargs


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_adapter_passes_base_url_via_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The google-genai SDK takes endpoint overrides through
    `http_options=HttpOptions(base_url=...)`. The adapter wraps the
    string in an HttpOptions object before passing to `genai.Client`."""
    cap = _install_fake_genai(monkeypatch)
    from bp_router.llm.providers.gemini import GeminiAdapter

    adapter = GeminiAdapter(
        concrete_model="gemini-2.5-flash",
        api_key="g-key",
        base_url="https://eu-gemini.example.com",
    )
    adapter._get_client()
    assert cap.kwargs["api_key"] == "g-key"
    http_opts = cap.kwargs["http_options"]
    # Our fake HttpOptions captures kwargs into `.kwargs`.
    assert http_opts.kwargs == {"base_url": "https://eu-gemini.example.com"}


def test_gemini_adapter_omits_http_options_when_no_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a custom base_url we don't import or instantiate
    HttpOptions — the SDK uses its own defaults."""
    cap = _install_fake_genai(monkeypatch)
    from bp_router.llm.providers.gemini import GeminiAdapter

    adapter = GeminiAdapter(concrete_model="gemini-2.5-flash", api_key="g-key")
    adapter._get_client()
    assert "http_options" not in cap.kwargs


# ---------------------------------------------------------------------------
# LlmService integration
# ---------------------------------------------------------------------------


def test_resolved_call_params_carries_base_url() -> None:
    from bp_router.llm.presets import Preset, resolve_call_params

    p = Preset(
        name="azure-gpt",
        provider="openai",
        concrete_model="gpt-5.5",
        api_key_ref="env://AZURE_OPENAI_KEY",
        base_url="https://my-azure.example.com/v1",
    )
    resolved = resolve_call_params(
        p, temperature=None, max_tokens=None, provider_options=None
    )
    assert resolved.base_url == "https://my-azure.example.com/v1"


def test_hosted_provider_with_base_url_distinct_cache_slot() -> None:
    """Two `openai` presets — one default, one Azure-proxied — must
    end up in distinct adapter cache slots so they hit the right
    endpoint."""
    from bp_router.llm.presets import Preset
    from bp_router.llm.service import LlmService

    class _Settings:
        pass

    svc = LlmService(_Settings())  # type: ignore[arg-type]
    direct = Preset(
        name="openai-direct",
        provider="openai",
        concrete_model="gpt-5.5",
        api_key_ref="env://OPENAI_API_KEY",
    )
    azure = Preset(
        name="openai-azure",
        provider="openai",
        concrete_model="gpt-5.5",
        api_key_ref="env://AZURE_OPENAI_KEY",
        base_url="https://my-azure.example.com/v1",
    )
    svc._register_preset_for_test(direct)
    svc._register_preset_for_test(azure)

    sentinel_direct, sentinel_azure = object(), object()
    svc._adapters[
        f"openai::gpt-5.5::-::{direct.api_key_ref}"
    ] = sentinel_direct  # type: ignore[assignment]
    svc._adapters[
        f"openai::gpt-5.5::{azure.base_url}::{azure.api_key_ref}"
    ] = sentinel_azure  # type: ignore[assignment]

    _, got_direct, _ = svc._resolve_one(
        preset=direct, temperature=None, max_tokens=None, provider_options=None,
    )
    _, got_azure, _ = svc._resolve_one(
        preset=azure, temperature=None, max_tokens=None, provider_options=None,
    )
    assert got_direct is sentinel_direct
    assert got_azure is sentinel_azure
    assert got_direct is not got_azure


def test_default_presets_carry_no_base_url() -> None:
    """Built-in defaults stay backward compatible — none of them
    pre-populate a base_url, so deployments using the SDK defaults
    keep working unchanged after the migration."""
    from bp_router.llm.presets import default_presets

    for p in default_presets():
        assert p.base_url is None, f"{p.name} unexpectedly carries base_url"
