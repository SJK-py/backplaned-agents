"""Tests for the inline `api_key` field and the retry / fallback chain
on top of the preset system.

Pure in-memory tests: we drive `LlmService` directly with stub adapters
so we never touch a real provider SDK or the DB.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bp_router.llm.presets import (
    Preset,
    PresetCycleError,
    PresetNotAllowedError,
    PresetUnknownError,
    detect_fallback_cycles,
    walk_fallback_chain,
)
from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    LlmService,
    Message,
)

# Helpers extracted to conftest.py so all four LLM test files share a
# single source of truth for stubs and cache-key construction.
from tests.conftest import (  # noqa: E402
    StubAdapter as _StubAdapter,
)
from tests.conftest import (
    cache_key_for,
)
from tests.conftest import (
    make_llm_service as _service,
)
from tests.conftest import (
    make_preset as _preset,
)


def _wire_stub(svc: LlmService, preset: Preset, adapter: _StubAdapter) -> None:
    """Pre-populate the adapter cache for `preset`. Test-local thin
    wrapper around `tests.conftest.wire_stub_adapter` that takes an
    explicit `adapter` (the conftest version creates one if omitted)."""
    svc._adapters[cache_key_for(preset)] = adapter  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_detect_fallback_cycles_simple_chain_ok() -> None:
    presets = {
        "a": _preset("a", fallback_preset="b"),
        "b": _preset("b", fallback_preset="c"),
        "c": _preset("c"),
    }
    detect_fallback_cycles(presets)  # no raise


def test_detect_fallback_cycles_self_loop_raises() -> None:
    presets = {"a": _preset("a", fallback_preset="a")}
    with pytest.raises(PresetCycleError):
        detect_fallback_cycles(presets)


def test_detect_fallback_cycles_two_node_loop_raises() -> None:
    presets = {
        "a": _preset("a", fallback_preset="b"),
        "b": _preset("b", fallback_preset="a"),
    }
    with pytest.raises(PresetCycleError):
        detect_fallback_cycles(presets)


def test_detect_fallback_cycles_dangling_target_is_not_a_cycle() -> None:
    """Pointing at a deleted preset isn't a cycle — runtime walker
    treats unknowns as terminal (no further fallback)."""
    presets = {"a": _preset("a", fallback_preset="ghost")}
    detect_fallback_cycles(presets)  # no raise


def test_walk_fallback_chain_stops_at_unknown() -> None:
    presets = {
        "a": _preset("a", fallback_preset="ghost"),
    }
    chain = walk_fallback_chain(presets, "a")
    assert [p.name for p in chain] == ["a"]


def test_walk_fallback_chain_full_traversal() -> None:
    presets = {
        "a": _preset("a", fallback_preset="b"),
        "b": _preset("b", fallback_preset="c"),
        "c": _preset("c"),
    }
    chain = walk_fallback_chain(presets, "a")
    assert [p.name for p in chain] == ["a", "b", "c"]


def test_walk_fallback_chain_defensive_against_runtime_cycle() -> None:
    """Even if cycle detection fails to catch a cycle (e.g., from a
    direct in-memory mutation), the walker uses a `seen` set so we
    can't infinite-loop at request time."""
    presets = {
        "a": _preset("a", fallback_preset="b"),
        "b": _preset("b", fallback_preset="a"),
    }
    chain = walk_fallback_chain(presets, "a")
    assert [p.name for p in chain] == ["a", "b"]


# ---------------------------------------------------------------------------
# Inline api_key precedence in the adapter cache
# ---------------------------------------------------------------------------


def test_inline_api_key_makes_distinct_cache_entries() -> None:
    """Two presets with the same provider+model+ref but DIFFERENT
    inline keys must end up in distinct adapter cache slots so a
    secret-rotation on one preset doesn't leak into another."""
    svc = _service()
    a = _preset("a", api_key="secret-A")
    b = _preset("b", api_key="secret-B")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    stub_a, stub_b = _StubAdapter("a"), _StubAdapter("b")
    _wire_stub(svc, a, stub_a)
    _wire_stub(svc, b, stub_b)

    _, ad_a, _ = svc._resolve_one(
        preset=a, temperature=None, max_tokens=None, provider_options=None,
    )
    _, ad_b, _ = svc._resolve_one(
        preset=b, temperature=None, max_tokens=None, provider_options=None,
    )
    assert ad_a is stub_a
    assert ad_b is stub_b
    assert ad_a is not ad_b


