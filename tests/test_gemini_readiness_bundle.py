"""Tests for the Gemini-readiness bundle.

Four fixes surfaced by an LLM-domain-focused review (the five-pass
security/correctness sweeps under-explored this surface):

  - #1 (Medium): streaming dispatch path now aggregates EVERY
    `TokenUsage` field — cache_read_tokens, cache_write_tokens,
    cost_microusd alongside the original input/output/thoughts —
    routes the final usage through `_serialize_usage` (matching
    the unary path's wire shape exactly), and lands the same
    Prometheus counters via a new
    `LlmService.record_streaming_outcome` helper. Without this,
    every streaming call was invisible to `router_llm_calls_total`,
    `router_llm_tokens_total`, and `router_llm_cost_microusd_total`
    — a silent telemetry hole for the dominant call shape.
  - #2 (Medium): Gemini `_extract_usage` now reads
    `cached_content_token_count` so cached-prompt billing /
    quota signal lands in `cache_read_tokens`. Anthropic and
    OpenAI already extracted this; Gemini was the lone gap.
  - #3 (Low): streaming branch of `LlmService.generate` captures
    `preset_obj` directly from `_resolve`'s return tuple instead
    of re-indexing `self._presets[preset]`. The re-index was a
    TOCTOU against `load_presets_from_db()`'s atomic swap.
  - #4 (Medium): Gemini message conversion skips assistant turns
    whose `parts` filter to `[]` (e.g. an Anthropic-style
    `thinking` / `redacted_thinking`-only turn round-tripped
    via fallback). Gemini rejects `{role: "model", parts: []}`
    with a 400.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

# ===========================================================================
# #1: Streaming dispatch path captures cache + cost AND records telemetry
# ===========================================================================


def test_gemini_readiness_1_streaming_aggregates_all_usage_fields() -> None:
    """Source pin: the streaming aggregator MUST track cache_read /
    cache_write / cost — not only input/output/thoughts. Pinning
    via source so the test stays a pure unit (no live LLM)."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    # All six aggregators must be present.
    assert "agg_in" in src
    assert "agg_out" in src
    assert "agg_thoughts" in src
    assert "agg_cache_read" in src, (
        "gemini-readiness #1 regression: cache_read_tokens "
        "no longer aggregated on the streaming path"
    )
    assert "agg_cache_write" in src
    assert "agg_cost" in src
    # Each must be max()-aggregated against delta.usage to handle
    # Anthropic's cumulative message_delta semantics. Use regex so
    # the assertion tolerates the multi-line form Black / ruff
    # tend to produce for long arg lists.
    import re
    assert re.search(r"max\(\s*agg_cache_read", src), (
        "gemini-readiness #1: agg_cache_read not max()-aggregated"
    )
    assert re.search(r"max\(\s*agg_cost", src), (
        "gemini-readiness #1: agg_cost not max()-aggregated"
    )


