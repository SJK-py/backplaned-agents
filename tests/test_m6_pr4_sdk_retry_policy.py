"""Tests for PR #4 of the M6 sequence — SDK `RetryPolicy`.

What this PR landed:
  - `RetryPolicy` dataclass on `bp_sdk.llm` with defaults from
    design doc §11 (`max_attempts=3`, `initial_backoff_s=0.5`,
    `max_backoff_s=10.0`, `total_attempts_cap=8`,
    `retry_codes=RETRIABLE_LLM_CODES`).
  - `__post_init__` clamps `max_attempts` at the cap.
  - `LlmCallError` now carries typed wire fields (`code`,
    `retriable`, `retry_after_seconds`, `upstream_class`) sourced
    from `LlmResultError`.
  - `_raise_for_error` reads the typed `LlmResultError` shape,
    replacing the old dict-style `.error.get('code')` pattern.
  - `generate / embed / count_tokens` accept `retry=` kwarg and
    drive `_run_with_retry` over the per-attempt request builder.
  - Streaming `generate(stream=True)` retries before the first
    content delta only; meta deltas are swallowed unless
    `policy.on_retry_pending` is set.

Tests build a minimal fake dispatcher / transport and drive the
client at the frame layer. Each async case is wrapped in
`asyncio.run(...)` so the suite runs without `pytest-asyncio`
(matching the PR #2 / PR #3 test style).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from bp_protocol.frames import (
    RETRIABLE_LLM_CODES,
    ErrorCode,
    LlmDeltaMeta,
    LlmResultError,
    LlmResultFrame,
)
from bp_sdk.context import CancelToken
from bp_sdk.correlation import PendingMap
from bp_sdk.llm import (
    LlmCallError,
    LlmDelta,
    LlmServiceClient,
    RetryPolicy,
    _compute_backoff,
    _raise_for_error,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records every `send()` call; tests inspect / replay on demand."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, frame: Any) -> None:
        self.sent.append(frame)


class _FakeAgentInfo:
    agent_id = "agent-test"


class _FakeAgent:
    info = _FakeAgentInfo()


class _FakeDispatcher:
    """Just enough surface for `LlmServiceClient` to operate without a
    real WebSocket / receive loop."""

    def __init__(self) -> None:
        self.agent = _FakeAgent()
        self.transport = _FakeTransport()
        self.pending_results = PendingMap(default_timeout_s=30.0)
        self._llm_streams: dict[str, asyncio.Queue] = {}
        self._task_correlations: dict[str, set] = {}

    def register_for_task(
        self,
        pmap: PendingMap,
        correlation_id: str,
        task_id: Any,
        *,
        timeout_s: Any = None,
    ) -> Any:
        # Match the real `Dispatcher.register_for_task` signature
        # (review item SDK-H1). Tests at this layer don't exercise
        # the per-task drain — they just need the future to come
        # back, so forward to `pmap.register`.
        return pmap.register(correlation_id, timeout_s=timeout_s)


class _FakeCtx:
    """Minimal `TaskContext` stand-in (the SDK only reads a few fields
    on the LLM path)."""

    def __init__(self) -> None:
        self.cancel_token = CancelToken()
        self.user_id = "u-test"
        self.task_id = "t-test"
        self.trace_id = "tr-test"
        self.span_id = "sp-test"


def _make_client() -> tuple[LlmServiceClient, _FakeDispatcher, _FakeCtx]:
    disp = _FakeDispatcher()
    ctx = _FakeCtx()
    return LlmServiceClient(ctx, disp), disp, ctx  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Resolver — replays a queue of canned router responses
# ---------------------------------------------------------------------------


def _install_unary_responses(
    disp: _FakeDispatcher,
    responses: list[LlmResultFrame],
) -> None:
    """Wire the fake transport so each `send()` resolves the matching
    pending future with the next canned response in order.

    Each `send()` call peels one response off the head of the list.
    """
    queue = list(responses)
    original_send = disp.transport.send

    async def _send(frame: Any) -> None:
        await original_send(frame)
        if not queue:
            raise AssertionError(
                f"unexpected send #{len(disp.transport.sent)}: "
                f"no canned response queued for {getattr(frame, 'kind', '?')}"
            )
        resp = queue.pop(0)
        # The router echoes the request's correlation_id as
        # ref_correlation_id; mirror that here.
        resp_with_ref = resp.model_copy(
            update={"ref_correlation_id": frame.correlation_id}
        )
        disp.pending_results.resolve(frame.correlation_id, resp_with_ref)

    disp.transport.send = _send  # type: ignore[method-assign]


def _ok_result(*, kind: str = "generate", **kwargs: Any) -> LlmResultFrame:
    base: dict[str, Any] = {
        "agent_id": "router",
        "trace_id": "tr-test",
        "span_id": "sp-router",
        "ref_correlation_id": "placeholder",
    }
    if kind == "generate":
        base["text"] = kwargs.pop("text", "ok")
    elif kind == "embed":
        base["vectors"] = kwargs.pop("vectors", [[0.1, 0.2]])
    elif kind == "count_tokens":
        base["total_tokens"] = kwargs.pop("total_tokens", 7)
    base.update(kwargs)
    return LlmResultFrame(**base)


def _error_result(
    *,
    code: str,
    message: str = "boom",
    retry_after_seconds: float | None = None,
    upstream_class: str | None = None,
) -> LlmResultFrame:
    err = LlmResultError(
        code=code,
        message=message,
        retry_after_seconds=retry_after_seconds,
        upstream_class=upstream_class,
    )
    return LlmResultFrame(
        agent_id="router",
        trace_id="tr-test",
        span_id="sp-router",
        ref_correlation_id="placeholder",
        error=err,
    )


def _stream_attempt_pusher(
    disp: _FakeDispatcher, scripts: list[list[Any]]
) -> None:
    """Wire `transport.send` so each call replays one streaming script.

    Each `scripts[i]` is a list of items to push onto the stream queue
    in order. Items may be:
      - `LlmDelta` objects (real chunks or meta deltas)
      - `LlmResultFrame` (terminal — error or success)
    """
    queue_scripts = list(scripts)
    original_send = disp.transport.send

    async def _send(frame: Any) -> None:
        await original_send(frame)
        if not queue_scripts:
            raise AssertionError("unexpected stream send: no script queued")
        script = queue_scripts.pop(0)
        # The dispatcher registers the queue keyed on
        # request.correlation_id BEFORE send(); replay onto it.
        q = disp._llm_streams[frame.correlation_id]
        for item in script:
            out_item = item
            if isinstance(item, LlmResultFrame):
                # Mirror the real receive loop, which fills in
                # `ref_correlation_id` on the way through.
                out_item = item.model_copy(
                    update={"ref_correlation_id": frame.correlation_id}
                )
            await q.put(out_item)

    disp.transport.send = _send  # type: ignore[method-assign]


async def _no_sleep(_seconds: float) -> None:
    """Replacement for `_sleep_or_cancel` so retry tests don't actually
    wait out the backoff. Cancel-token semantics aren't tested here."""
    return None


