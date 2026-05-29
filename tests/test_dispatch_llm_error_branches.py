"""Tests for the LLM dispatch error branches in `_run_llm_call`.

The review (C8) flagged dispatch error branches as untested. The
function maps three exception classes to wire-level error codes:

  PresetUnknownError      → LlmResultFrame.error.code = "preset_unknown"
  PresetNotAllowedError   → LlmResultFrame.error.code = "preset_not_allowed"
  Generic Exception       → LlmResultFrame.error.code = "internal_error"
  asyncio.CancelledError  → propagates (no result frame; socket is gone)

Plus:
  - kind="embed"        → service.embed() called; vectors in result
  - kind="count_tokens" → service.count_tokens() called; total_tokens in result
  - kind="generate"     → service.generate() called

We drive `_run_llm_call` against an in-memory stub `state` with mocked
`llm_service` + `db_pool` + `entry.outbox`. The exception classes
under test are the real classes from `bp_router.llm.presets`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bp_protocol.frames import ErrorCode, LlmRequestFrame, LlmResultFrame
from bp_router.llm.presets import PresetNotAllowedError, PresetUnknownError

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _llm_request(
    *,
    kind: str = "generate",
    preset: str = "default",
    user_id: str = "usr_alice",
    task_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    text: list[str] | None = None,
    stream: bool = False,
) -> LlmRequestFrame:
    """Build a fully-formed `LlmRequestFrame`. The protocol envelope
    (`type`, `trace_id`, etc.) is auto-filled. `task_id` binds the call
    to a task — required for a tier-gated preset, whose gate derives the
    caller's level from the task row (active-executor verified), not the
    agent-asserted `user_id`."""
    base = {
        "type": "LlmRequest",
        "trace_id": "trc_test",
        "span_id": "spn_test",
        "agent_id": "agt_caller",
        "kind": kind,
        "preset": preset,
        "task_id": task_id,
        "messages": messages if messages is not None else [
            {"role": "user", "content": "hi"}
        ],
        "text": text or [],
        "user_id": user_id,
        "stream": stream,
    }
    return LlmRequestFrame(**base)


class _StubOutbox:
    """Captures frames the dispatcher tries to send."""

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def put(self, frame: Any) -> None:
        self.frames.append(frame)


_DEFAULT_TASK_ROW = {
    "user_id": "usr_alice",
    "session_id": "ses_1",
    "active_agent_id": "agt_caller",
}


def _make_state(*, llm_service: Any, task_row: Any = _DEFAULT_TASK_ROW) -> Any:
    """Stub `AppState` with the bits `_run_llm_call` reads.

    `task_row` is what the stub conn returns for the tier-gate's task lookup
    (`derive_task_file_scope`). Pass `None` (no such task) or a row whose
    `active_agent_id` differs from `agt_caller` to simulate a caller that is
    NOT the task's active executor."""
    state = MagicMock()
    state.llm_service = llm_service

    # db_pool.acquire() async-context manager that yields a stub conn.
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    # The tier-gate derives the trusted identity from the task row via
    # `derive_task_file_scope`, which `await conn.fetchrow(...)`s. Default to
    # a task the calling agent (agt_caller) is the active executor of, so a
    # tier-gated request resolves a real level instead of being refused as
    # unverifiable. (`*`-preset tests never reach this — the lookup is
    # skipped for them.)
    conn.fetchrow = AsyncMock(return_value=task_row)
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return state


def _make_entry() -> Any:
    entry = MagicMock()
    entry.agent_id = "agt_caller"
    entry.outbox = _StubOutbox()
    return entry


def _stub_llm_service(**method_overrides: Any) -> Any:
    """Build an `LlmService` stand-in. Only the methods accessed by
    `_run_llm_call` are wired."""
    svc = MagicMock()
    # Defaults — caller overrides per test.
    svc.get_preset = MagicMock(return_value=MagicMock(min_user_level="*"))
    svc.resolve_user_level = AsyncMock(return_value="admin")
    svc.generate = AsyncMock()
    svc.embed = AsyncMock()
    svc.count_tokens = AsyncMock()
    for k, v in method_overrides.items():
        setattr(svc, k, v)
    return svc