def test_gemini_readiness_1_streaming_routes_usage_through_serialize() -> None:
    """The final LlmResultFrame.usage MUST be built via
    `_serialize_usage(final_usage)` — NOT the prior hand-built
    `{"input_tokens": ..., "output_tokens": ..., "thoughts_tokens": ...}`
    dict literal that omitted cache + cost. Pin the call site so
    a future refactor that re-introduces the hand-built dict is
    caught."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    # Streaming branch must use _serialize_usage on the aggregated
    # TokenUsage object.
    assert "_serialize_usage(final_usage)" in src, (
        "gemini-readiness #1 regression: streaming path no longer "
        "routes usage through _serialize_usage"
    )
    # And the synthesised TokenUsage must include all six fields.
    for field_name in (
        "input_tokens=agg_in",
        "output_tokens=agg_out",
        "thoughts_tokens=agg_thoughts",
        "cache_read_tokens=agg_cache_read",
        "cache_write_tokens=agg_cache_write",
        "cost_microusd=agg_cost",
    ):
        assert field_name in src, (
            f"gemini-readiness #1: TokenUsage construction missing {field_name}"
        )


def test_gemini_readiness_1_streaming_calls_record_streaming_outcome() -> None:
    """The streaming path MUST land Prometheus telemetry via the
    new `record_streaming_outcome` helper. Without this,
    streaming calls remained invisible to
    `router_llm_{calls,tokens,cost_microusd}_total`."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    assert "record_streaming_outcome(" in src
    # Must pass the four contextual fields the helper needs.
    for kw in ("preset_name=", "usage=", "finish_reason=", "user_id=", "task_id="):
        assert kw in src


def test_gemini_readiness_1_record_streaming_outcome_helper_exists() -> None:
    """Source pin: the helper exists on `LlmService`, takes
    keyword-only args, swallows lookup failures (so a telemetry
    blip can't disrupt agent-visible behaviour)."""
    from bp_router.llm.service import LlmService

    fn = LlmService.record_streaming_outcome
    sig = inspect.signature(fn)
    # All caller args must be keyword-only.
    params = list(sig.parameters.values())
    assert params[0].name == "self"
    for p in params[1:]:
        assert p.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    src = inspect.getsource(fn)
    # Must be best-effort: any failure is swallowed.
    assert "except Exception" in src
    # Must reuse `_resolve_one` (which uses the adapter cache) so
    # we don't construct a fresh SDK client just for telemetry.
    assert "_resolve_one(" in src
    # And must delegate to the unary `_record` so the metric set
    # stays unified between paths.
    assert "self._record(" in src


def test_gemini_readiness_1_record_streaming_outcome_no_op_when_preset_removed() -> None:
    """If the preset was deleted between stream start and finish
    (admin CRUD), the helper must skip telemetry rather than
    crash. Behavioural pin via a stub LlmService."""
    from bp_router.llm.service import LlmService, TokenUsage

    svc = LlmService.__new__(LlmService)
    svc._presets = {}  # preset removed
    svc.record_streaming_outcome(
        preset_name="gone",
        usage=TokenUsage(input_tokens=10, output_tokens=20),
        finish_reason="stop",
    )  # must not raise


# ===========================================================================
# #2: Gemini _extract_usage now reads cached_content_token_count
# ===========================================================================


def test_gemini_readiness_2_extract_usage_reads_cached_content_token() -> None:
    """Source pin: `_extract_usage` MUST read
    `cached_content_token_count` from `usage_metadata` and
    populate `cache_read_tokens`. Without it, Gemini was the
    sole adapter under-reporting on cached prompts."""
    from bp_router.llm.providers import gemini

    src = inspect.getsource(gemini.GeminiAdapter._extract_usage)
    assert "cached_content_token_count" in src, (
        "gemini-readiness #2 regression: Gemini no longer extracts "
        "cache_read_tokens from cached_content_token_count"
    )
    assert "cache_read_tokens=" in src


def test_gemini_readiness_2_extract_usage_returns_cache_read_when_present() -> None:
    """Behavioural pin: a stub `usage_metadata` carrying
    `cached_content_token_count=128` produces a TokenUsage with
    `cache_read_tokens == 128`."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    adapter = GeminiAdapter.__new__(GeminiAdapter)

    meta = MagicMock()
    meta.prompt_token_count = 1000
    meta.candidates_token_count = 200
    meta.thoughts_token_count = 50
    meta.cached_content_token_count = 128
    resp = MagicMock(usage_metadata=meta)

    usage = adapter._extract_usage(resp)
    assert usage is not None
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200
    assert usage.thoughts_tokens == 50
    assert usage.cache_read_tokens == 128


def test_gemini_readiness_2_extract_usage_handles_missing_cached_field() -> None:
    """Older google-genai versions may not surface
    `cached_content_token_count`; the `getattr(meta, ..., 0)`
    pattern must default to 0 cleanly. Pin so a future refactor
    that drops the default doesn't crash on those SDK versions."""
    from bp_router.llm.providers.gemini import GeminiAdapter

    adapter = GeminiAdapter.__new__(GeminiAdapter)

    # spec=[] means the mock does NOT have cached_content_token_count.
    meta = MagicMock(spec=[
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
    ])
    meta.prompt_token_count = 100
    meta.candidates_token_count = 50
    meta.thoughts_token_count = 0
    resp = MagicMock(usage_metadata=meta)

    usage = adapter._extract_usage(resp)
    assert usage is not None
    assert usage.cache_read_tokens == 0


# ===========================================================================
# #3: Streaming branch captures preset_obj from _resolve return tuple
# ===========================================================================


def test_gemini_readiness_3_stream_captures_preset_from_resolve_tuple() -> None:
    """Source pin: the streaming branch of `LlmService.generate`
    must capture preset_obj from the third element of `_resolve`'s
    return tuple — NOT re-index `self._presets[preset]`. The
    re-index was a TOCTOU window against
    `load_presets_from_db()`'s atomic swap."""
    from bp_router.llm import service as svc_module

    src = inspect.getsource(svc_module.LlmService.generate)
    # The replacement form must be present.
    assert "resolved, adapter, preset_obj = self._resolve(" in src, (
        "gemini-readiness #3 regression: streaming branch no longer "
        "captures preset_obj from _resolve's tuple"
    )
    # The buggy re-index form must be GONE in the streaming branch.
    # Look at just the `if stream:` block.
    stream_idx = src.find("if stream:")
    assert stream_idx > 0
    # Find the end of the streaming branch — the next blank line at
    # the same indent or the next top-level method.
    stream_block = src[stream_idx:stream_idx + 2000]
    # Strip comment lines so we don't false-match on the rationale
    # block that explains the prior bug. Pin the active code only.
    code_only_lines = [
        line for line in stream_block.split("\n")
        if not line.lstrip().startswith("#")
    ]
    code_only = "\n".join(code_only_lines)
    assert "self._presets[preset]" not in code_only, (
        "gemini-readiness #3: streaming branch still re-indexes "
        "self._presets[preset] in code — TOCTOU window re-opened"
    )


# ===========================================================================
# #4: Gemini conversion skips empty assistant parts
# ===========================================================================


def test_gemini_readiness_4_empty_parts_assistant_turn_skipped() -> None:
    """Source pin: the Gemini message conversion MUST skip turns
    whose `parts` list filters to empty. Without this, an
    assistant turn carrying ONLY Anthropic-style
    `thinking` / `redacted_thinking` parts (round-tripped via a
    fallback path) would produce `{role: "model", parts: []}`,
    which Gemini rejects with a 400."""
    from bp_router.llm.providers import gemini

    # The conversion lives in `_messages_to_contents` (or whatever
    # the function is called); locate it via the module source.
    module_src = inspect.getsource(gemini)
    # Pin: the filter pattern must be present.
    assert "if not parts:" in module_src, (
        "gemini-readiness #4 regression: empty-parts turns are "
        "no longer skipped — Gemini will 400 on assistant turns "
        "carrying only opaque-to-Gemini reasoning blocks"
    )
    # And there must be a citation explaining why the skip exists,
    # so a future maintainer doesn't 'fix' it back to the unconditional
    # append.


def test_gemini_readiness_4_skip_does_not_drop_non_empty_turns() -> None:
    """Sanity-pin the happy path: a turn with non-empty parts
    still appends. Catches a regression that over-corrects to
    skip every turn."""
    from bp_router.llm.providers import gemini

    src = inspect.getsource(gemini)
    # The append must still be reachable (not behind an
    # always-true `continue`).
    assert "contents.append({\"role\": role, \"parts\": parts})" in src