# ===========================================================================
# RetryPolicy — construction / clamping
# ===========================================================================


def test_retry_policy_defaults_match_design_doc() -> None:
    p = RetryPolicy()
    assert p.max_attempts == 3
    assert p.initial_backoff_s == 0.5
    assert p.max_backoff_s == 10.0
    assert p.backoff_multiplier == 2.0
    assert p.jitter is True
    assert p.total_attempts_cap == 8
    # `retry_codes` defaults to the protocol-side constant — single
    # source of truth shared with the router classifiers.
    assert p.retry_codes == frozenset(RETRIABLE_LLM_CODES)
    assert p.on_retry_pending is None


def test_retry_policy_max_attempts_clamped_at_cap() -> None:
    # Without the clamp, an SDK call could trigger SDK × router ×
    # chain-length attempts under outage. The cap prevents that.
    p = RetryPolicy(max_attempts=100)
    assert p.max_attempts == 8


def test_retry_policy_max_attempts_below_one_clamped_up() -> None:
    # `max_attempts=0` would short-circuit the loop and make a method
    # silently return / hit UnboundLocal — clamp to 1.
    p = RetryPolicy(max_attempts=0)
    assert p.max_attempts == 1


def test_retry_policy_custom_cap_respected() -> None:
    p = RetryPolicy(max_attempts=10, total_attempts_cap=4)
    assert p.max_attempts == 4


def test_retry_policy_custom_retry_codes_replaces_default() -> None:
    p = RetryPolicy(retry_codes=frozenset({ErrorCode.LLM_UPSTREAM_TIMEOUT}))
    # Caller wanted timeout-only; rate-limit / unavailable should be
    # excluded from the new policy.
    assert ErrorCode.LLM_UPSTREAM_TIMEOUT in p.retry_codes
    assert ErrorCode.LLM_UPSTREAM_RATE_LIMITED not in p.retry_codes


def test_retry_policy_total_attempts_cap_clamped_at_one() -> None:
    """L3 finding: a negative or zero `total_attempts_cap` would
    wrong-foot the `max_attempts` clamp logic (which checks
    `> total_attempts_cap`). Clamp the cap itself to >= 1."""
    p = RetryPolicy(max_attempts=3, total_attempts_cap=-5)
    assert p.total_attempts_cap == 1
    # `max_attempts=3` is now > cap of 1, so clamps down to 1.
    assert p.max_attempts == 1