def test_inline_api_key_resolved_on_resolve() -> None:
    """`ResolvedCallParams.api_key` carries the inline secret when
    present, so `_build_adapter` short-circuits the secret-ref
    resolver."""
    svc = _service()
    p = _preset("p", api_key="sk-inline")
    svc._register_preset_for_test(p)
    _wire_stub(svc, p, _StubAdapter())

    resolved, _, _ = svc._resolve_one(
        preset=p, temperature=None, max_tokens=None, provider_options=None,
    )
    assert resolved.api_key == "sk-inline"


def test_inline_api_key_absent_falls_through_to_ref() -> None:
    svc = _service()
    p = _preset("p", api_key=None, api_key_ref="env://Y")
    svc._register_preset_for_test(p)
    _wire_stub(svc, p, _StubAdapter())

    resolved, _, _ = svc._resolve_one(
        preset=p, temperature=None, max_tokens=None, provider_options=None,
    )
    assert resolved.api_key is None
    assert resolved.api_key_ref == "env://Y"


# ---------------------------------------------------------------------------
# Retry on the same preset
# ---------------------------------------------------------------------------


def _ok_response() -> LlmResponse:
    return LlmResponse(text="ok")


def test_generate_retries_same_preset_then_succeeds() -> None:
    svc = _service()
    p = _preset("only", max_retries=2)
    svc._register_preset_for_test(p)

    stub = _StubAdapter("only")
    stub.push(RuntimeError("boom #1"))
    stub.push(RuntimeError("boom #2"))
    stub.push(_ok_response())
    _wire_stub(svc, p, stub)

    result = asyncio.run(svc.generate(
        [Message(role="user", content="hi")],
        preset="only",
        user_level="admin",
    ))
    assert isinstance(result, LlmResponse)
    assert result.text == "ok"
    assert stub.calls == 3  # initial + 2 retries


def test_generate_exhausts_retries_then_raises_when_no_fallback() -> None:
    svc = _service()
    p = _preset("only", max_retries=1)
    svc._register_preset_for_test(p)

    stub = _StubAdapter("only")
    stub.push(RuntimeError("boom #1"))
    stub.push(RuntimeError("boom #2"))
    _wire_stub(svc, p, stub)

    # PR #2 of the M6 sequence: `_call_with_fallback` wraps the
    # last exception into `LlmUpstreamError` so dispatch can emit a
    # typed error code. Original exception preserved via __cause__.
    from bp_router.llm.retry_classification import LlmUpstreamError

    with pytest.raises(LlmUpstreamError, match="boom #2") as exc_info:
        asyncio.run(svc.generate(
            [Message(role="user", content="hi")],
            preset="only",
            user_level="admin",
        ))
    assert stub.calls == 2  # initial + 1 retry, no fallback
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert exc_info.value.code == "internal_error"  # default for unclassified


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


def test_generate_falls_back_after_retries_exhausted() -> None:
    svc = _service()
    primary = _preset("primary", max_retries=0, fallback_preset="backup")
    backup = _preset("backup")
    svc._register_preset_for_test(primary)
    svc._register_preset_for_test(backup)

    stub_primary = _StubAdapter("primary").push(RuntimeError("primary down"))
    stub_backup = _StubAdapter("backup").push(_ok_response())
    _wire_stub(svc, primary, stub_primary)
    _wire_stub(svc, backup, stub_backup)

    result = asyncio.run(svc.generate(
        [Message(role="user", content="hi")],
        preset="primary",
        user_level="admin",
    ))
    assert isinstance(result, LlmResponse)
    assert stub_primary.calls == 1
    assert stub_backup.calls == 1


def test_generate_walks_full_chain_on_repeated_failure() -> None:
    svc = _service()
    a = _preset("a", max_retries=0, fallback_preset="b")
    b = _preset("b", max_retries=1, fallback_preset="c")
    c = _preset("c", max_retries=0)
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)
    svc._register_preset_for_test(c)

    stub_a = _StubAdapter("a").push(RuntimeError("a-fail"))
    stub_b = _StubAdapter("b")
    stub_b.push(RuntimeError("b-fail-1"))
    stub_b.push(RuntimeError("b-fail-2"))
    stub_c = _StubAdapter("c").push(_ok_response())
    _wire_stub(svc, a, stub_a)
    _wire_stub(svc, b, stub_b)
    _wire_stub(svc, c, stub_c)

    result = asyncio.run(svc.generate(
        [Message(role="user", content="hi")],
        preset="a",
        user_level="admin",
    ))
    assert isinstance(result, LlmResponse)
    assert stub_a.calls == 1
    assert stub_b.calls == 2  # initial + 1 retry
    assert stub_c.calls == 1


