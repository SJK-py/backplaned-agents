# LLM retriable-error code taxonomy (M6)

> **Status:** draft for review. No code changes proposed in this
> document — implementation lives in follow-up PRs once the wire shape
> is agreed.
>
> **Scope:** the review's M6 finding — when an LLM call fails (esp.
> mid-stream), the agent has no way to distinguish a transient
> network blip from a permanent failure. Streaming bypasses the
> retry/fallback wrapper today, so the only signal is a generic
> `internal_error`.
>
> **Outcome we want:** SDK / agent code can implement a sensible
> retry policy without provider-specific knowledge. Operators see
> meaningful error rates per cause class on dashboards.

---

## 1. The bug today

Three call sites map LLM-call failures to wire frames:

| Path | Wrapper | On failure |
| --- | --- | --- |
| `generate(stream=False)` | `LlmService._call_with_fallback` | retries `max_retries` times per preset, walks fallback chain; on chain exhaustion re-raises last exception |
| `embed` / `count_tokens` | same wrapper | same |
| `generate(stream=True)` | bypasses wrapper (`service.py`) | dispatch catches `Exception`, emits `LlmResultFrame.error.code = "internal_error"` |

`dispatch._run_llm_call`'s `except Exception` branch is generic
(`bp_router/dispatch.py`). The agent receives:

```jsonc
{
  "error": {
    "code": "internal_error",
    "message": "<str of underlying exception>"
  }
}
```

…with no machine-readable signal whether to retry. Reasonable
agent / SDK responses today:

1. Never retry → resilience tax during transient upstream blips.
2. Always retry → wastes credits on permanent failures (bad prompt,
   content filter); worsens rate-limit storms; risks duplicate work
   on streaming where partial output was already consumed.

Neither is good. Operators looking at dashboards can't break down
failure rate by cause either — every blip ends up in the same
`router_llm_calls_total{status="error"}` bucket.

## 2. Goal

When an LLM call fails:

- The agent receives **enough information** to decide retry vs. give
  up automatically, without provider-specific knowledge.
- Operators see **per-cause failure rates** on dashboards and can
  alert on `upstream_rate_limited` separately from
  `upstream_invalid_request` (the former is transient, the latter
  is application-bug-shaped).
- **Streaming-specific timing** is honest: a failure before the
  first delta CAN be retried by the router transparently; a
  failure after the first delta cannot (agent has partial output
  already).

## 3. Design space

Three shapes were considered. Each is a different point on the
"vocabulary growth vs. SDK simplicity" tradeoff.

### Option A — Boolean `retriable` flag on `error`

```jsonc
{
  "error": {
    "code": "internal_error",
    "message": "...",
    "retriable": true,
    "retry_after_seconds": 5      // optional
  }
}
```

**Pros**: minimal vocabulary growth — the existing `internal_error`
code stays. SDK-side retry loop is two lines. Forwards-compatible
for clients that don't read the new fields.

**Cons**: `retriable` becomes part of every error contract; future
callers may forget to set it correctly. Dashboards still see one
opaque bucket — operators can't break down by cause.

### Option B — Typed error codes

```jsonc
{
  "error": {
    "code": "upstream_rate_limited",
    "message": "..."
  }
}
```

New codes:

| Code | Retriable? | Meaning |
| --- | --- | --- |
| `upstream_timeout` | yes (backoff) | request to upstream timed out |
| `upstream_rate_limited` | yes (backoff, honour `retry_after`) | 429 / RateLimitError |
| `upstream_unavailable` | yes (backoff) | 502/503/504, network down |
| `upstream_invalid_request` | no | 400 — bad prompt / oversized / malformed tool spec |
| `upstream_auth_failed` | no | 401/403 — wrong API key, expired |
| `upstream_content_filter` | no | content moderation blocked the prompt or response |
| `upstream_quota_exhausted` | no (admin must rotate) | account-level quota out |
| `stream_interrupted` | no | connection dropped mid-stream after deltas had been delivered |
| `internal_error` | yes (fallback) | router-side failure not classified above |

