"""Tests for the code-quality bundle (review M15, M16, L1, L2, L3, L4).

Each refactor is verified by either source-reading (when the runtime
needs FastAPI / openai) or direct invocation (when the helper is
runnable standalone).
"""

from __future__ import annotations

import inspect
import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# M15 — make_async_openai factory + four adapters consume it
# ---------------------------------------------------------------------------


def test_make_async_openai_passes_api_key_only_when_no_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`base_url=None` must NOT be forwarded — the SDK uses its
    default endpoint when the kwarg is absent."""
    captured: dict[str, Any] = {}

    class _AsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _AsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake)

    from bp_router.llm.providers._openai_client import make_async_openai

    make_async_openai(api_key="sk-x")
    assert captured == {"api_key": "sk-x"}


def test_make_async_openai_passes_base_url_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _AsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _AsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake)

    from bp_router.llm.providers._openai_client import make_async_openai

    make_async_openai(api_key="sk-x", base_url="https://proxy/v1")
    assert captured == {"api_key": "sk-x", "base_url": "https://proxy/v1"}


def test_make_async_openai_raises_clean_error_on_missing_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four adapters used to duplicate this exact error string."""
    monkeypatch.setitem(sys.modules, "openai", None)  # forces ImportError

    from bp_router.llm.providers._openai_client import make_async_openai

    with pytest.raises(RuntimeError, match="openai not installed"):
        make_async_openai(api_key="sk-x")


def test_all_four_openai_adapters_use_make_async_openai() -> None:
    """Source check: no adapter reproduces the 4-line lazy-import +
    AsyncOpenAI(...) construction. Catches a regression where someone
    inlines a fifth copy."""
    import bp_router.llm.providers.openai as openai_mod
    import bp_router.llm.providers.openai_compatible as oac_mod

    for mod in (openai_mod, oac_mod):
        src = inspect.getsource(mod)
        # The factory call must appear; no inline `AsyncOpenAI(**kwargs)`
        # should remain (only the import inside the factory does).
        assert "make_async_openai" in src
        assert "AsyncOpenAI(**kwargs)" not in src


# ---------------------------------------------------------------------------
# M16 — preset bounds live in `bp_router.llm.presets`
# ---------------------------------------------------------------------------


def test_preset_bounds_constants_exposed() -> None:
    from bp_router.llm import presets

    assert presets.TEMPERATURE_MIN == 0.0
    assert presets.TEMPERATURE_MAX == 2.0
    assert presets.MAX_TOKENS_MIN == 1
    assert presets.MAX_RETRIES_MIN == 0
    assert presets.MAX_RETRIES_MAX == 10


@pytest.mark.parametrize("v,ok", [
    (0.0, True), (1.5, True), (2.0, True),
    (-0.1, False), (2.5, False),
])
def test_temperature_in_range(v: float, ok: bool) -> None:
    from bp_router.llm.presets import temperature_in_range

    assert temperature_in_range(v) is ok


@pytest.mark.parametrize("v,ok", [
    (0, False), (1, True), (-1, False), (1024, True), (10**9, True),
])
def test_max_tokens_in_range(v: int, ok: bool) -> None:
    from bp_router.llm.presets import max_tokens_in_range

    assert max_tokens_in_range(v) is ok


@pytest.mark.parametrize("v,ok", [
    (0, True), (5, True), (10, True),
    (-1, False), (11, False),
])
def test_max_retries_in_range(v: int, ok: bool) -> None:
    from bp_router.llm.presets import max_retries_in_range

    assert max_retries_in_range(v) is ok


def test_admin_api_uses_centralised_bounds() -> None:
    """`_validate_preset_payload` calls the helpers from presets.py
    rather than carrying its own `0 <= temp <= 2` literal."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from bp_router.api import admin

    src = inspect.getsource(admin._validate_preset_payload)
    # No inline literal range checks for the centralised fields.
    assert "0 <= temp <= 2" not in src
    assert "max_t <= 0" not in src
    assert "retries < 0 or retries > 10" not in src
    # The helpers are referenced.
    assert "temperature_in_range" in src
    assert "max_tokens_in_range" in src
    assert "max_retries_in_range" in src


def test_admin_form_helper_uses_centralised_bounds() -> None:
    """Same single-source-of-truth check on the admin webUI side."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from bp_admin.pages import llm_presets as admin_ui

    src = inspect.getsource(admin_ui._form_to_payload)
    assert "0 <= t <= 2" not in src
    assert "n <= 0" not in src
    assert "r < 0 or r > 10" not in src
    assert "temperature_in_range" in src
    assert "max_tokens_in_range" in src
    assert "max_retries_in_range" in src