def test_generate_chain_exhausted_raises_last_error() -> None:
    svc = _service()
    a = _preset("a", fallback_preset="b")
    b = _preset("b")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    stub_a = _StubAdapter("a").push(RuntimeError("a-fail"))
    stub_b = _StubAdapter("b").push(ValueError("b-fail"))
    _wire_stub(svc, a, stub_a)
    _wire_stub(svc, b, stub_b)

    # PR #2 wraps the LAST chain exception into LlmUpstreamError;
    # the original (here `ValueError("b-fail")`) is preserved as
    # `__cause__`. The wrapper's message mirrors the original.
    from bp_router.llm.retry_classification import LlmUpstreamError

    with pytest.raises(LlmUpstreamError, match="b-fail") as exc_info:
        asyncio.run(svc.generate(
            [Message(role="user", content="hi")],
            preset="a",
            user_level="admin",
        ))
    assert isinstance(exc_info.value.__cause__, ValueError)


# ---------------------------------------------------------------------------
# Tier gate semantics on the fallback chain
# ---------------------------------------------------------------------------


def test_first_preset_tier_gate_failure_surfaces_immediately() -> None:
    """The user explicitly asked for `restricted`, which their tier
    can't access. We must NOT silently fall back — surface
    PresetNotAllowedError so the caller knows their request was
    denied."""
    svc = _service()
    restricted = _preset(
        "restricted", min_user_level="tier0", fallback_preset="open"
    )
    open_ = _preset("open", min_user_level="*")
    svc._register_preset_for_test(restricted)
    svc._register_preset_for_test(open_)

    stub_restricted = _StubAdapter("restricted")
    stub_open = _StubAdapter("open").push(_ok_response())
    _wire_stub(svc, restricted, stub_restricted)
    _wire_stub(svc, open_, stub_open)

    with pytest.raises(PresetNotAllowedError):
        asyncio.run(svc.generate(
            [Message(role="user", content="hi")],
            preset="restricted",
            user_level="tier3",
        ))
    assert stub_restricted.calls == 0
    assert stub_open.calls == 0


def test_fallback_target_tier_gate_failure_is_silently_skipped() -> None:
    """Mid-chain fallback targets are tier-gated too, but a denial
    skips that target and continues to *its* fallback. Lets admins
    mix permissive + restricted-tier presets in one chain."""
    svc = _service()
    a = _preset("a", min_user_level="*", fallback_preset="b")
    b = _preset("b", min_user_level="tier0", fallback_preset="c")
    c = _preset("c", min_user_level="*")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)
    svc._register_preset_for_test(c)

    stub_a = _StubAdapter("a").push(RuntimeError("a-fail"))
    stub_b = _StubAdapter("b")
    stub_c = _StubAdapter("c").push(_ok_response())
    _wire_stub(svc, a, stub_a)
    _wire_stub(svc, b, stub_b)
    _wire_stub(svc, c, stub_c)

    result = asyncio.run(svc.generate(
        [Message(role="user", content="hi")],
        preset="a",
        user_level="tier3",  # blocked from b
    ))
    assert isinstance(result, LlmResponse)
    assert stub_a.calls == 1
    assert stub_b.calls == 0  # skipped silently
    assert stub_c.calls == 1


# ---------------------------------------------------------------------------
# Streaming bypass
# ---------------------------------------------------------------------------


def test_streaming_does_not_walk_fallback_chain() -> None:
    """Once we start yielding deltas there's no transparent way to
    fall back, so streaming uses only the requested preset."""
    svc = _service()
    primary = _preset("primary", fallback_preset="backup")
    backup = _preset("backup")
    svc._register_preset_for_test(primary)
    svc._register_preset_for_test(backup)

    async def _delta_iter():
        yield LlmDelta(text="hello")

    stub_primary = _StubAdapter("primary").push(_delta_iter())
    stub_backup = _StubAdapter("backup")
    _wire_stub(svc, primary, stub_primary)
    _wire_stub(svc, backup, stub_backup)

    # PR #3 of the M6 sequence: streaming returns an async generator
    # that defers the adapter call until iteration begins. So
    # `service.generate(stream=True)` itself doesn't touch any adapter;
    # the first `__anext__()` does.
    async def _drive() -> tuple[Any, list[Any]]:
        out = await svc.generate(
            [Message(role="user", content="hi")],
            preset="primary",
            user_level="admin",
            stream=True,
        )
        deltas: list[Any] = []
        async for d in out:
            deltas.append(d)
        return out, deltas

    out, deltas = asyncio.run(_drive())
    # Once iterated, only `primary` was used; backup never touched
    # (streaming doesn't walk the fallback chain).
    assert stub_primary.calls == 1
    assert stub_backup.calls == 0
    assert deltas and deltas[0].text == "hello"
    assert hasattr(out, "__aiter__")