**Pros**: dashboards alert on each cause separately. SDK code
expressing "retry only on transient" is `code in TRANSIENT_CODES` —
explicit, easy to audit.

**Cons**: vocabulary doubles. Every adapter must be able to classify
its native exception types into one of these codes. SDK pattern
matching grows. Adding a new category needs a protocol bump.

### Option C — Hybrid: typed codes + retriable flag

Both. `code` is the typed value; `retriable` and
`retry_after_seconds` are derived hints that SDK code can use
without reading every code value.

```jsonc
{
  "error": {
    "code": "upstream_rate_limited",
    "message": "...",
    "retriable": true,
    "retry_after_seconds": 12
  }
}
```

**Pros**:
- SDK retry policy is `if error.retriable: retry()` — doesn't have
  to enumerate codes.
- Dashboards still get the typed codes for cause breakdowns.
- New codes can be added without breaking SDK retry logic — as long
  as `retriable` is set, the behaviour is right.

**Cons**: two fields to keep in sync. The router must always set
`retriable` consistently with the code; documentation has to make
that mapping clear.

### Recommendation

**Option C (hybrid).** The duplication is minimal (one bool + one
optional float) and the SDK / dashboard cuts both win.

The `retriable` ↔ `code` mapping is a single source of truth — a
constant table in `bp_protocol.frames`:

```python
RETRIABLE_LLM_CODES = frozenset({
    "upstream_timeout",
    "upstream_rate_limited",
    "upstream_unavailable",
    "internal_error",
})
```

The router computes `retriable` from `code` at frame construction
time so the two cannot drift.

## 4. Wire shape

### 4.1 Extend `bp_protocol.frames.ErrorCode`

```python
class ErrorCode:
    # ... existing constants ...

    # LLM upstream errors. The router classifies provider exceptions
    # into these. `retriable` is set on the error object based on
    # `RETRIABLE_LLM_CODES`.
    LLM_UPSTREAM_TIMEOUT = "upstream_timeout"
    LLM_UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    LLM_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    LLM_UPSTREAM_INVALID_REQUEST = "upstream_invalid_request"
    LLM_UPSTREAM_AUTH_FAILED = "upstream_auth_failed"
    LLM_UPSTREAM_CONTENT_FILTER = "upstream_content_filter"
    LLM_UPSTREAM_QUOTA_EXHAUSTED = "upstream_quota_exhausted"
    LLM_STREAM_INTERRUPTED = "stream_interrupted"


RETRIABLE_LLM_CODES = frozenset({
    ErrorCode.LLM_UPSTREAM_TIMEOUT,
    ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
    ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
    ErrorCode.INTERNAL_ERROR,
})
```

### 4.2 Extend `LlmResultFrame.error`

```python
class LlmResultError(BaseModel):
    code: str
    message: str
    retriable: bool = False
    retry_after_seconds: Optional[float] = None
    # Original provider exception class name, for telemetry only.
    # Never used for routing logic on either side.
    upstream_class: Optional[str] = None


class LlmResultFrame(_FrameBase):
    # ... existing fields ...
    error: Optional[LlmResultError] = None
```

The current `error: Optional[dict[str, Any]]` becomes a typed
sub-model. **Backwards-compatible**: existing clients reading
`error["code"]` and `error["message"]` keep working;
`error["retriable"]` is the new optional field.

### 4.3 Protocol doc

`docs/router/protocol.md` §5 grows a sub-table for the LLM upstream
codes, mapping `code → retriable → typical retry-after`.

## 5. Per-provider exception classifier

Each adapter needs `_classify_exception(exc) -> RetryHint`. The
hint type:

```python
@dataclass(frozen=True)
class RetryHint:
    code: str                              # one of ErrorCode.LLM_*
    retry_after_seconds: Optional[float] = None
    upstream_class: str = ""               # for telemetry
```