def test_retry_policy_negative_max_attempts_clamped_to_one() -> None:
    """Same long-tail-of-bad-input policy as `max_attempts=0`."""
    p = RetryPolicy(max_attempts=-3)
    assert p.max_attempts == 1


def test_retry_policy_non_int_max_attempts_coerced() -> None:
    """L3: YAML / JSON config layers occasionally produce floats
    (`max_attempts: 3.0`). Without coercion, `range(self.max_attempts)`
    raises `TypeError` deep in the retry loop. Coerce at construction
    so misconfig fails (or recovers) here, not at retry time."""
    p = RetryPolicy(max_attempts=3.0)  # type: ignore[arg-type]
    assert p.max_attempts == 3
    assert isinstance(p.max_attempts, int)


def test_retry_policy_garbage_max_attempts_falls_back_to_one() -> None:
    """A truly unparseable `max_attempts` (e.g. a string) coerces
    via int() failure path to the safe minimum of 1."""
    p = RetryPolicy(max_attempts="not-a-number")  # type: ignore[arg-type]
    assert p.max_attempts == 1


# ===========================================================================
# _compute_backoff
# ===========================================================================


def test_compute_backoff_retry_after_overrides_schedule() -> None:
    # Provider hint takes precedence over the exponential schedule.
    p = RetryPolicy(jitter=False, initial_backoff_s=0.5, max_backoff_s=10.0)
    assert _compute_backoff(0, policy=p, retry_after_seconds=2.5) == 2.5


def test_compute_backoff_retry_after_capped_at_max() -> None:
    # A misconfigured upstream returning `Retry-After: 3600` would
    # otherwise pause us for an hour; the policy cap protects.
    p = RetryPolicy(jitter=False, max_backoff_s=10.0)
    assert _compute_backoff(0, policy=p, retry_after_seconds=3600.0) == 10.0


def test_compute_backoff_negative_retry_after_clamped_to_zero() -> None:
    p = RetryPolicy(jitter=False)
    assert _compute_backoff(0, policy=p, retry_after_seconds=-5.0) == 0.0


def test_compute_backoff_no_jitter_returns_capped_exponential() -> None:
    p = RetryPolicy(
        jitter=False,
        initial_backoff_s=0.5,
        backoff_multiplier=2.0,
        max_backoff_s=10.0,
    )
    assert _compute_backoff(0, policy=p) == 0.5
    assert _compute_backoff(1, policy=p) == 1.0
    assert _compute_backoff(4, policy=p) == 8.0
    # 0.5 × 2^10 = 512 — clamped at cap.
    assert _compute_backoff(10, policy=p) == 10.0


def test_compute_backoff_jitter_returns_value_within_range() -> None:
    p = RetryPolicy(
        jitter=True,
        initial_backoff_s=0.5,
        backoff_multiplier=2.0,
        max_backoff_s=10.0,
    )
    # Full jitter samples uniformly from `[0, capped]`. Sample a few
    # times to confirm the bound.
    for attempt in range(3):
        for _ in range(20):
            v = _compute_backoff(attempt, policy=p)
            expected_max = min(0.5 * (2 ** attempt), 10.0)
            assert 0.0 <= v <= expected_max


def test_compute_backoff_negative_max_backoff_clamped_to_zero() -> None:
    """L1 finding: a misconfigured policy with `max_backoff_s=-5` used
    to flow through to `random.uniform(0.0, -5)` which returns a
    negative number. `asyncio.sleep` rounds negatives to 0 silently
    but the contract is more obvious if we clamp here."""
    p = RetryPolicy(jitter=False, max_backoff_s=-5.0)
    # Retry-After hint path.
    assert _compute_backoff(0, policy=p, retry_after_seconds=2.0) == 0.0
    # Exponential schedule path.
    assert _compute_backoff(3, policy=p) == 0.0


def test_compute_backoff_injectable_rng_is_deterministic() -> None:
    """L2 finding: the SDK helper now mirrors the router copy's
    injectable `rng=` parameter, letting tests pin sample values."""
    import random

    p = RetryPolicy(
        jitter=True,
        initial_backoff_s=0.5,
        backoff_multiplier=2.0,
        max_backoff_s=10.0,
    )
    rng_a = random.Random(1234)
    rng_b = random.Random(1234)
    # Same seed → same sample.
    a = _compute_backoff(2, policy=p, rng=rng_a)
    b = _compute_backoff(2, policy=p, rng=rng_b)
    assert a == b
    # Different seed → different sample (statistically — both fall
    # within the bound but not equal).
    rng_c = random.Random(9999)
    c = _compute_backoff(2, policy=p, rng=rng_c)
    assert c != a


# ===========================================================================
# _raise_for_error — typed-error access
# ===========================================================================


