"""Tests for the SDK ergonomics bundle (#5, #6, #7, #8 +
SDK-side cache/cost wire round-trip).

Follow-up to the Gemini-readiness bundle that landed PR #86.
The external reviewer's deferred items, plus a related fix:

  - #5 (doc): adapter cache `inline:{preset.name}` marker depends
    on the load-bearing global `_adapters.clear()` in
    `load_presets_from_db()`. Document the invariant so a future
    optimization that drops the global clear doesn't silently
    leak rotated keys through cached SDK clients.
  - SDK TokenUsage extended for cache + cost. The router started
    putting these on the wire (review3-M3 + gemini-readiness #1)
    but the SDK's `_result_to_response` and `_frame_delta_to_delta`
    were silently discarding them. Mirror the router's full field
    set and read all six from the wire.
  - #6: Promote Message / ToolCall / ToolSpec / LlmResponse /
    LlmDelta / TokenUsage / RetryPolicy / image_part /
    StreamAccumulator / LlmCallError / LlmServiceClient to the
    `bp_sdk` package surface. Agents previously had to bind to
    the internal `bp_sdk.llm` module path.
  - #7: Add `StreamAccumulator` helper for folding LlmDelta
    chunks into a single LlmResponse. Multi-turn agent loops
    that consume the streaming API need this for round-tripping
    reasoning_blocks via `Message.assistant_from_response`.
  - #8: Move hard-coded SDK thresholds
    (MAX_CONSECUTIVE_FAILURES, BUFFER_RESOLVES_S,
    BUFFER_MAX_SIZE) to AgentConfig fields with safe defaults
    so slow-network deployments can tune without forking.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# #5: Doc-only invariant on the adapter cache marker
# ===========================================================================


def test_5_adapter_cache_marker_documents_load_bearing_clear() -> None:
    """The `inline:{preset.name}` marker is safe TODAY only because
    `load_presets_from_db()` does a global `_adapters.clear()`.
    Pin that the comment block explaining this load-bearing
    invariant is present so a future optimization can't silently
    drop the global clear and let rotated keys leak through
    cached SDK clients."""
    from bp_router.llm import service as svc_module

    src = inspect.getsource(svc_module)
    # Pin the rationale comment so a future maintainer optimizing
    # `load_presets_from_db` sees the dependency.
    assert "LOAD-BEARING INVARIANT" in src, (
        "gemini-readiness #5 regression: the load-bearing-clear "
        "comment block has been removed"
    )
    assert "_adapters.clear()" in src


# ===========================================================================
# SDK TokenUsage cache/cost round-trip (related to gemini-readiness #1)
# ===========================================================================


def test_sdk_token_usage_has_cache_and_cost_fields() -> None:
    """The SDK's `TokenUsage` dataclass must mirror the router's —
    `cache_read_tokens`, `cache_write_tokens`, `cost_microusd`
    alongside the original three. Without this, the router's
    streaming-path #1 fix (which puts cache + cost on the wire)
    has nowhere to land in agent code."""
    from bp_sdk.llm import TokenUsage

    # Default-construction must succeed.
    usage = TokenUsage()
    # All six fields must exist with the same names the router uses.
    for field_name in (
        "input_tokens",
        "output_tokens",
        "thoughts_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_microusd",
    ):
        assert hasattr(usage, field_name), (
            f"SDK TokenUsage missing {field_name} — wire data dropped"
        )
        # Default = 0.
        assert getattr(usage, field_name) == 0


def test_sdk_result_to_response_reads_all_usage_fields() -> None:
    """`_result_to_response` must read all six wire fields from
    `result.usage`. Pin via source so a future refactor that
    drops one is caught."""
    from bp_sdk import llm as llm_module

    src = inspect.getsource(llm_module._result_to_response)
    for wire_field in (
        "input_tokens",
        "output_tokens",
        "thoughts_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_microusd",
    ):
        assert f'"{wire_field}"' in src, (
            f"_result_to_response no longer reads {wire_field} from wire"
        )


def test_sdk_frame_delta_to_delta_reads_all_usage_fields() -> None:
    """`_frame_delta_to_delta` mirrors `_result_to_response` for
    streaming. All six fields must be read so per-delta usage
    snapshots carry the full set."""
    from bp_sdk import llm as llm_module

    src = inspect.getsource(llm_module._frame_delta_to_delta)
    for wire_field in (
        "input_tokens",
        "output_tokens",
        "thoughts_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_microusd",
    ):
        assert f'"{wire_field}"' in src


# ===========================================================================
# #6: Public exports promoted to the bp_sdk package surface
# ===========================================================================


def test_6_bp_sdk_promotes_llm_names_to_package_surface() -> None:
    """All the names agent authors need MUST be importable
    directly from `bp_sdk`, not via the internal `bp_sdk.llm`
    module path."""
    import bp_sdk

    expected = [
        "Agent",
        "AgentConfig",
        "CancellationError",
        "HandlerError",
        "InputValidationError",
        "LlmCallError",
        "LlmDelta",
        "LlmResponse",
        "LlmServiceClient",
        "Message",
        "NotFoundError",
        "PermissionDeniedError",
        "RetryPolicy",
        "StreamAccumulator",
        "TaskContext",
        "TokenUsage",
        "ToolCall",
        "ToolSpec",
        "UpstreamError",
        "image_part",
        "load_agent_config",
    ]
    for name in expected:
        assert hasattr(bp_sdk, name), (
            f"gemini-readiness #6: {name} not exposed on bp_sdk"
        )
        assert name in bp_sdk.__all__, (
            f"gemini-readiness #6: {name} missing from bp_sdk.__all__"
        )


def test_6_bp_sdk_imports_match_internal_classes() -> None:
    """`bp_sdk.LlmResponse` MUST be the same class as
    `bp_sdk.llm.LlmResponse` — not a re-implementation. Catches
    a regression that creates a parallel class hierarchy."""
    import bp_sdk
    import bp_sdk.llm

    for name in (
        "Message", "ToolCall", "ToolSpec",
        "LlmResponse", "LlmDelta", "TokenUsage",
        "RetryPolicy", "image_part", "StreamAccumulator",
        "LlmCallError", "LlmServiceClient",
    ):
        assert getattr(bp_sdk, name) is getattr(bp_sdk.llm, name), (
            f"gemini-readiness #6: bp_sdk.{name} is not bp_sdk.llm.{name}"
        )


# ===========================================================================
# #7: StreamAccumulator behavioural tests
# ===========================================================================


def test_7_stream_accumulator_concatenates_text_deltas() -> None:
    """Plain text deltas concatenate in arrival order."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="Hello "))
    acc.add(LlmDelta(text="world"))
    acc.add(LlmDelta(text="!"))
    response = acc.build()
    assert response.text == "Hello world!"