# ---------------------------------------------------------------------------
# Generic exception → internal_error
# ---------------------------------------------------------------------------


def test_generic_exception_in_generate_maps_to_internal_error() -> None:
    """Anything not specifically caught becomes `internal_error`. The
    message MUST be the fixed string `"internal_error"` — NOT
    `str(exc)` — so a router-internal failure can't leak host
    names, env-var hints, file paths, or upstream provider error
    bodies to the calling agent (review item review3-M2)."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(
        generate=AsyncMock(side_effect=RuntimeError("upstream 503"))
    )
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(_run_llm_call(state, entry, _llm_request(kind="generate")))

    [result] = entry.outbox.frames
    assert isinstance(result, LlmResultFrame)
    assert result.error is not None
    assert result.error.code == ErrorCode.INTERNAL_ERROR
    # Fixed redacted message — must NOT echo `str(RuntimeError)`.
    assert result.error.message == "internal_error"
    assert "upstream 503" not in (result.error.message or ""), (
        "review3-M2 regression: catch-all is leaking exception text"
    )
    # `internal_error` is in `RETRIABLE_LLM_CODES`, so `retriable`
    # auto-fills True.
    assert result.error.retriable is True


# ---------------------------------------------------------------------------
# PresetUnknownError → preset_unknown
# ---------------------------------------------------------------------------


def test_preset_unknown_maps_to_preset_unknown_code() -> None:
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(
        # First, dispatch peeks at the preset for the tier check; treat
        # unknown as None (not the full chain).
        get_preset=MagicMock(return_value=None),
        generate=AsyncMock(side_effect=PresetUnknownError("ghost-preset")),
    )
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(
        _run_llm_call(state, entry, _llm_request(preset="ghost-preset"))
    )

    [result] = entry.outbox.frames
    assert result.error.code == ErrorCode.LLM_PRESET_UNKNOWN
    assert "ghost-preset" in result.error.message
    # `preset_unknown` is NOT in `RETRIABLE_LLM_CODES`.
    assert result.error.retriable is False


# ---------------------------------------------------------------------------
# PresetNotAllowedError → preset_not_allowed
# ---------------------------------------------------------------------------


def test_preset_not_allowed_maps_to_preset_not_allowed_code() -> None:
    from bp_router.dispatch import _run_llm_call

    err = PresetNotAllowedError(
        preset_name="claude-opus",
        user_level="tier3",
        required="tier1",
    )
    svc = _stub_llm_service(generate=AsyncMock(side_effect=err))
    # The preset peeks `min_user_level=tier1` (gated).
    svc.get_preset = MagicMock(return_value=MagicMock(min_user_level="tier1"))
    # The tier gate resolves the (task-derived, verified) caller as tier3.
    svc.resolve_user_level = AsyncMock(return_value="tier3")
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    # task_id binds the gate to a task agt_caller executes; the SERVICE
    # then raises PresetNotAllowedError (tier3 < tier1) which maps here.
    asyncio.run(
        _run_llm_call(state, entry, _llm_request(preset="claude-opus", task_id="tsk_1"))
    )

    [result] = entry.outbox.frames
    assert result.error.code == ErrorCode.LLM_PRESET_NOT_ALLOWED
    # The error text mentions both the preset and the requirement so
    # operators can debug from the audit log alone.
    assert "claude-opus" in result.error.message
    assert "tier1" in result.error.message
    # Tier denial is permanent for this caller — not retriable.
    assert result.error.retriable is False


# ---------------------------------------------------------------------------
# Tier gate: trusted identity comes from the TASK, never frame.user_id
# ---------------------------------------------------------------------------


def test_tier_gate_uses_task_user_id_not_asserted_user_id() -> None:
    """The gate resolves the level from the task-derived user_id (usr_alice),
    NOT the agent-asserted frame.user_id — so a low-trust agent can't claim a
    privileged user to satisfy a tier-gated preset."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(generate=AsyncMock(return_value=MagicMock(
        text="ok", tool_calls=[], finish_reason="stop",
        usage=MagicMock(input_tokens=1, output_tokens=1, total_tokens=2),
        raw={},
    )))
    svc.get_preset = MagicMock(return_value=MagicMock(min_user_level="tier1"))
    svc.resolve_user_level = AsyncMock(return_value="admin")  # task user passes
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    # The agent ASSERTS it is acting for an admin user; the task it executes
    # belongs to usr_alice (the default task_row).
    asyncio.run(_run_llm_call(state, entry, _llm_request(
        preset="claude-opus", task_id="tsk_1", user_id="usr_admin_claimed",
    )))

    # resolve_user_level was called with the TASK's user_id, not the claim.
    called_user = svc.resolve_user_level.await_args.args[1]
    assert called_user == "usr_alice"
    assert called_user != "usr_admin_claimed"


