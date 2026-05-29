"""Tests for the LLM preset surface — resolution, tier gate, override
semantics, default-seed bootstrap.

Pure unit tests against the in-memory `LlmService` map and the helper
functions in `bp_router.llm.presets`. No DB; the
`load_presets_from_db` path is exercised separately when DB tests run.
"""

from __future__ import annotations

import asyncio

import pytest

from bp_router.llm.presets import (
    Preset,
    PresetNotAllowedError,
    PresetUnknownError,
    default_presets,
    is_valid_min_user_level,
    is_valid_preset_name,
    is_valid_provider,
    resolve_call_params,
    user_level_satisfies,
)

# Helpers extracted to conftest.py so the four LLM test files share a
# single source of truth for service/preset stubs and cache-key
# construction. Kept as thin aliases so existing call sites keep
# working without churn.
from tests.conftest import make_llm_service as _service  # noqa: E402

# ---------------------------------------------------------------------------
# Tier gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("required,actual,expected", [
    # Wildcard admits anyone, including missing levels.
    ("*", "admin", True),
    ("*", "service", True),
    ("*", "tier3", True),
    ("*", None, True),
    # admin / service are exact matches.
    ("admin", "admin", True),
    ("admin", "service", False),
    ("admin", "tier0", False),
    ("service", "service", True),
    ("service", "admin", False),
    # tierN is "this tier or stricter" (lower number).
    ("tier1", "admin", True),    # admin satisfies any tier rule
    ("tier1", "service", True),  # service satisfies any tier rule
    ("tier1", "tier0", True),    # stricter
    ("tier1", "tier1", True),    # exact
    ("tier1", "tier2", False),   # less privileged
    ("tier1", "tier3", False),
    # Missing actual level fails any non-* gate.
    ("admin", None, False),
    ("tier0", None, False),
    # Bogus required level = closed by default.
    ("garbage", "admin", False),
])
def test_user_level_satisfies(required, actual, expected) -> None:
    assert user_level_satisfies(actual, required) is expected


# ---------------------------------------------------------------------------
# Override semantics
# ---------------------------------------------------------------------------


def _preset(**kwargs) -> Preset:
    """Test preset with sensible defaults for the fields callers don't set."""
    return Preset(
        name=kwargs.get("name", "test"),
        provider=kwargs.get("provider", "gemini"),
        concrete_model=kwargs.get("concrete_model", "gemini-2.5-flash"),
        api_key_ref=kwargs.get("api_key_ref", "env://X"),
        min_user_level=kwargs.get("min_user_level", "*"),
        default_temperature=kwargs.get("default_temperature"),
        default_max_tokens=kwargs.get("default_max_tokens"),
        default_provider_options=kwargs.get("default_provider_options", {}),
    )


def test_resolve_call_params_uses_preset_defaults_when_unset() -> None:
    p = _preset(default_temperature=0.7, default_max_tokens=1024)
    out = resolve_call_params(
        p, temperature=None, max_tokens=None, provider_options=None,
    )
    assert out.temperature == 0.7
    assert out.max_tokens == 1024


def test_resolve_call_params_call_time_overrides_preset() -> None:
    p = _preset(default_temperature=0.7, default_max_tokens=1024)
    out = resolve_call_params(
        p, temperature=0.2, max_tokens=512, provider_options=None,
    )
    assert out.temperature == 0.2
    assert out.max_tokens == 512


def test_resolve_call_params_zero_max_tokens_treated_as_set() -> None:
    """`temperature=0.0` and `max_tokens=0` are valid call-time
    values that should override the preset, not fall back to the
    default. We use `is not None` for that reason."""
    p = _preset(default_temperature=0.7, default_max_tokens=1024)
    out = resolve_call_params(
        p, temperature=0.0, max_tokens=0, provider_options=None,
    )
    assert out.temperature == 0.0
    assert out.max_tokens == 0


def test_resolve_call_params_provider_options_replace_not_merge() -> None:
    """Per the design call: call-time `provider_options` REPLACES the
    preset's default dict entirely, not merged. If the agent wants
    to keep some preset defaults, they spread them themselves at
    call time."""
    p = _preset(default_provider_options={
        "thinking": {"type": "adaptive"},
        "max_output_tokens": 2048,
    })
    # Agent passes a single key; preset's defaults DON'T leak through.
    out = resolve_call_params(
        p, temperature=None, max_tokens=None,
        provider_options={"reasoning": {"effort": "low"}},
    )
    assert out.provider_options == {"reasoning": {"effort": "low"}}


def test_resolve_call_params_provider_options_inherits_when_unset() -> None:
    p = _preset(default_provider_options={
        "thinking": {"type": "adaptive"},
    })
    out = resolve_call_params(
        p, temperature=None, max_tokens=None, provider_options=None,
    )
    assert out.provider_options == {"thinking": {"type": "adaptive"}}