def test_7_stream_accumulator_separates_thought_text() -> None:
    """`thought=True` deltas go into `thought_summary`, NOT into
    the user-facing `text`. Mirrors Gemini's
    `include_thoughts=True` shape — agents shouldn't surface
    thinking-trace text as the assistant reply."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="<thinking>", thought=True))
    acc.add(LlmDelta(text="Real answer"))
    acc.add(LlmDelta(text=" continues"))
    acc.add(LlmDelta(text="</thinking>", thought=True))
    response = acc.build()
    assert response.text == "Real answer continues"
    assert response.thought_summary == "<thinking></thinking>"


def test_7_stream_accumulator_thought_summary_none_when_no_thoughts() -> None:
    """`thought_summary` defaults to None (NOT empty string) when
    no thought deltas were seen. Matches the unary path's
    LlmResponse semantics."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="just content"))
    response = acc.build()
    assert response.thought_summary is None


def test_7_stream_accumulator_appends_tool_calls_in_order() -> None:
    """Per-delta `tool_call` appended as a list. Router emits
    fully-formed ToolCall per delta — no per-delta assembly here."""
    from bp_sdk import LlmDelta, StreamAccumulator, ToolCall

    tc1 = ToolCall(id="c1", name="get_weather", args={"city": "SF"})
    tc2 = ToolCall(id="c2", name="get_news", args={})
    acc = StreamAccumulator()
    acc.add(LlmDelta(tool_call=tc1))
    acc.add(LlmDelta(tool_call=tc2))
    response = acc.build()
    assert response.tool_calls == [tc1, tc2]