def test_raise_for_error_no_error_is_noop() -> None:
    _raise_for_error(_ok_result())


def test_raise_for_error_propagates_typed_fields() -> None:
    # Build the typed-error frame the way the router would after a
    # 429 with `Retry-After: 5`.
    frame = _error_result(
        code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
        message="rate limit",
        retry_after_seconds=5.0,
        upstream_class="RateLimitError",
    )
    with pytest.raises(LlmCallError) as excinfo:
        _raise_for_error(frame)
    exc = excinfo.value
    assert exc.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED
    # `retriable` is auto-derived from `code` via
    # `LlmResultError._derive_retriable`; rate-limited is in
    # `RETRIABLE_LLM_CODES`.
    assert exc.retriable is True
    assert exc.retry_after_seconds == 5.0
    assert exc.upstream_class == "RateLimitError"


def test_raise_for_error_non_retriable_code_keeps_flag_false() -> None:
    # `LLM_UPSTREAM_INVALID_REQUEST` isn't in `RETRIABLE_LLM_CODES` —
    # the auto-derived flag must reflect that even when the SDK reads
    # it.
    frame = _error_result(code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST)
    with pytest.raises(LlmCallError) as excinfo:
        _raise_for_error(frame)
    assert excinfo.value.retriable is False


# ===========================================================================
# generate() / embed() / count_tokens() with retry=None
# ===========================================================================


def test_generate_success_single_attempt() -> None:
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [_ok_result(text="hello")])

    async def _drive() -> None:
        resp = await client.generate("hi")
        assert resp.text == "hello"

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 1


def test_generate_error_single_attempt_when_policy_disables_retry() -> None:
    # The SDK now defaults `retry=None` to a built-in `RetryPolicy()`
    # — callers wanting single-shot must opt out with
    # `RetryPolicy(max_attempts=1)`. Pin that contract.
    from bp_sdk.llm import RetryPolicy  # noqa: PLC0415

    client, disp, _ = _make_client()
    _install_unary_responses(
        disp, [_error_result(code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED)]
    )

    async def _drive() -> None:
        with pytest.raises(LlmCallError) as excinfo:
            await client.generate("hi", retry=RetryPolicy(max_attempts=1))
        assert excinfo.value.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 1


def test_embed_success_single_attempt() -> None:
    client, disp, _ = _make_client()
    _install_unary_responses(
        disp, [_ok_result(kind="embed", vectors=[[1.0]])]
    )

    async def _drive() -> None:
        v = await client.embed("hello")
        assert v == [[1.0]]

    asyncio.run(_drive())


def test_count_tokens_success_single_attempt() -> None:
    client, disp, _ = _make_client()
    _install_unary_responses(
        disp, [_ok_result(kind="count_tokens", total_tokens=42)]
    )

    async def _drive() -> None:
        n = await client.count_tokens("hi")
        assert n == 42

    asyncio.run(_drive())


# ===========================================================================
# Retry behaviour
# ===========================================================================


def test_retry_retries_on_retriable_code_then_succeeds() -> None:
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
        _ok_result(text="recovered"),
    ])

    async def _drive() -> None:
        # Disable real sleeping — tests should be fast.
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            resp = await client.generate(
                "hi", retry=RetryPolicy(max_attempts=3, jitter=False),
            )
        assert resp.text == "recovered"

    asyncio.run(_drive())
    # First attempt failed → second succeeded → no third send.
    assert len(disp.transport.sent) == 2


def test_retry_no_retry_on_non_retriable_code() -> None:
    # `llm_upstream_invalid_request` is the agent's bug, not transient.
    # Even with a policy in place, retrying would be wasteful.
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST),
    ])

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            with pytest.raises(LlmCallError) as excinfo:
                await client.generate("hi", retry=RetryPolicy(max_attempts=5))
        assert excinfo.value.code == ErrorCode.LLM_UPSTREAM_INVALID_REQUEST

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 1


def test_retry_max_attempts_caps_retries() -> None:
    # max_attempts=2 means TWO total attempts (one initial + one
    # retry). Three errors queued; the third never gets exercised.
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
    ])

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            with pytest.raises(LlmCallError):
                await client.generate(
                    "hi", retry=RetryPolicy(max_attempts=2, jitter=False),
                )

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 2


def test_retry_codes_excludes_unmatched_code() -> None:
    # Caller narrowed `retry_codes` to `upstream_timeout` only; a
    # rate-limit error must surface immediately even though the
    # protocol-side wide default would include it.
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED),
    ])
    narrow = RetryPolicy(
        max_attempts=3,
        retry_codes=frozenset({ErrorCode.LLM_UPSTREAM_TIMEOUT}),
    )

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            with pytest.raises(LlmCallError) as excinfo:
                await client.generate("hi", retry=narrow)
        assert excinfo.value.code == ErrorCode.LLM_UPSTREAM_RATE_LIMITED

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 1