def test_resolve_call_params_provider_options_empty_default_yields_none() -> None:
    """If the preset has no default options, omitting at call time
    yields None — adapters know None means "no provider_options"."""
    p = _preset()  # default_provider_options={} is the default
    out = resolve_call_params(
        p, temperature=None, max_tokens=None, provider_options=None,
    )
    assert out.provider_options is None


def test_resolve_call_params_provider_metadata_passes_through() -> None:
    p = _preset(provider="openai", concrete_model="gpt-5.5",
                api_key_ref="env://OPENAI_API_KEY")
    out = resolve_call_params(p, temperature=None, max_tokens=None,
                              provider_options=None)
    assert out.provider == "openai"
    assert out.concrete_model == "gpt-5.5"
    assert out.api_key_ref == "env://OPENAI_API_KEY"


# ---------------------------------------------------------------------------
# Service: preset registry + tier gate
# ---------------------------------------------------------------------------


def test_service_seeds_default_presets() -> None:
    """Service constructor pre-populates the in-memory map with the
    built-in defaults so test harnesses that skip the DB seed step
    still resolve the legacy alias names."""
    svc = _service()
    names = [p.name for p in svc.list_presets()]
    assert "default" in names
    # Preset names with `-` replacing `.` per upstream-bug #2.
    assert "gpt-5-5" in names
    assert "claude-opus-4-8" in names
    assert "text-embedding-3-small" in names


def test_service_get_preset_returns_none_for_unknown() -> None:
    svc = _service()
    assert svc.get_preset("nonexistent") is None


def test_service_register_preset_overrides_default() -> None:
    """register_preset is a test-only convenience that mutates the
    in-memory map without touching the DB."""
    svc = _service()
    svc._register_preset_for_test(_preset(
        name="default",
        provider="anthropic",
        concrete_model="claude-haiku-4-5",
        api_key_ref="env://ANTHROPIC_API_KEY",
    ))
    p = svc.get_preset("default")
    assert p.provider == "anthropic"
    assert p.concrete_model == "claude-haiku-4-5"


def test_service_resolve_unknown_preset_raises() -> None:
    """`_resolve` is internal but the error type must propagate
    through `generate` / `embed` / `count_tokens` so dispatch can
    map to a `preset_unknown` LlmResultFrame error."""
    svc = _service()
    with pytest.raises(PresetUnknownError):
        svc._resolve(
            preset_name="nonexistent",
            user_level="admin",
            temperature=None,
            max_tokens=None,
            provider_options=None,
        )


def test_service_resolve_tier_gate_blocks_low_level() -> None:
    """Tier gate uses the same semantics as ACL rules. A preset
    requiring tier0 rejects tier1+."""
    svc = _service()
    svc._register_preset_for_test(_preset(name="restricted", min_user_level="tier0"))
    with pytest.raises(PresetNotAllowedError) as exc_info:
        svc._resolve(
            preset_name="restricted",
            user_level="tier1",  # less privileged than tier0
            temperature=None,
            max_tokens=None,
            provider_options=None,
        )
    assert exc_info.value.preset_name == "restricted"
    assert exc_info.value.user_level == "tier1"
    assert exc_info.value.required == "tier0"


# Cache-stub helpers live in conftest. `_StubAdapter` here is
# preserved as an alias for tests that may still reference the type
# (e.g. for isinstance checks or attribute introspection).
from tests.conftest import StubAdapter as _StubAdapter  # noqa: E402, F401
from tests.conftest import wire_stub_adapter as _service_with_stub_adapter  # noqa: E402


def test_service_resolve_tier_gate_admits_admin() -> None:
    """admin satisfies any tierN rule (admin/service always pass)."""
    svc = _service()
    p = _preset(name="restricted", min_user_level="tier1")
    svc._register_preset_for_test(p)
    _service_with_stub_adapter(svc, p)
    # Doesn't raise — admin satisfies tier1.
    resolved, _, _ = svc._resolve(
        preset_name="restricted",
        user_level="admin",
        temperature=None,
        max_tokens=None,
        provider_options=None,
    )
    assert resolved.provider == "gemini"


def test_service_resolve_wildcard_admits_anyone() -> None:
    """min_user_level='*' is the default and admits any level
    including missing user_level (e.g., embedded pre-auth contexts)."""
    svc = _service()
    p = _preset(name="open", min_user_level="*")
    svc._register_preset_for_test(p)
    _service_with_stub_adapter(svc, p)
    # No user_level supplied at all.
    resolved, _, _ = svc._resolve(
        preset_name="open", user_level=None,
        temperature=None, max_tokens=None, provider_options=None,
    )
    assert resolved.concrete_model == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# User-level cache (tier gate plumbing)
# ---------------------------------------------------------------------------


