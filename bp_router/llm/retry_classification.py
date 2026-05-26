"""bp_router.llm.retry_classification — Shared retry/classification types.

Per-provider adapters classify their native SDK exceptions into the
typed `ErrorCode.LLM_*` vocabulary defined in `bp_protocol.frames`.
This module provides:

  - `RetryHint`: the dataclass adapters return from `_classify`.
  - `LlmUpstreamError`: the wrapper exception `_call_with_fallback`
    raises on chain exhaustion. Carries the classified `RetryHint` so
    `dispatch._run_llm_call` can surface the typed code in the
    `LlmResultFrame.error` payload.
  - `safe_classify`: reads `adapter._classify(exc)` if defined,
    otherwise falls back to a classifier-agnostic default (the
    `internal_error` bucket — which IS retriable per
    `RETRIABLE_LLM_CODES`, so transient failures still drive at least
    one router-side retry).
  - `compute_backoff`: backoff schedule used between retry attempts.
    Honours an explicit `retry_after_seconds` (mirrors HTTP
    `Retry-After`) when supplied; otherwise exponential 0.5 × 2^N
    with full jitter, capped at 10s (matches the SDK-side
    `RetryPolicy.max_backoff_s` default per design doc §11.2).

See `docs/design/llm-retriable-errors.md`.
"""

from __future__ import annotations

import email.utils
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bp_protocol.frames import ErrorCode

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryHint:
    """One classified upstream failure.

    `code` is one of the `ErrorCode.LLM_*` constants — the wire-level
    typed error reported back to the agent.

    `retry_after_seconds` is the provider's hint (HTTP `Retry-After`,
    or `RateLimitError.retry_after`). When set, the router uses it
    verbatim for the inter-attempt sleep, capped at the schedule's
    max. None means "use the schedule default".

    `upstream_class` is the provider exception class name. Telemetry
    only — never used for routing logic.
    """

    code: str
    retry_after_seconds: float | None = None
    upstream_class: str = ""


# Default hint for unclassified exceptions. `internal_error` IS in
# `RETRIABLE_LLM_CODES`, so the router still drives at least one
# retry on a brand-new exception type from a future SDK release —
# that's the "long tail of unknowns" carve-out from design doc §11.1.
DEFAULT_HINT = RetryHint(code=ErrorCode.INTERNAL_ERROR)


class LlmUpstreamError(Exception):
    """Raised by `_call_with_fallback` when the entire fallback chain
    has been exhausted.

    Carries the `RetryHint` from the LAST attempt across the chain,
    plus the original exception (`__cause__`). `dispatch._run_llm_call`
    catches this and surfaces `hint.code` in the `LlmResultFrame.error`
    payload, replacing the previous generic `internal_error`.

    Why not raise the original exception directly? Two reasons:
      1. Keeping the wire-level typed code is a contract — losing it
         to the generic `except Exception` branch defeats the whole
         M6 design.
      2. Dispatch needs to distinguish "we tried, here's what went
         wrong" from "router-internal bug" — the former gets the
         provider-classified code, the latter falls through to
         `internal_error`.
    """

    def __init__(
        self,
        *,
        hint: RetryHint,
        message: str,
    ) -> None:
        super().__init__(message)
        self.hint = hint
        self.message = message

    @property
    def code(self) -> str:
        return self.hint.code

    @property
    def retry_after_seconds(self) -> float | None:
        return self.hint.retry_after_seconds

    @property
    def upstream_class(self) -> str:
        return self.hint.upstream_class