# ---------------------------------------------------------------------------
# L1 — _row_to_dict deleted
# ---------------------------------------------------------------------------


def test_row_to_dict_no_longer_defined() -> None:
    from bp_router.db import queries

    assert not hasattr(queries, "_row_to_dict")


# ---------------------------------------------------------------------------
# L2 — delete_llm_preset uses ` 1` not `1`
# ---------------------------------------------------------------------------


def test_delete_llm_preset_uses_consistent_rowcount_match() -> None:
    """Cosmetic but real: `endswith("1")` would match `DELETE 11`,
    `DELETE 21`, etc. The PK uniqueness guarantees 0 or 1 rows so
    this can't actually misfire today, but consistency-as-discipline."""
    from bp_router.db import queries

    src = inspect.getsource(queries.delete_llm_preset)
    assert 'endswith(" 1")' in src
    assert 'endswith("1")' not in src or 'endswith(" 1")' in src


# ---------------------------------------------------------------------------
# L3 — register_preset renamed to _register_preset_for_test
# ---------------------------------------------------------------------------


def test_register_preset_for_test_is_underscored() -> None:
    """The public `register_preset` is renamed to make its test-only
    intent explicit in code search, IDE autocomplete, etc."""
    from bp_router.llm.service import LlmService

    assert hasattr(LlmService, "_register_preset_for_test")
    # The old public name is gone.
    assert not hasattr(LlmService, "register_preset")


def test_no_call_sites_use_old_register_preset_name() -> None:
    """Catch a regression where someone re-introduces `register_preset`
    as a shim or alias."""
    import bp_router.llm.service as svc

    src = inspect.getsource(svc)
    # The class only defines the underscored form.
    assert "def register_preset(" not in src
    assert "def _register_preset_for_test(" in src


# ---------------------------------------------------------------------------
# L4 — shared LLM-test helpers in conftest.py
# ---------------------------------------------------------------------------


def test_conftest_exposes_shared_llm_helpers() -> None:
    from tests import conftest

    # Five callables / classes test files import from here.
    assert callable(conftest.make_llm_service)
    assert callable(conftest.make_preset)
    assert callable(conftest.cache_key_for)
    assert callable(conftest.wire_stub_adapter)
    assert isinstance(conftest.StubAdapter, type)


def test_conftest_cache_key_matches_service_resolve_one() -> None:
    """The cache-key helper must mirror the format
    `LlmService._resolve_one` actually uses; otherwise tests pre-populate
    the wrong slot and silently miss the cache."""
    from tests.conftest import cache_key_for, make_preset

    # Hosted preset, no inline key, no base_url → secret_marker is the ref.
    p1 = make_preset("p1", api_key_ref="env://X")
    assert cache_key_for(p1) == "gemini::model-p1::-::env://X"

    # Inline-key preset → secret_marker becomes `inline:<name>`.
    p2 = make_preset("p2", api_key="sk-secret")
    assert cache_key_for(p2) == "gemini::model-p2::-::inline:p2"

    # Local-server preset with base_url → URL appears in the key.
    p3 = make_preset(
        "p3", provider="openai-compatible", base_url="http://x:8000/v1"
    )
    assert cache_key_for(p3) == (
        "openai-compatible::model-p3::http://x:8000/v1::env://X"
    )


def test_conftest_stub_adapter_is_programmable() -> None:
    """Smoke test on the StubAdapter — pre-stage outcomes, drive
    via generate, observe the call counter."""
    import asyncio

    from tests.conftest import StubAdapter

    s = StubAdapter("primary")
    s.push("ok").push(RuntimeError("boom"))

    # First call returns the staged value.
    assert asyncio.run(s.generate()) == "ok"
    assert s.calls == 1

    # Second call raises.
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(s.generate())
    assert s.calls == 2

    # Third call (queue exhausted) raises a clear error rather than IndexError.
    with pytest.raises(RuntimeError, match="ran out of outcomes"):
        asyncio.run(s.generate())