def test_cancel_during_inter_attempt_sleep_aborts_retry_loop() -> None:
    """Coverage gap: an agent tearing down during the backoff between
    retry attempts should NOT pay for the rest of the wait. The
    cancel_token trip wakes `_sleep_or_cancel` immediately and the
    retry loop surfaces CancellationError."""
    from bp_sdk.errors import CancellationError

    client, disp, ctx = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
        _ok_result(text="should-not-reach"),
    ])
    real_sleep = client._sleep_or_cancel  # Use the real sleep, not _no_sleep.

    async def _drive() -> None:
        # Trip cancel_token shortly into the backoff. The first
        # attempt fails with a retriable code → SDK calls
        # _sleep_or_cancel → we trip cancel mid-sleep → it raises.
        async def _trip_after(_):  # noqa: ANN001
            ctx.cancel_token.trip("user requested")

        loop = asyncio.get_event_loop()
        # Schedule the trip just after the real sleep starts.
        loop.call_later(0.01, ctx.cancel_token.trip, "user requested")

        with patch.object(client, "_sleep_or_cancel", new=real_sleep):
            with pytest.raises(CancellationError):
                await client.generate(
                    "hi",
                    retry=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_s=10.0,  # would otherwise wait 10s
                        jitter=False,
                    ),
                )

    asyncio.run(_drive())
    # First attempt sent, then cancelled during sleep → no second send.
    assert len(disp.transport.sent) == 1


def test_retry_stream_interrupted_with_broadened_codes_still_terminal() -> None:
    """Coverage gap: a paranoid agent that broadens `retry_codes` to
    include `LLM_STREAM_INTERRUPTED` should NOT loop, because the
    wire-level `retriable=False` flag (auto-derived from the code
    via `LlmResultError._derive_retriable`) takes precedence over
    the agent's allowlist. Two-layer policy: code must be in
    `retry_codes` AND the wire flag must be True."""
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_STREAM_INTERRUPTED),
    ])
    # Manually broadened code set — but wire flag still wins.
    paranoid = RetryPolicy(
        max_attempts=3,
        retry_codes=frozenset({
            ErrorCode.LLM_UPSTREAM_TIMEOUT,
            ErrorCode.LLM_STREAM_INTERRUPTED,
        }),
    )

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            with pytest.raises(LlmCallError) as excinfo:
                await client.generate("hi", retry=paranoid)
        assert excinfo.value.code == ErrorCode.LLM_STREAM_INTERRUPTED
        assert excinfo.value.retriable is False

    asyncio.run(_drive())
    # Single attempt — the SDK respected the wire flag despite the
    # broadened retry_codes.
    assert len(disp.transport.sent) == 1


def test_retry_after_seconds_passed_to_backoff() -> None:
    # The provider hint must drive the inter-attempt sleep so the SDK
    # respects `Retry-After: N`. Spy on `_sleep_or_cancel`.
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(
            code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
            retry_after_seconds=2.0,
        ),
        _ok_result(text="ok"),
    ])
    sleeps: list[float] = []

    async def _record(s: float) -> None:
        sleeps.append(s)

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_record):
            await client.generate(
                "hi", retry=RetryPolicy(max_attempts=3, jitter=False),
            )

    asyncio.run(_drive())
    # `compute_backoff(retry_after_seconds=2.0)` returns 2.0 (capped at
    # 10s). The schedule is bypassed entirely when the hint is set.
    assert sleeps == [2.0]


def test_embed_retry_works_same_as_generate() -> None:
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE),
        _ok_result(kind="embed", vectors=[[3.14]]),
    ])

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            v = await client.embed(
                "hi", retry=RetryPolicy(max_attempts=2, jitter=False),
            )
        assert v == [[3.14]]

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 2


def test_count_tokens_retry_works_same_as_generate() -> None:
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
        _ok_result(kind="count_tokens", total_tokens=11),
    ])

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            n = await client.count_tokens(
                "hi", retry=RetryPolicy(max_attempts=2, jitter=False),
            )
        assert n == 11

    asyncio.run(_drive())


def test_retry_each_attempt_uses_fresh_correlation_id() -> None:
    # Reusing a correlation_id across attempts would make the
    # dispatcher's pending-results map collide on the second
    # `register()`. Each attempt must build a fresh request.
    client, disp, _ = _make_client()
    _install_unary_responses(disp, [
        _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT),
        _ok_result(text="ok"),
    ])

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            await client.generate(
                "hi", retry=RetryPolicy(max_attempts=2, jitter=False),
            )

    asyncio.run(_drive())
    cids = [f.correlation_id for f in disp.transport.sent]
    assert len(cids) == 2 and cids[0] != cids[1]