class StreamInterrupted(Exception):
    """Streaming call failed AFTER deltas had been delivered.

    The agent already has partial output, so the router can't safely
    re-issue. Surfaced as `error.code = stream_interrupted` in the
    terminal `LlmResultFrame` (NOT retriable per design doc §3).

    `after_n_deltas` records how many deltas had streamed before the
    drop — useful for log diagnostics. The original exception is
    preserved as `__cause__`.
    """

    def __init__(
        self,
        *,
        message: str,
        after_n_deltas: int,
        upstream_class: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.after_n_deltas = after_n_deltas
        self.upstream_class = upstream_class


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def safe_classify(adapter: Any, exc: BaseException) -> RetryHint:
    """Look up `adapter._classify(exc)` if available, else return the
    default hint. Catches its own classification errors so a buggy
    classifier can't break the router (we'd rather report
    `internal_error` than crash the request)."""
    classifier = getattr(adapter, "_classify", None)
    if classifier is None:
        return DEFAULT_HINT
    try:
        result = classifier(exc)
    except Exception:  # noqa: BLE001
        return DEFAULT_HINT
    if not isinstance(result, RetryHint):
        return DEFAULT_HINT
    return result


def compute_backoff(
    attempt_idx: int,
    *,
    retry_after_seconds: float | None = None,
    max_backoff_s: float = 10.0,
    initial_backoff_s: float = 0.5,
    multiplier: float = 2.0,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> float:
    """Compute the wait between retry attempts.

    Provider hint wins when set: `Retry-After: 5` from a 429 means
    we wait 5s (capped at `max_backoff_s` so a misconfigured server
    can't pause us for an hour). Otherwise we use exponential
    backoff with full jitter — `0.5 × 2^attempt_idx`, capped, then
    sampled uniformly in `[0, that]`.

    Defaults (10s cap, 0.5s initial, ×2 multiplier, full jitter)
    match `RetryPolicy` from design doc §11.2 so the router-side
    schedule lines up with the SDK-side schedule.

    `rng` is injectable so tests can drive deterministic outputs;
    production calls leave it None and use the module-global RNG.
    """
    # Defensive clamp: a misconfigured caller passing
    # `max_backoff_s=-5` would make `random.uniform(0.0, -5)` return
    # a negative number, which `asyncio.sleep` silently treats as 0.
    # Clamp to 0 here so the schedule degrades gracefully rather
    # than depending on `asyncio.sleep`'s tolerance.
    max_backoff_s = max(0.0, max_backoff_s)
    if retry_after_seconds is not None:
        # Provider hint wins for the BASE wait, but jitter it
        # before returning. Without jitter, every worker that
        # received the same `Retry-After: 5` from a shared
        # upstream rate-limit hit will sleep exactly 5s and then
        # stampede the upstream simultaneously on retry — the
        # classic thundering-herd. Apply ±20% jitter (uniform in
        # [0.8, 1.2] × hint) so the retries spread across a small
        # window. R5 second-pass review.
        base = max(retry_after_seconds, 0.0)
        if jitter and base > 0.0:
            r = rng if rng is not None else random
            base = base * r.uniform(0.8, 1.2)
        return min(base, max_backoff_s)
    raw = initial_backoff_s * (multiplier ** attempt_idx)
    capped = min(raw, max_backoff_s)
    if not jitter:
        return capped
    r = rng if rng is not None else random
    return r.uniform(0.0, capped)


def parse_http_retry_after(exc: BaseException) -> float | None:
    """Extract `Retry-After` seconds from an SDK exception that wraps
    an HTTP response.

    Both OpenAI and Anthropic Python SDKs expose the response headers
    via `exc.response.headers` (an httpx Headers mapping). Per the
    HTTP spec, `Retry-After` accepts two forms:

      - **Delta-seconds**: `Retry-After: 5`
      - **HTTP-date**:     `Retry-After: Fri, 31 Dec 1999 23:59:59 GMT`

    Both are handled here. WAF-fronted endpoints (Cloudflare, Akamai)
    use the date form for ban-list responses where the wait is
    minutes-to-hours; an exponential-only fallback would back off
    way too quickly. The retry-policy cap (`max_backoff_s`) still
    applies downstream — extremely long dates won't pause us for
    more than 10s.

    Returns None on any parse miss — best-effort, the caller's
    schedule kicks in.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if raw is None:
        return None
    # Form 1 — delta-seconds. Try this first; it's the common case.
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    # Form 2 — HTTP-date. `parsedate_to_datetime` returns an aware
    # datetime when the date includes a timezone (HTTP dates always do).
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # Defensive — RFC says HTTP dates are always GMT, but if a
        # malformed header lacks tzinfo, assume UTC rather than crash.
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(UTC)).total_seconds()
    # If the date is in the past (clock skew, server bug), fall back
    # to 0 — the caller's schedule treats 0 as "retry immediately"
    # which is safer than emitting a negative sleep.
    return max(0.0, delta)