def test_7_stream_accumulator_appends_reasoning_blocks_in_order() -> None:
    """Reasoning blocks accumulate per-delta. Anthropic's
    `thinking` / `redacted_thinking` MUST be returned unchanged
    on the next assistant turn during tool use, so order matters."""
    from bp_sdk import LlmDelta, StreamAccumulator

    b1 = {"type": "thinking", "text": "step 1"}
    b2 = {"type": "thinking", "text": "step 2"}
    b3 = {"type": "redacted_thinking", "data": "opaque"}
    acc = StreamAccumulator()
    acc.add(LlmDelta(reasoning_block=b1))
    acc.add(LlmDelta(text="some text"))  # interleaved
    acc.add(LlmDelta(reasoning_block=b2))
    acc.add(LlmDelta(reasoning_block=b3))
    response = acc.build()
    assert response.reasoning_blocks == [b1, b2, b3]


def test_7_stream_accumulator_max_aggregates_anthropic_cumulative_usage() -> None:
    """Anthropic emits cumulative usage in `message_delta`. The
    accumulator must `max()` per-field so cumulative reports
    don't double-count."""
    from bp_sdk import LlmDelta, StreamAccumulator, TokenUsage

    acc = StreamAccumulator()
    # Cumulative-style: each delta's usage covers everything-so-far.
    acc.add(LlmDelta(usage=TokenUsage(input_tokens=100, output_tokens=10)))
    acc.add(LlmDelta(usage=TokenUsage(input_tokens=100, output_tokens=25)))
    acc.add(LlmDelta(usage=TokenUsage(input_tokens=100, output_tokens=42)))
    response = acc.build()
    # max() across the three: 100 + 42, NOT 300 + 77.
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 42


def test_7_stream_accumulator_carries_cache_and_cost_in_usage() -> None:
    """Cache + cost fields propagate too. End-to-end with the
    SDK TokenUsage extension above."""
    from bp_sdk import LlmDelta, StreamAccumulator, TokenUsage

    acc = StreamAccumulator()
    acc.add(LlmDelta(usage=TokenUsage(
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=750,
        cache_write_tokens=128,
        cost_microusd=4200,
    )))
    response = acc.build()
    assert response.usage.cache_read_tokens == 750
    assert response.usage.cache_write_tokens == 128
    assert response.usage.cost_microusd == 4200


def test_7_stream_accumulator_finish_reason_latest_non_none_wins() -> None:
    """`finish_reason` uses last-non-None semantics. A mid-stream
    `tool_calls` reason isn't overwritten by a final None; a
    final `stop` overwrites a mid-stream `tool_calls`."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="hi"))
    acc.add(LlmDelta(finish_reason="tool_calls"))
    acc.add(LlmDelta(finish_reason="stop"))  # finalises
    response = acc.build()
    assert response.finish_reason == "stop"


def test_7_stream_accumulator_finish_reason_defaults_to_stop() -> None:
    """If no delta supplied a finish_reason, default to `stop`
    (matches LlmResponse.finish_reason default)."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="content only"))
    response = acc.build()
    assert response.finish_reason == "stop"