def test_streaming_first_preset_tier_gate_still_applies() -> None:
    svc = _service()
    p = _preset("restricted", min_user_level="tier0")
    svc._register_preset_for_test(p)
    _wire_stub(svc, p, _StubAdapter("restricted"))

    with pytest.raises(PresetNotAllowedError):
        asyncio.run(svc.generate(
            [Message(role="user", content="hi")],
            preset="restricted",
            user_level="tier3",
            stream=True,
        ))


# ---------------------------------------------------------------------------
# Embed + count_tokens use the same wrapper
# ---------------------------------------------------------------------------


def test_embed_falls_back_on_failure() -> None:
    svc = _service()
    a = _preset("a", provider="openai-embeddings",
                concrete_model="text-embedding-3-small",
                fallback_preset="b")
    b = _preset("b", provider="openai-embeddings",
                concrete_model="text-embedding-3-large")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    stub_a = _StubAdapter("a").push(RuntimeError("a-fail"))
    stub_b = _StubAdapter("b").push([[0.1, 0.2, 0.3]])
    _wire_stub(svc, a, stub_a)
    _wire_stub(svc, b, stub_b)

    out = asyncio.run(svc.embed("hello", preset="a", user_level="admin"))
    assert out == [[0.1, 0.2, 0.3]]
    assert stub_a.calls == 1
    assert stub_b.calls == 1


def test_count_tokens_falls_back_on_failure() -> None:
    svc = _service()
    a = _preset("a", fallback_preset="b")
    b = _preset("b")
    svc._register_preset_for_test(a)
    svc._register_preset_for_test(b)

    stub_a = _StubAdapter("a").push(RuntimeError("a-fail"))
    stub_b = _StubAdapter("b").push(42)
    _wire_stub(svc, a, stub_a)
    _wire_stub(svc, b, stub_b)

    out = asyncio.run(svc.count_tokens(
        [Message(role="user", content="hi")],
        preset="a",
        user_level="admin",
    ))
    assert out == 42
    assert stub_a.calls == 1
    assert stub_b.calls == 1


# ---------------------------------------------------------------------------
# Unknown preset and tier-gate edge cases
# ---------------------------------------------------------------------------


def test_unknown_first_preset_raises() -> None:
    svc = _service()
    with pytest.raises(PresetUnknownError):
        asyncio.run(svc.generate(
            [Message(role="user", content="hi")],
            preset="ghost",
            user_level="admin",
        ))


def test_unknown_fallback_target_terminates_chain_quietly() -> None:
    """A deleted fallback target shouldn't crash the chain — we
    just stop walking after the last known preset."""
    svc = _service()
    p = _preset("p", fallback_preset="ghost")
    svc._register_preset_for_test(p)

    stub = _StubAdapter("p").push(RuntimeError("boom"))
    _wire_stub(svc, p, stub)

    # Same wrap as elsewhere in this file: chain exhaustion surfaces
    # `LlmUpstreamError` with the original exception as `__cause__`.
    from bp_router.llm.retry_classification import LlmUpstreamError

    with pytest.raises(LlmUpstreamError, match="boom") as exc_info:
        asyncio.run(svc.generate(
            [Message(role="user", content="hi")],
            preset="p",
            user_level="admin",
        ))
    assert stub.calls == 1
    assert isinstance(exc_info.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# Default presets still seed without the new fields
# ---------------------------------------------------------------------------


def test_default_presets_have_no_fallback_or_inline_key() -> None:
    """Built-in defaults stay backward-compatible — no inline keys
    (operators ship their own via api_key_ref) and no fallback chain
    by default."""
    from bp_router.llm.presets import default_presets

    for p in default_presets():
        assert p.api_key is None, f"{p.name} unexpectedly has inline key"
        assert p.fallback_preset is None, f"{p.name} unexpectedly has fallback"
        assert p.max_retries == 0, f"{p.name} has non-zero default max_retries"