### 5.1 Per-provider sketch

**OpenAI** (`bp_router/llm/providers/openai.py`):

```python
def _classify(exc: BaseException) -> RetryHint:
    import openai

    if isinstance(exc, openai.RateLimitError):
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
            retry_after_seconds=_parse_retry_after(exc),
            upstream_class=type(exc).__name__,
        )
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
            upstream_class=type(exc).__name__,
        )
    if isinstance(exc, openai.InternalServerError):
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
            upstream_class=type(exc).__name__,
        )
    if isinstance(exc, openai.AuthenticationError):
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
            upstream_class=type(exc).__name__,
        )
    if isinstance(exc, openai.BadRequestError):
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class=type(exc).__name__,
        )
    # ... etc
    return RetryHint(code=ErrorCode.INTERNAL_ERROR,
                     upstream_class=type(exc).__name__)
```

**Anthropic** — same shape; class names from `anthropic` SDK
(`RateLimitError`, `OverloadedError`, `APIConnectionError`, ...).

**Gemini (google-genai)** — class names from
`google.api_core.exceptions` and `google.genai.errors`. Map
`ResourceExhausted → RATE_LIMITED`, `ServiceUnavailable → UNAVAILABLE`,
`DeadlineExceeded → TIMEOUT`, `InvalidArgument → INVALID_REQUEST`.

**openai-compatible (vLLM, LM Studio, etc.)** — uses the OpenAI SDK's
exception classes since they reach for the same client. Same
classifier as `openai`. Local servers may produce slightly different
HTTP status mappings; we can refine the classifier as we observe
real-world failures.

### 5.2 Lazy import

Each classifier lives in its provider module and imports the SDK
lazily (matching the existing pattern). No new top-level deps for
`bp_router`.

## 6. Streaming retry timing

The streaming path needs a small state-machine restructure. **Pre-
first-delta failures CAN be retried**; post-first-delta failures
cannot.

```python
async def _generate_stream_with_setup_retry(self, adapter, kwargs, max_retries):
    """Generator that retries connection / first-delta failures up to
    max_retries times, then yields deltas without further retries."""
    for attempt in range(max_retries + 1):
        iterator = await adapter.generate(**kwargs, stream=True)
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            # Empty stream — treat as a successful no-op response.
            return
        except Exception as exc:
            hint = adapter._classify(exc)
            if hint.code in RETRIABLE_LLM_CODES and attempt < max_retries:
                wait_s = _backoff(attempt, hint.retry_after_seconds)
                # Emit a meta delta so UI clients can show a "retrying"
                # spinner during the backoff. SDK callers that don't
                # care just `if delta.meta: continue`.
                yield LlmDelta(meta=LlmDeltaMeta(
                    kind="retry_pending",
                    attempt=attempt + 1,
                    max_attempts=max_retries + 1,
                    retry_after_seconds=wait_s,
                    reason_code=hint.code,
                ))
                await asyncio.sleep(wait_s)
                continue
            raise
        else:
            yield first
            try:
                async for d in iterator:
                    yield d
            except Exception as exc:
                # Mid-stream failure. Re-raise as stream_interrupted —
                # NOT retriable; the agent already has partial output.
                raise StreamInterrupted(
                    code=ErrorCode.LLM_STREAM_INTERRUPTED,
                    after_n_deltas=...,
                    underlying=exc,
                ) from exc
            return
```

Wrinkles to handle in implementation:

- **Anthropic / OpenAI Responses use `async with stream() as s:`** —
  the connection error may surface at `__aenter__` rather than
  `__anext__`. Each adapter's `_generate_stream` needs the retry
  loop wrapped around the right scope; the abstraction may differ
  per adapter.
- **`asyncio.CancelledError` handoff**. The `Cancel` frame already
  cancels the running task. The retry loop must propagate
  `CancelledError` unchanged — never treat it as a retriable error.