def test_7_stream_accumulator_thought_signature_latest_wins() -> None:
    """Gemini 3 function-calling requires the LATEST
    thought_signature on the round-trip; the accumulator must
    keep the last non-None value."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(thought_signature="sig1"))
    acc.add(LlmDelta(text="more text"))
    acc.add(LlmDelta(thought_signature="sig2"))
    response = acc.build()
    assert response.thought_signature == "sig2"


def test_7_stream_accumulator_skips_meta_deltas() -> None:
    """Meta-only deltas (router-side retry hints) are status
    notifications, not part of the final response. Skipped
    silently."""
    from bp_protocol.frames import LlmDeltaMeta
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="real"))
    acc.add(LlmDelta(meta=LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=1.0,
        reason_code="rate_limited",
    )))
    acc.add(LlmDelta(text=" content"))
    response = acc.build()
    assert response.text == "real content"


def test_7_stream_accumulator_build_is_idempotent() -> None:
    """Calling `build()` twice yields equivalent objects (helper
    isn't reset). Pin so a future refactor that empties the
    state in build() is caught."""
    from bp_sdk import LlmDelta, StreamAccumulator

    acc = StreamAccumulator()
    acc.add(LlmDelta(text="hi"))
    r1 = acc.build()
    r2 = acc.build()
    assert r1.text == r2.text == "hi"


# ===========================================================================
# #8: Hard-coded thresholds moved to AgentConfig
# ===========================================================================


def test_8_agent_config_exposes_recv_consecutive_failures_max() -> None:
    """`AgentConfig.recv_consecutive_failures_max` exists and
    defaults to the previous hard-coded value (16)."""
    from bp_sdk import AgentConfig

    cfg = AgentConfig()  # type: ignore[call-arg]
    assert hasattr(cfg, "recv_consecutive_failures_max")
    assert cfg.recv_consecutive_failures_max == 16


def test_8_agent_config_exposes_pending_buffer_window_s() -> None:
    """`AgentConfig.pending_buffer_window_s` defaults to the
    previous hard-coded BUFFER_RESOLVES_S value (5.0)."""
    from bp_sdk import AgentConfig

    cfg = AgentConfig()  # type: ignore[call-arg]
    assert hasattr(cfg, "pending_buffer_window_s")
    assert cfg.pending_buffer_window_s == 5.0


def test_8_agent_config_exposes_pending_buffer_max_size() -> None:
    """`AgentConfig.pending_buffer_max_size` defaults to the
    previous hard-coded BUFFER_MAX_SIZE value (1024)."""
    from bp_sdk import AgentConfig

    cfg = AgentConfig()  # type: ignore[call-arg]
    assert hasattr(cfg, "pending_buffer_max_size")
    assert cfg.pending_buffer_max_size == 1024


def test_8_recv_loop_reads_threshold_from_config() -> None:
    """Source pin: the recv loop's MAX_CONSECUTIVE_FAILURES MUST
    come from `self.agent.config.recv_consecutive_failures_max`,
    not a hard-coded literal. A regression that re-introduces
    `MAX_CONSECUTIVE_FAILURES = 16` defeats the per-deployment
    tuning."""
    from bp_sdk import dispatch as sdk_dispatch

    src = inspect.getsource(sdk_dispatch.Dispatcher)
    assert "self.agent.config.recv_consecutive_failures_max" in src, (
        "gemini-readiness #8 regression: recv loop no longer reads "
        "the threshold from AgentConfig"
    )


def test_8_dispatcher_init_propagates_buffer_overrides() -> None:
    """The Dispatcher constructor must apply
    `pending_buffer_window_s` and `pending_buffer_max_size`
    onto PendingMap so the overrides take effect."""
    from bp_sdk import dispatch as sdk_dispatch

    src = inspect.getsource(sdk_dispatch.Dispatcher.__init__)
    assert "PendingMap.BUFFER_RESOLVES_S" in src
    assert "PendingMap.BUFFER_MAX_SIZE" in src
    assert "pending_buffer_window_s" in src
    assert "pending_buffer_max_size" in src


def test_8_agent_config_threshold_validators_reject_zero_and_negative() -> None:
    """Bounds: failures_max ≥ 1, window_s > 0, max_size ≥ 1.
    Misconfigurations fail fast at startup."""
    pytest.importorskip("pydantic_settings")
    from bp_sdk import AgentConfig

    with pytest.raises(Exception):
        AgentConfig(recv_consecutive_failures_max=0)  # type: ignore[call-arg]
    with pytest.raises(Exception):
        AgentConfig(pending_buffer_window_s=0.0)  # type: ignore[call-arg]
    with pytest.raises(Exception):
        AgentConfig(pending_buffer_max_size=0)  # type: ignore[call-arg]


def test_8_pending_map_class_attrs_overridable_at_runtime() -> None:
    """The `BUFFER_RESOLVES_S` and `BUFFER_MAX_SIZE` class
    attributes are read-on-each-call (no captured-at-init copy),
    so a runtime override actually takes effect. Pin so a future
    refactor that captures them as instance attributes at __init__
    silently breaks the override path."""
    from bp_sdk.correlation import PendingMap

    original_window = PendingMap.BUFFER_RESOLVES_S
    original_size = PendingMap.BUFFER_MAX_SIZE
    try:
        PendingMap.BUFFER_RESOLVES_S = 99.0
        PendingMap.BUFFER_MAX_SIZE = 42
        pm = PendingMap(default_timeout_s=10.0)
        # Reads off the class attribute, not a copy.
        assert pm.BUFFER_RESOLVES_S == 99.0
        assert pm.BUFFER_MAX_SIZE == 42
    finally:
        PendingMap.BUFFER_RESOLVES_S = original_window
        PendingMap.BUFFER_MAX_SIZE = original_size