def test_tier_gate_refuses_non_executor_agent() -> None:
    """If the calling agent is NOT the task's active executor,
    `derive_task_file_scope` returns None → no trusted identity → refuse."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service()
    svc.get_preset = MagicMock(return_value=MagicMock(min_user_level="tier1"))
    # Task is executed by a DIFFERENT agent → scope derivation returns None.
    state = _make_state(
        llm_service=svc,
        task_row={"user_id": "usr_alice", "session_id": "ses_1",
                  "active_agent_id": "agt_someone_else"},
    )
    entry = _make_entry()

    asyncio.run(_run_llm_call(state, entry, _llm_request(
        preset="claude-opus", task_id="tsk_1",
    )))

    [result] = entry.outbox.frames
    assert result.error.code == ErrorCode.LLM_PRESET_NOT_ALLOWED
    svc.generate.assert_not_awaited()  # never reached the provider


def test_tier_gate_requires_task_context() -> None:
    """A tier-gated preset with no task_id can't be bound to a trusted
    identity → refuse (don't fall back to the asserted user_id)."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service()
    svc.get_preset = MagicMock(return_value=MagicMock(min_user_level="tier1"))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(_run_llm_call(state, entry, _llm_request(
        preset="claude-opus", task_id=None,
    )))

    [result] = entry.outbox.frames
    assert result.error.code == ErrorCode.LLM_PRESET_NOT_ALLOWED
    svc.resolve_user_level.assert_not_awaited()  # no lookup without a task
    svc.generate.assert_not_awaited()


def test_star_preset_skips_tier_lookup_entirely() -> None:
    """A `*` preset never consults user_level → no task lookup, no
    resolve_user_level (the hot default path pays zero DB cost)."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(generate=AsyncMock(return_value=MagicMock(
        text="ok", tool_calls=[], finish_reason="stop",
        usage=MagicMock(input_tokens=1, output_tokens=1, total_tokens=2),
        raw={},
    )))
    svc.get_preset = MagicMock(return_value=MagicMock(min_user_level="*"))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(_run_llm_call(state, entry, _llm_request(
        preset="default", task_id="tsk_1",
    )))

    svc.resolve_user_level.assert_not_awaited()


# ---------------------------------------------------------------------------
# CancelledError must propagate (don't swallow + don't send result)
# ---------------------------------------------------------------------------


def test_cancelled_error_propagates_no_result_frame() -> None:
    """Disconnect / supersede triggers CancelledError. The dispatcher
    must NOT try to send a result frame (the socket is gone) and must
    NOT swallow the exception (the supervising task tree needs it)."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(
        generate=AsyncMock(side_effect=asyncio.CancelledError())
    )
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run_llm_call(state, entry, _llm_request()))

    # No frame should have been queued — the socket is gone.
    assert entry.outbox.frames == []


# ---------------------------------------------------------------------------
# kind="embed" routes to service.embed and packs vectors
# ---------------------------------------------------------------------------