- **Per-attempt counters**. Existing `llm_calls_total` increments
  once per call. The retry loop should bump
  `llm_fallback_attempts_total` (added in PR #48) so retries are
  visible. Streaming gets its own outcome label (e.g. `setup_retry`).

## 7. SDK retry policy

Once the wire surfaces structured retriability, the SDK can either:

**Pass-through** (today's behaviour, but typed): agent code reads
`response.error.code` and decides itself.

**Auto-retry** (opt-in via kwarg):

```python
@dataclass
class RetryPolicy:
    # Per-call attempts that the SDK itself initiates. The router
    # already retries inside `_call_with_fallback`, so the effective
    # worst-case attempt count is bounded by `total_attempts_cap`
    # below — that prevents the SDK × router multiplication from
    # exploding under outages.
    max_attempts: int = 3

    # Backoff schedule. Conservative defaults — small initial wait,
    # short cap, full jitter so retries fan out under load. Operators
    # tuning for low-latency UIs can shrink further; tuning for
    # bulk workloads can grow them.
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 10.0          # was 30s in the first draft
    backoff_multiplier: float = 2.0
    jitter: bool = True

    # Hard ceiling on TOTAL attempts (SDK + router-side fallback chain
    # combined) per single agent call. Without this, an agent issuing
    # one `generate()` could trigger:
    #     SDK retries × chain max_retries × chain length
    #   = 3 × 3 × 4 = 36 upstream attempts under a regional outage.
    # The cap clamps that to a single-digit ceiling. Counted against
    # any per-attempt outcome, including those that triggered
    # `llm_fallback_used_total`.
    total_attempts_cap: int = 8

    # Codes the SDK retries on. Defaults to RETRIABLE_LLM_CODES
    # (the wire constant); agents can broaden / narrow.
    retry_codes: frozenset[str] = frozenset(RETRIABLE_LLM_CODES)


await ctx.llm.generate(
    messages,
    preset="claude-haiku",
    retry=RetryPolicy(max_attempts=5),
)
```

Streaming retry on the SDK side is bounded to **pre-first-delta
only** (same constraint as the router). Once the first chunk lands,
the SDK propagates the iterator without re-issuing.

### 7.1 Visible retry-pending hint during streaming setup

When the router's streaming setup-retry loop pauses between
attempts (e.g. waiting out a `Retry-After: 5` after a 429), the
agent's `LlmDelta` stream goes silent for the backoff duration. UI
clients showing token-by-token output need a signal — otherwise
they look hung.

We extend `LlmDelta` with a `meta` field that's mutually exclusive
with the existing content fields. When `meta` is set,
`text` / `tool_call` / `reasoning_block` / `finish_reason` /
`usage` are all `None` — the delta is purely a status hint.

```python
class LlmDelta(_FrameBase):
    # ... existing fields (text, tool_call, finish_reason, ...) ...

    # Status hint, populated only on "meta" deltas. When set, the
    # other content fields MUST all be None — clients distinguish
    # via `delta.meta is not None`.
    meta: Optional[LlmDeltaMeta] = None


class LlmDeltaMeta(BaseModel):
    kind: Literal["retry_pending"]    # only value today; reserved for future
    # 1-indexed attempt number that just failed.
    attempt: int
    max_attempts: int
    # The router-side wait before the next attempt (seconds). Comes
    # from `Retry-After` for rate-limited responses; otherwise the
    # adapter's backoff schedule.
    retry_after_seconds: float
    # The classified code from the just-failed attempt. Lets the UI
    # show "rate limited; retrying in 5s" vs "upstream timeout;
    # retrying in 1s".
    reason_code: str
```

Wire shape:

```jsonc
{
  "type": "LlmDelta",
  "ref_correlation_id": "...",
  "text": null,
  "tool_call": null,
  "finish_reason": null,
  "usage": null,
  "meta": {
    "kind": "retry_pending",
    "attempt": 2,
    "max_attempts": 3,
    "retry_after_seconds": 5.2,
    "reason_code": "upstream_rate_limited"
  }
}
```

**Why a meta field on the existing frame, not a new frame type:**

- Agents iterating `async for delta in stream:` already have one
  loop. A new frame type would force them to handle two channels.
- The `ref_correlation_id` correlation already routes to the right
  in-flight call; no new wire-level plumbing needed.
- SDK clients that don't care can `if delta.meta: continue` —
  preserving the appearance of a transparent retry.

**Mutual-exclusivity invariant.** A `LlmDelta` with `meta` set MUST
have every other content field set to None. The router enforces
this at frame construction; SDK validation rejects malformed deltas
loudly so a future bug here surfaces immediately.

`finish_reason` on the FINAL post-success delta MAY be set; the
final frame is a normal delta (or the `LlmResultFrame` envelope),
not a meta one.

## 8. Implementation sequence

Five PRs, roughly equal in size:

| # | Scope | Dependency |
| --- | --- | --- |
| 1 | **Protocol bump.** Extend `ErrorCode`, add `LlmResultError` typed sub-model with `retriable` + `retry_after_seconds`, add `LlmDeltaMeta` + `LlmDelta.meta` field with the mutual-exclusivity invariant (§7.1), document new codes / fields as "reserved — emitted by future PRs", protocol doc + tests for the wire shape. | none |
| 2 | **Per-provider classifiers.** Add `_classify` to OpenAI, Anthropic, Gemini, openai-compatible adapters. Wire into `_call_with_fallback` so non-streaming retries respect `retry_after_seconds` and emit typed codes on chain exhaustion. Streaming still bypasses but failures get the typed code. | #1 |
| 3 | **Streaming setup-retry + meta deltas.** Per-adapter restructure of `_generate_stream` to wrap the connection-establishment / first-delta phase in a retry loop. Yield `LlmDelta(meta=...)` during the backoff sleep so UI clients can show a spinner. Mid-stream failures emit `stream_interrupted`. Add `llm_fallback_attempts_total{outcome="setup_retry"}`. | #2 |
| 4 | **SDK retry policy.** `RetryPolicy` dataclass with `total_attempts_cap`, integration with `ctx.llm.generate / embed / count_tokens`, default policy. Streaming retry bounded to pre-first-delta. SDK swallows `delta.meta` by default; agents opt into surfacing via a callback. | #1 |
| 5 | **Doc + capability matrix updates.** Cross-provider classifier coverage table; per-provider `Retry-After` header parsing notes; sample alert PromQL for typed codes. | #2, #3, #4 |

PRs 1, 2, 3 are protocol-side; 4 is SDK-side; 5 is docs. PR 1 is
the gate — its design decisions (Option C in §3, plus the four
resolutions in §11) determine everything that follows.

## 9. Migration / compatibility

- The `error` field on `LlmResultFrame` is currently
  `Optional[dict[str, Any]]`. The new `LlmResultError` Pydantic
  model accepts the existing `{"code", "message"}` shape and adds
  optional fields. Existing clients that read `error["code"]` keep
  working unchanged.
- Old SDKs predating this work see new typed codes (`upstream_timeout`
  etc.) they don't recognise — they fall through to their default
  "unknown error" branch, which is the current behaviour for any
  non-`internal_error` code anyway. No behavioural regression.
- The `internal_error` code stays in `RETRIABLE_LLM_CODES` for
  backwards compat: SDKs that already retry on it keep doing so.

## 10. What not to do

- **Don't auto-retry inside the dispatch handler.** Retry policy is
  the SDK's call. The router only ferries the typed information.
  Auto-retrying in dispatch would duplicate the existing
  `_call_with_fallback` for non-streaming and create surprise
  duplication for streaming.
- **Don't try to be exhaustive about every provider exception.**
  The classifier should map the SDK exceptions we've seen in
  production to the broad categories above; truly unknown
  exceptions land in `internal_error` (which is retriable) and we
  refine the classifier as we learn.
- **Don't expose the `upstream_class` field as routing logic.** It's
  a telemetry field — useful for "we just classified
  `openai.APIConnectionError` as `upstream_timeout`, was that
  right?" debugging, not for SDK retry decisions.

## 11. Resolved design decisions

The four open questions from the first draft are now resolved. Each
resolution is folded into the relevant section above; they're
re-stated here so reviewers can audit the chain of reasoning in one
place.

### 11.1 `internal_error` stays retriable — KEEP

Decision: keep `internal_error` in `RETRIABLE_LLM_CODES`. No split
into a separate "internal_retriable" code.

Rationale: the catch-all bucket exists precisely for transients we
couldn't classify (a new exception type in a future SDK release, a
network glitch we don't recognise). Marking it not-retriable would
mean SDKs default to no-retry on the long tail of unknown
transients — exactly the resilience hole this work is meant to
close. The downside (SDKs retry on genuine router-side bugs too) is
real but bounded by `total_attempts_cap` (see 11.3).

Reflected in §4.1: `RETRIABLE_LLM_CODES` includes
`ErrorCode.INTERNAL_ERROR`.

### 11.2 Backoff defaults — SHRINK CAP

Decision: tighten the cap from 30s to 10s. Defaults:

| Field | Default |
| --- | --- |
| `max_attempts` | 3 |
| `initial_backoff_s` | 0.5 |
| `backoff_multiplier` | 2.0 |
| `max_backoff_s` | **10.0** (was 30.0) |
| `jitter` | True (full jitter) |

Rationale: a 30-second cap is fine for batch / overnight workloads
but feels like an eternity from a UI client. With max_attempts=3
and the new cap, worst-case wait is 0.5 + 1 + 2 = 3.5s of backoff
plus any provider-supplied `Retry-After` (which is honoured
verbatim — the cap doesn't override it). Operators with bulk
workloads override `max_backoff_s` per call.

Reflected in §7's `RetryPolicy` definition.

### 11.3 Total-attempts cap — YES

Decision: add `total_attempts_cap: int = 8` to `RetryPolicy`.

Rationale: SDK retries × router-side fallback chain × chain
`max_retries` multiply quickly under outages. Worst case in the
default config:
`SDK 3 × chain depth 4 × per-preset retries 3 = 36 upstream calls
per agent request`. The cap clamps that to a single-digit ceiling.
Counted across every adapter attempt, including those that
triggered `llm_fallback_used_total`. When the cap is hit, the SDK
surfaces the last error to the agent without further retries.

Reflected in §7's `RetryPolicy` definition.

### 11.4 Retry-pending hint — `LlmDelta.meta` field

Decision: extend `LlmDelta` with an optional `meta: LlmDeltaMeta`
field, mutually exclusive with the existing content fields. Used
to emit a "retry_pending" hint between failed attempts of the
streaming setup-retry loop.

Why on `LlmDelta` rather than a new frame type:
- Agents already iterate `async for delta in stream:`. A new frame
  type would force them to handle two channels.
- `ref_correlation_id` correlation already routes to the right
  in-flight call; no new wire plumbing.
- Clients that don't care about the spinner can `if delta.meta:
  continue` — the retry remains transparent.

Mutual-exclusivity invariant: when `meta` is set, every other
content field MUST be None. Router enforces at frame construction;
SDK validation rejects malformed deltas loudly.

Reflected in §7.1 (full sub-model + wire shape) and the §6 retry
state machine (yields a meta delta during the backoff sleep).

---

If this design is approved, PR #1 (protocol bump) is the next
concrete step. Estimated ~150 LOC + ~30 tests; no behavioural
change beyond adding the new typed fields (no router code emits
them yet — that's PRs #2–#3).