# ===========================================================================
# Streaming — meta delta swallowing / callback / pre-first-delta retry
# ===========================================================================


def test_stream_meta_delta_swallowed_by_default() -> None:
    # No `on_retry_pending` callback → SDK acts as if meta deltas
    # never existed; agent only sees content.
    client, disp, _ = _make_client()
    meta = LlmDelta(meta=LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=1.0,
        reason_code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
    ))
    chunk = LlmDelta(text="hi")
    terminal = _ok_result(text="hi")
    _stream_attempt_pusher(disp, [[meta, chunk, terminal]])

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True)
        async for d in gen:
            out.append(d)
        return out

    out = asyncio.run(_drive())
    # Meta delta is swallowed; only the content delta is yielded.
    assert len(out) == 1 and out[0].text == "hi"


def test_stream_on_retry_pending_callback_invoked_with_meta() -> None:
    # When the callback IS provided, the SDK forwards the meta payload
    # to the agent's UI hook. Meta still doesn't show up in the
    # iterator output.
    client, disp, _ = _make_client()
    meta_payload = LlmDeltaMeta(
        kind="retry_pending",
        attempt=2,
        max_attempts=3,
        retry_after_seconds=4.5,
        reason_code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
    )
    chunk = LlmDelta(text="content")
    terminal = _ok_result()
    _stream_attempt_pusher(disp, [
        [LlmDelta(meta=meta_payload), chunk, terminal],
    ])
    seen: list[Any] = []

    def _hint(m: Any) -> None:
        seen.append(m)

    policy = RetryPolicy(on_retry_pending=_hint)

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True, retry=policy)
        async for d in gen:
            out.append(d)
        return out

    out = asyncio.run(_drive())
    assert seen == [meta_payload]
    assert [d.text for d in out] == ["content"]


def test_stream_on_retry_pending_exception_does_not_break_stream() -> None:
    # A buggy UI callback shouldn't tear down the agent's stream. The
    # SDK logs and continues.
    client, disp, _ = _make_client()
    meta = LlmDelta(meta=LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=0.0,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
    ))
    _stream_attempt_pusher(disp, [
        [meta, LlmDelta(text="ok"), _ok_result()],
    ])

    def _bad(_m: Any) -> None:
        raise RuntimeError("oops")

    policy = RetryPolicy(on_retry_pending=_bad)

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True, retry=policy)
        async for d in gen:
            out.append(d)
        return out

    out = asyncio.run(_drive())
    assert [d.text for d in out] == ["ok"]


def test_stream_on_retry_pending_async_callback_is_awaited() -> None:
    """M2 finding: an `async def on_retry_pending` callback was
    silently broken — the call returned a coroutine that was never
    awaited (RuntimeWarning at GC, no UI hint). The SDK now detects
    coroutines and awaits them inline."""
    client, disp, _ = _make_client()
    meta_payload = LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=0.0,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
    )
    _stream_attempt_pusher(disp, [
        [LlmDelta(meta=meta_payload), LlmDelta(text="content"), _ok_result()],
    ])
    seen: list[Any] = []

    async def _async_hint(m: Any) -> None:
        # Real async work: yield to the loop so the test fails if the
        # SDK calls but doesn't await.
        await asyncio.sleep(0)
        seen.append(m)

    policy = RetryPolicy(on_retry_pending=_async_hint)

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True, retry=policy)
        async for d in gen:
            out.append(d)
        return out

    out = asyncio.run(_drive())
    # Async callback observed the meta payload exactly once.
    assert seen == [meta_payload]
    # Content delta still flowed to the agent.
    assert [d.text for d in out] == ["content"]


def test_stream_on_retry_pending_async_callback_exception_logged() -> None:
    """An async callback that raises inside its body is logged and
    swallowed, same contract as the sync path. The stream stays
    intact."""
    client, disp, _ = _make_client()
    meta = LlmDelta(meta=LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=0.0,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
    ))
    _stream_attempt_pusher(disp, [
        [meta, LlmDelta(text="ok"), _ok_result()],
    ])

    async def _bad(_m: Any) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("async oops")

    policy = RetryPolicy(on_retry_pending=_bad)

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True, retry=policy)
        async for d in gen:
            out.append(d)
        return out

    out = asyncio.run(_drive())
    # Stream survived; agent saw the content delta.
    assert [d.text for d in out] == ["ok"]


# ===========================================================================
# Streaming abort — M3: agent breaking out mid-iteration sends CancelFrame
# ===========================================================================


def _count_cancel_frames(disp: _FakeDispatcher) -> int:
    """Count the CancelFrames that have been sent via the fake transport."""
    from bp_protocol.frames import CancelFrame as _CancelFrame
    return sum(1 for f in disp.transport.sent if isinstance(f, _CancelFrame))