def test_embed_kind_calls_service_embed_and_returns_vectors() -> None:
    from bp_router.dispatch import _run_llm_call

    vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    svc = _stub_llm_service(embed=AsyncMock(return_value=vectors))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(_run_llm_call(
        state, entry, _llm_request(
            kind="embed",
            preset="text-embedding-3-small",
            text=["hello", "world"],
            messages=[],
        ),
    ))

    svc.embed.assert_awaited_once()
    args, kwargs = svc.embed.call_args
    # First positional arg is the input text list.
    assert args[0] == ["hello", "world"]
    assert kwargs["preset"] == "text-embedding-3-small"

    [result] = entry.outbox.frames
    assert isinstance(result, LlmResultFrame)
    assert result.vectors == vectors
    # No error field on success.
    assert result.error is None


# ---------------------------------------------------------------------------
# kind="count_tokens" routes to service.count_tokens
# ---------------------------------------------------------------------------


def test_count_tokens_kind_calls_service_count_tokens() -> None:
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(count_tokens=AsyncMock(return_value=42))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(_run_llm_call(
        state, entry, _llm_request(
            kind="count_tokens",
            preset="default",
            messages=[{"role": "user", "content": "how many tokens?"}],
        ),
    ))

    svc.count_tokens.assert_awaited_once()
    [result] = entry.outbox.frames
    assert result.total_tokens == 42


# ---------------------------------------------------------------------------
# kind="generate" non-streaming returns full text + tool calls
# ---------------------------------------------------------------------------


def test_generate_non_streaming_returns_text_and_tool_calls() -> None:
    from bp_router.dispatch import _run_llm_call
    from bp_router.llm.service import LlmResponse, TokenUsage, ToolCall

    resp = LlmResponse(
        text="Hello, world!",
        tool_calls=[ToolCall(id="c1", name="f", args={"x": 1})],
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=10, output_tokens=20),
    )
    svc = _stub_llm_service(generate=AsyncMock(return_value=resp))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    asyncio.run(_run_llm_call(state, entry, _llm_request()))

    [result] = entry.outbox.frames
    assert result.text == "Hello, world!"
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "f"
    assert result.tool_calls[0]["args"] == {"x": 1}
    assert result.usage["input_tokens"] == 10


# ---------------------------------------------------------------------------
# preset selection: `preset` wins over legacy `model`
# ---------------------------------------------------------------------------


def test_preset_field_wins_over_legacy_model_field() -> None:
    """`preset` is the new field; `model` is legacy. When both are set,
    `preset` wins (per docstring on `_run_llm_call`)."""
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(generate=AsyncMock(side_effect=RuntimeError("x")))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    frame = LlmRequestFrame(
        type="LlmRequest",
        trace_id="trc",
        span_id="spn",
        agent_id="agt",
        kind="generate",
        preset="winning-preset",
        model="legacy-model",
        messages=[{"role": "user", "content": "hi"}],
        user_id="usr",
    )
    asyncio.run(_run_llm_call(state, entry, frame))

    # The service.generate call was invoked with preset="winning-preset",
    # not "legacy-model".
    args, kwargs = svc.generate.call_args
    assert kwargs["preset"] == "winning-preset"


def test_legacy_model_used_when_preset_field_unset() -> None:
    from bp_router.dispatch import _run_llm_call

    svc = _stub_llm_service(generate=AsyncMock(side_effect=RuntimeError("x")))
    state = _make_state(llm_service=svc)
    entry = _make_entry()

    frame = LlmRequestFrame(
        type="LlmRequest",
        trace_id="trc",
        span_id="spn",
        agent_id="agt",
        kind="generate",
        preset=None,
        model="legacy-model",
        messages=[{"role": "user", "content": "hi"}],
        user_id="usr",
    )
    asyncio.run(_run_llm_call(state, entry, frame))

    args, kwargs = svc.generate.call_args
    assert kwargs["preset"] == "legacy-model"