def test_user_level_cache_hit_returns_cached_value() -> None:
    """When the cache is warm, resolve_user_level doesn't hit the DB
    even if `conn=None` is passed — cache wins."""
    svc = _service()

    # Warm the cache directly.
    import time as _time

    from bp_router.llm.service import _UserLevelCacheEntry
    svc._user_level_cache["u1"] = _UserLevelCacheEntry(
        level="tier0",
        expires_at=_time.monotonic() + 60.0,
    )
    out = asyncio.run(svc.resolve_user_level(None, "u1"))
    assert out == "tier0"


def test_user_level_cache_invalidate() -> None:
    """invalidate_user_level drops the cached entry so the next
    lookup re-fetches from the DB."""
    svc = _service()
    import time as _time

    from bp_router.llm.service import _UserLevelCacheEntry
    svc._user_level_cache["u1"] = _UserLevelCacheEntry(
        level="tier0",
        expires_at=_time.monotonic() + 60.0,
    )
    svc.invalidate_user_level("u1")
    # Cache empty + conn=None → returns None (no DB to fall back on).
    out = asyncio.run(svc.resolve_user_level(None, "u1"))
    assert out is None


def test_user_level_cache_no_user_id_returns_none() -> None:
    svc = _service()
    out = asyncio.run(svc.resolve_user_level(None, None))
    assert out is None


# ---------------------------------------------------------------------------
# Default presets cover all three providers + embeddings
# ---------------------------------------------------------------------------


def test_default_presets_cover_known_aliases() -> None:
    """The built-in seed must carry the canonical aliases callers rely on.
    Preset names use `-` instead of `.` (the `llm_presets.name` CHECK regex
    `^[a-z][a-z0-9_-]{0,63}$` disallows `.`); `concrete_model` keeps the
    dotted upstream form (upstream-bug #2)."""
    names = {p.name for p in default_presets()}
    expected = {
        # Gemini
        "default", "default_embedding", "gemini", "gemini-2-5-pro",
        "gemini-3-5-flash", "gemini-3-1-flash-lite", "gemini-3-1-pro",
        "gemini-lite", "gemini-pro", "gemini-embedding-2",
        # Generic tier slots
        "lite", "pro",
        # Anthropic
        "claude", "claude-opus", "claude-opus-4-8",
        "claude-sonnet", "claude-sonnet-4-6",
        "claude-haiku", "claude-haiku-4-5",
        # OpenAI
        "openai", "gpt", "gpt-5-5", "gpt-5-5-pro",
        "gpt-5-4", "gpt-5-4-mini", "gpt-5-4-nano", "gpt-5", "gpt-5-mini",
        "gpt-5-nano", "gpt-4-1", "gpt-nano", "gpt-pro",
        # Embeddings
        "text-embedding-3-small", "text-embedding-3-large",
    }
    missing = expected - names
    assert not missing, f"missing default presets: {missing}"


def test_default_presets_min_user_level_is_wildcard() -> None:
    """Defaults preserve the old no-gate behaviour. Operators can
    tighten via the admin UI."""
    for p in default_presets():
        assert p.min_user_level == "*", (
            f"preset {p.name!r} default min_user_level should be '*' "
            f"for back-compat (got {p.min_user_level!r})"
        )


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,valid", [
    ("default", True),
    ("gpt-5.5", False),         # period not allowed in slug
    ("gpt-5", True),
    ("a", True),
    ("a-b_c", True),
    ("Default", False),         # uppercase
    ("0name", False),           # starts with digit
    ("", False),                # empty
    ("a" * 64, True),           # 64 chars max
    ("a" * 65, False),
    ("name with space", False),
])
def test_is_valid_preset_name(name, valid) -> None:
    # gpt-5.5 has a dot; the actual default presets have dots in their
    # names, but validators reject dots — that's deliberate. The dotted
    # default presets predate this validator and live in the seed list,
    # not user-defined names. New presets created via the admin UI must
    # satisfy the slug grammar.
    if name == "gpt-5.5":
        assert not is_valid_preset_name(name)
    else:
        assert is_valid_preset_name(name) is valid


@pytest.mark.parametrize("level,valid", [
    ("*", True),
    ("admin", True),
    ("service", True),
    ("tier0", True),
    ("tier99", True),
    ("tier", False),
    ("tier-1", False),
    ("anyone", False),
    ("", False),
])
def test_is_valid_min_user_level(level, valid) -> None:
    assert is_valid_min_user_level(level) is valid


@pytest.mark.parametrize("provider,valid", [
    ("gemini", True),
    ("anthropic", True),
    ("openai", True),
    ("openai-embeddings", True),
    ("voyage", False),
    ("", False),
])
def test_is_valid_provider(provider, valid) -> None:
    assert is_valid_provider(provider) is valid