def test_stream_break_mid_iteration_sends_cancel_frame() -> None:
    """M3 finding: `async for d in gen: break` after a content delta
    used to leave the router producing deltas to a queue the SDK
    had just popped from `_llm_streams`. The SDK now sends a
    `CancelFrame` in the iterator's `finally` so the router stops
    pushing immediately."""
    client, disp, _ = _make_client()
    # Three content deltas + a terminal LlmResult, but the agent
    # will only consume the first two and break.
    _stream_attempt_pusher(disp, [
        [
            LlmDelta(text="one"),
            LlmDelta(text="two"),
            LlmDelta(text="three"),
            _ok_result(),
        ],
    ])

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True)
        async for d in gen:
            out.append(d)
            if len(out) == 2:
                break
        return out

    out = asyncio.run(_drive())
    # Agent saw the two deltas it consumed.
    assert [d.text for d in out] == ["one", "two"]
    # Crucially: a CancelFrame was sent so the router knows to stop.
    assert _count_cancel_frames(disp) == 1


def test_stream_natural_completion_sends_no_cancel_frame() -> None:
    """Inverse case: when the iterator runs to completion (`_END` or
    terminal `LlmResultFrame`), the router has already terminated
    naturally — no Cancel needed. We check that the cleanup path
    doesn't fire spuriously."""
    client, disp, _ = _make_client()
    _stream_attempt_pusher(disp, [
        [LlmDelta(text="hi"), _ok_result()],
    ])

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True)
        async for d in gen:
            out.append(d)
        return out

    asyncio.run(_drive())
    # Terminal LlmResultFrame means router said "I'm done"; no Cancel.
    assert _count_cancel_frames(disp) == 0


def test_stream_router_error_sends_no_cancel_frame() -> None:
    """When the router signals an error via terminal `LlmResultFrame`,
    that's also a clean termination from the router's side — Cancel
    would be redundant noise. Opt out of the default retry policy with
    `max_attempts=1` since this test only queues one attempt."""
    from bp_sdk.llm import RetryPolicy  # noqa: PLC0415

    client, disp, _ = _make_client()
    _stream_attempt_pusher(disp, [
        [_error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT)],
    ])

    async def _drive() -> None:
        gen = await client.generate(
            "prompt", stream=True, retry=RetryPolicy(max_attempts=1)
        )
        with pytest.raises(LlmCallError):
            async for _ in gen:
                pass

    asyncio.run(_drive())
    assert _count_cancel_frames(disp) == 0


def test_stream_break_after_meta_delta_only_sends_cancel_frame() -> None:
    """If the agent breaks after seeing only a meta delta (which is
    swallowed before reaching the iterator), the SDK still treats
    the iteration as aborted and sends Cancel. Edge case for the
    interaction between meta-swallowing and the cancel-on-abort
    path."""
    client, disp, _ = _make_client()
    meta = LlmDelta(meta=LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=0.0,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
    ))
    _stream_attempt_pusher(disp, [
        [meta, LlmDelta(text="content"), _ok_result()],
    ])
    # Agent consumes the first content delta, then breaks BEFORE the
    # router's terminal envelope arrives.

    async def _drive() -> list[LlmDelta]:
        out: list[LlmDelta] = []
        gen = await client.generate("prompt", stream=True)
        async for d in gen:
            out.append(d)
            break
        return out

    out = asyncio.run(_drive())
    assert [d.text for d in out] == ["content"]
    assert _count_cancel_frames(disp) == 1


def test_stream_retries_setup_failure_before_any_delta() -> None:
    # Router exhausted its chain BEFORE delivering content. SDK
    # re-issues per `RetryPolicy`.
    client, disp, _ = _make_client()
    _stream_attempt_pusher(disp, [
        [_error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT)],
        [LlmDelta(text="recovered"), _ok_result()],
    ])

    async def _drive() -> list[LlmDelta]:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            gen = await client.generate(
                "prompt",
                stream=True,
                retry=RetryPolicy(max_attempts=2, jitter=False),
            )
            return [d async for d in gen]

    out = asyncio.run(_drive())
    assert [d.text for d in out] == ["recovered"]
    assert len(disp.transport.sent) == 2


def test_stream_no_retry_after_first_delta() -> None:
    # Once a content delta has been yielded, a downstream failure is
    # `stream_interrupted` (not retriable per design doc §3). The SDK
    # propagates it without re-issuing.
    client, disp, _ = _make_client()
    _stream_attempt_pusher(disp, [
        [
            LlmDelta(text="partial"),
            _error_result(code=ErrorCode.LLM_STREAM_INTERRUPTED),
        ],
        # If the SDK incorrectly retried, the second script would be
        # consumed and the test would NOT raise.
        [LlmDelta(text="should-not-see"), _ok_result()],
    ])
    out: list[LlmDelta] = []

    async def _drive() -> None:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            gen = await client.generate(
                "prompt",
                stream=True,
                retry=RetryPolicy(max_attempts=3, jitter=False),
            )
            with pytest.raises(LlmCallError) as excinfo:
                async for d in gen:
                    out.append(d)
            assert excinfo.value.code == ErrorCode.LLM_STREAM_INTERRUPTED

    asyncio.run(_drive())
    assert [d.text for d in out] == ["partial"]
    # Only the first script consumed.
    assert len(disp.transport.sent) == 1


def test_stream_single_attempt_when_policy_disables_retry() -> None:
    # Callers wanting single-shot must opt out with
    # `RetryPolicy(max_attempts=1)` — `retry=None` now defaults to
    # the built-in 3-attempt policy.
    client, disp, _ = _make_client()
    _stream_attempt_pusher(disp, [
        [_error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT)],
    ])

    async def _drive() -> None:
        gen = await client.generate(
            "prompt", stream=True, retry=RetryPolicy(max_attempts=1)
        )
        with pytest.raises(LlmCallError) as excinfo:
            async for _ in gen:
                pass
        assert excinfo.value.code == ErrorCode.LLM_UPSTREAM_TIMEOUT

    asyncio.run(_drive())
    assert len(disp.transport.sent) == 1


def test_stream_meta_deltas_do_not_count_as_first_delta() -> None:
    # If the router emits a meta hint and THEN fails the setup, SDK
    # should still retry — meta isn't content.
    client, disp, _ = _make_client()
    meta = LlmDelta(meta=LlmDeltaMeta(
        kind="retry_pending",
        attempt=1,
        max_attempts=3,
        retry_after_seconds=0.0,
        reason_code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
    ))
    _stream_attempt_pusher(disp, [
        [meta, _error_result(code=ErrorCode.LLM_UPSTREAM_TIMEOUT)],
        [LlmDelta(text="recovered"), _ok_result()],
    ])

    async def _drive() -> list[LlmDelta]:
        with patch.object(client, "_sleep_or_cancel", new=_no_sleep):
            gen = await client.generate(
                "prompt",
                stream=True,
                retry=RetryPolicy(max_attempts=2, jitter=False),
            )
            return [d async for d in gen]

    out = asyncio.run(_drive())
    assert [d.text for d in out] == ["recovered"]
    assert len(disp.transport.sent) == 2


# ---------------------------------------------------------------------------
# embed — auto-split so request/result frames fit the negotiated payload cap
# ---------------------------------------------------------------------------


def _install_embed_responder(disp: _FakeDispatcher, *, dim: int) -> None:
    """Resolve each embed send with one `dim`-length vector per input text, so
    the responder works for whatever sub-batches `embed` chooses to send."""
    original_send = disp.transport.send

    async def _send(frame: Any) -> None:
        await original_send(frame)
        resp = _ok_result(
            kind="embed", vectors=[[0.01] * dim for _ in range(len(frame.text))]
        ).model_copy(update={"ref_correlation_id": frame.correlation_id})
        disp.pending_results.resolve(frame.correlation_id, resp)

    disp.transport.send = _send  # type: ignore[method-assign]


def test_embed_autosplits_under_payload_cap() -> None:
    from types import SimpleNamespace

    client, disp, _ctx = _make_client()
    # Tiny negotiated cap → even modest batches must split.
    disp.transport.welcome = SimpleNamespace(max_payload_bytes=2000)
    _install_embed_responder(disp, dim=8)

    inputs = [f"chunk-{i}" for i in range(40)]
    vectors = asyncio.run(client.embed(inputs, preset="emb"))

    # Every input embedded, exactly once, in order.
    assert len(vectors) == 40
    embed_sends = [f for f in disp.transport.sent if getattr(f, "kind", None) == "embed"]
    assert len(embed_sends) > 1  # the list was split across requests
    assert [t for f in embed_sends for t in f.text] == inputs
    # Each batch's inline result frame stays within the budget (or the
    # single-item floor for a lone oversized input).
    budget = int(2000 * 0.6)
    for f in embed_sends:
        assert len(f.text) == 1 or len(f.text) * 8 * 22 <= budget


def test_embed_single_request_when_it_fits() -> None:
    client, disp, _ctx = _make_client()  # no welcome → default 1 MiB cap
    _install_embed_responder(disp, dim=8)

    vectors = asyncio.run(client.embed(["a", "b", "c"], preset="emb"))

    assert len(vectors) == 3
    embed_sends = [f for f in disp.transport.sent if getattr(f, "kind", None) == "embed"]
    assert len(embed_sends) == 1  # small input → one request, unchanged
    assert embed_sends[0].text == ["a", "b", "c"]
