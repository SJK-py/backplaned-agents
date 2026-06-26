"""bp_sdk.llm — Agent-side LLM service client.

Routes calls to the router-side LlmService over the same WebSocket frame
channel that carries every other agent traffic. Streaming generates
yield `LlmDelta` chunks; the iterator ends when the terminal
`LlmResult` arrives.

See `docs/backplaned/sdk/services.md` §1.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bp_protocol.frames import (
    CancelFrame,
    LlmDeltaFrame,
    LlmDeltaMeta,
    LlmRequestFrame,
    LlmResultFrame,
    ResultFrame,
)
from bp_sdk.errors import CancellationError

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider-neutral types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            out["name"] = self.name
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        return out

    @classmethod
    def assistant_from_response(cls, resp: LlmResponse) -> Message:
        """Rebuild the assistant turn from an LlmResponse so the model
        sees its own previous output verbatim on the next call.

        Multi-turn function calling is strict about reasoning round-trip:
          - Gemini 3 requires the first function call's
            `thought_signature` back, or 400.
          - Anthropic with extended thinking + tools requires every
            `thinking` and `redacted_thinking` block back unchanged.
        Using this helper handles both.

        Output shape:

            Message(role="assistant", content=[
                # 1) Reasoning blocks first (Anthropic; Gemini empty).
                #    Provider-shaped, opaque to the SDK.
                {"type": "thinking", "thinking": ..., "signature": ...},
                {"type": "redacted_thinking", "data": ...},
                # 2) Text part — only when resp.text is non-empty.
                {"text": resp.text,
                 "thought_signature": resp.thought_signature?},
                # 3) One part per tool call; Gemini signature on the
                #    FIRST only, mirroring parallel-call emission.
                {"function_call": {"id": ..., "name": ..., "args": ...},
                 "thought_signature": tc.thought_signature?},
                ...
            ])
        """
        parts: list[dict[str, Any]] = []
        # 1) Reasoning blocks (Anthropic-only today).
        for block in resp.reasoning_blocks:
            parts.append(dict(block))
        # 2) Text.
        if resp.text:
            text_part: dict[str, Any] = {"text": resp.text}
            # Attach the signature to the text part only when there are
            # no function calls — when there ARE calls, the mandatory
            # signature lives on the first call instead.
            if not resp.tool_calls and resp.thought_signature:
                text_part["thought_signature"] = resp.thought_signature
            parts.append(text_part)
        # 3) Tool calls.
        for i, tc in enumerate(resp.tool_calls):
            fc: dict[str, Any] = {"name": tc.name, "args": tc.args}
            if tc.id:
                fc["id"] = tc.id
            part: dict[str, Any] = {"function_call": fc}
            # Per Gemini docs: signature only on the first call of any
            # parallel batch — subsequent calls in the same response
            # carry no signature.
            if i == 0 and tc.thought_signature:
                part["thought_signature"] = tc.thought_signature
            parts.append(part)
        if not parts:
            return cls(role="assistant", content="")
        return cls(role="assistant", content=parts)

    @classmethod
    def tool_response(
        cls,
        *,
        tool_call_id: str,
        name: str,
        response: str | dict[str, Any] | list[dict[str, Any]],
    ) -> Message:
        """Build a function-response message paired to a tool call.

        Gemini 3 maps results back to in-flight calls by `id`, so the
        SDK threads `tool_call_id` through to the adapter, which lands
        it in the `function_response.id` field.

        `response` accepts three shapes:

          - `str` — plain text result.
          - `dict` — structured JSON-encodable payload.
          - `list[dict]` — multimodal content parts (text + neutral
            image envelopes, same shape as `Message.content`).
            Provider adapters render each part natively:
              * Gemini → `function_response.parts[].inline_data`
              * Anthropic → `tool_result.content[]` image blocks
              * OpenAI Responses → follow-up `user` message with
                `input_image` data-URLs (the function_call_output
                field itself is text-only).
              * OpenAI-compat → same as OpenAI Responses with
                `image_url` data-URLs.
        """
        return cls(
            role="tool",
            name=name,
            tool_call_id=tool_call_id,
            content=response,
        )

    @classmethod
    def tool_response_from_result(
        cls,
        *,
        tool_call_id: str,
        name: str,
        result: ResultFrame,
    ) -> Message:
        """Build a tool-response message from a `peers.spawn(...)`
        result, automatically threading the child's
        `result.output.files` — file-store NAMES the producer chose
        to surface — as `{"file_ref": {"name": …}}` parts alongside
        `result.output.content`. The ROUTER resolves each name into
        the provider call (bytes never cross a frame), scoped to the
        caller's task.

        The names are PRODUCER-declared (`AgentOutput.files`), so the
        receiver-side tool-call loop stays generic — a newly-
        discovered agent is covered by the same call without per-tool
        branching:

            for tc in resp.tool_calls:
                child = await ctx.peers.spawn_from_tool_call(tc)
                messages.append(Message.tool_response_from_result(
                    tool_call_id=tc.id, name=tc.name, result=child,
                ))

        Behaviour:
          * No `output.files` → identical wire output to
            `tool_response(response=result.output.content)`. Plain
            text, zero multimodal-envelope cost.
          * One or more names → multimodal `response=[{"text":
            content?}, {"file_ref": {"name": n}}, ...]`. The modality
            (`image` / `document`) is inferred at the router from
            the named blob's mime type.
          * No content and no files → empty string (a valid empty
            tool result).

        For errored / cancelled results the caller branches on
        `result.status` before invoking this helper if strict-success
        semantics matter; names present on a non-`succeeded` result
        are still threaded through.
        """
        text = (result.output.content if result.output else "") or ""
        names = list(result.output.files) if result.output else []
        if not names:
            return cls.tool_response(
                tool_call_id=tool_call_id,
                name=name,
                response=text,
            )
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"text": text})
        for fname in names:
            parts.append({"file_ref": {"name": fname}})
        return cls.tool_response(
            tool_call_id=tool_call_id,
            name=name,
            response=parts,
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


ToolChoice = Literal["auto", "none", "required"] | dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    # Round-tripped on the next turn for Gemini 3 function calling
    # (mandatory on the first call of each step) — the SDK helper
    # `Message.assistant_from_response` injects this back into the
    # rebuilt assistant message.
    thought_signature: str | None = None


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Gemini reports thinking tokens separately from output for billing —
    # surfaced on Anthropic via `thinking` blocks too. Mirrors the
    # router-side `bp_router.llm.service.TokenUsage` field set so the
    # full wire payload round-trips into agent code without truncation.
    thoughts_tokens: int = 0
    # Cache accounting. Surfaced by every provider that supports
    # context caching (Anthropic prompt caching,
    # OpenAI cached_tokens, Gemini cached_content_token_count).
    # Lets agents make caching-aware decisions (e.g. "always include
    # the system block to maximise cache hit rate"). Without these
    # the SDK-side `_result_to_response` was silently discarding
    # billing-relevant signal the router had put on the wire.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Router-computed cost in micro-USD. Provider adapters that
    # know unit pricing populate this; others leave it 0.
    cost_microusd: int = 0


@dataclass
class LlmResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    # Concatenation of `part.thought=True` text on Gemini, or visible
    # `thinking` block text on Anthropic with `display="summarized"`.
    # None when thinking is off / omitted.
    thought_summary: str | None = None
    # Gemini-specific signature on the last text part — recommended
    # (not required) to round-trip. `assistant_from_response` carries
    # it through automatically. Anthropic uses `reasoning_blocks`
    # instead (see below).
    thought_signature: str | None = None
    # Provider-shaped reasoning blocks for round-tripping. On
    # Anthropic, these are the `thinking` / `redacted_thinking`
    # content blocks that MUST be returned unchanged on the next
    # assistant turn during tool use — dropping them produces a 400
    # from the upstream. On Gemini, this is empty (signatures live
    # on individual parts instead). `assistant_from_response`
    # prepends these to the rebuilt assistant turn.
    reasoning_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LlmDelta:
    text: str | None = None
    tool_call: ToolCall | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    # Streaming: True when this delta's text is a thought-summary chunk.
    thought: bool = False
    thought_signature: str | None = None
    # Provider-shaped reasoning block emitted at content-block-stop on
    # Anthropic (or end-of-block on any future provider with the same
    # shape). Agents that need round-trip support after a streaming
    # call can accumulate these into a list and synthesise an
    # `LlmResponse` for `Message.assistant_from_response`.
    reasoning_block: dict[str, Any] | None = None
    # Status hint emitted by the router during streaming setup-retry.
    # When set, every content field above is None — the delta is
    # purely a "still working, retrying after backoff" notification.
    # The SDK swallows these by default; agents subscribe via
    # `RetryPolicy.on_retry_pending`.
    meta: LlmDeltaMeta | None = None


class StreamAccumulator:
    """Folds a stream of `LlmDelta` chunks into a single `LlmResponse`.

    Multi-turn agent loops that consume the streaming API need to
    rebuild an `LlmResponse` from the deltas to:

      - Round-trip reasoning blocks via
        `Message.assistant_from_response` (Anthropic `thinking` /
        `redacted_thinking` MUST be returned unchanged on the next
        assistant turn during tool use, or the call 400s).
      - Surface tool calls + finish_reason + usage uniformly with
        the unary path.
      - Carry `thought_signature` (Gemini 3 function-calling
        requires round-tripping it on the first call of each step).

    Without this helper every agent reinvented the same fold, and
    the easy mistakes (dropping reasoning_block, dropping
    thought_signature, mishandling tool-call deltas, ignoring
    `meta` retry hints, double-counting Anthropic's cumulative
    usage) are exactly the ones that break Gemini 3 + Anthropic
    round-trips.

    Usage:

        acc = StreamAccumulator()
        async for delta in await ctx.llm.generate(..., stream=True):
            acc.add(delta)
        response = acc.build()
        # response is a fully-shaped LlmResponse, equivalent to
        # what the unary path would have returned.

    Semantics:
      - `text`: concatenation of every content `text` field where
        `thought is False`.
      - `thought_summary`: concatenation of every `text` field where
        `thought is True`. None when no thought deltas were seen.
      - `tool_calls`: every per-delta `tool_call` appended in
        arrival order. The router emits one fully-formed `ToolCall`
        per delta — provider-side incremental assembly happens in
        the adapter, not here.
      - `finish_reason`: latest non-None value (so a mid-stream
        `tool_calls` reason isn't overwritten by a final `stop`
        unless the stream actually ends with `stop`).
      - `usage`: `max()` per-field across deltas. Anthropic's
        `message_delta` reports cumulative usage; max() absorbs
        that without re-summing partials. OpenAI/Gemini emit
        usage once at the end; max() against the only-ever-once
        value is a no-op.
      - `thought_signature`: latest non-None value.
      - `reasoning_blocks`: every per-delta `reasoning_block`
        appended in arrival order.
      - `meta` deltas: silently skipped (they're status hints,
        not part of the final response).
    """

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._thought_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._reasoning_blocks: list[dict[str, Any]] = []
        self._finish_reason: str | None = None
        self._thought_signature: str | None = None
        self._usage = TokenUsage()

    def add(self, delta: LlmDelta) -> None:
        """Fold one delta. Safe to call with meta-only deltas;
        they're skipped."""
        if delta.meta is not None:
            return
        if delta.text is not None:
            if delta.thought:
                self._thought_parts.append(delta.text)
            else:
                self._text_parts.append(delta.text)
        if delta.tool_call is not None:
            self._tool_calls.append(delta.tool_call)
        if delta.reasoning_block is not None:
            self._reasoning_blocks.append(delta.reasoning_block)
        if delta.finish_reason is not None:
            self._finish_reason = delta.finish_reason
        if delta.thought_signature is not None:
            self._thought_signature = delta.thought_signature
        if delta.usage is not None:
            # max() per-field absorbs Anthropic's cumulative
            # message_delta semantics. Updates IN PLACE so we don't
            # rebuild the dataclass on every delta.
            u = delta.usage
            cur = self._usage
            self._usage = TokenUsage(
                input_tokens=max(cur.input_tokens, u.input_tokens),
                output_tokens=max(cur.output_tokens, u.output_tokens),
                thoughts_tokens=max(cur.thoughts_tokens, u.thoughts_tokens),
                cache_read_tokens=max(
                    cur.cache_read_tokens, u.cache_read_tokens
                ),
                cache_write_tokens=max(
                    cur.cache_write_tokens, u.cache_write_tokens
                ),
                cost_microusd=max(cur.cost_microusd, u.cost_microusd),
            )

    def build(self) -> LlmResponse:
        """Assemble the final `LlmResponse`. Idempotent — calling
        twice yields equivalent objects (the helper isn't reset)."""
        return LlmResponse(
            text="".join(self._text_parts),
            tool_calls=list(self._tool_calls),
            finish_reason=self._finish_reason or "stop",
            usage=self._usage,
            raw={},
            thought_summary=(
                "".join(self._thought_parts) if self._thought_parts else None
            ),
            thought_signature=self._thought_signature,
            reasoning_blocks=list(self._reasoning_blocks),
        )


class LlmCallError(RuntimeError):
    """Raised when the router returns an `LlmResultFrame` with `error` set.

    Carries the typed wire fields from `LlmResultError` so SDK retry
    policy can inspect them without re-parsing the message:

      - `code`               — `ErrorCode.LLM_*` value
      - `retriable`          — auto-derived flag from the wire frame
      - `retry_after_seconds` — provider hint for rate limits
      - `upstream_class`     — provider exception class (telemetry)
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        retriable: bool = False,
        retry_after_seconds: float | None = None,
        upstream_class: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.retry_after_seconds = retry_after_seconds
        self.upstream_class = upstream_class


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


# Sentinel default for `RetryPolicy.retry_codes`. Imported from the
# protocol layer so the SDK and router share the same source of truth.
from bp_protocol.frames import RETRIABLE_LLM_CODES as _RETRIABLE_LLM_CODES  # noqa: E402


@dataclass
class RetryPolicy:
    """SDK-level retry policy for `ctx.llm.generate / embed / count_tokens`.

    The router already retries internally per `preset.max_retries` and
    walks the fallback chain; this is an OUTER retry layer for cases
    where the entire chain has been exhausted but the agent still
    wants another shot (e.g. the upstream came back during the
    backoff). Defaults below match design doc §11.

    `total_attempts_cap` is a defensive ceiling — `max_attempts` is
    clamped at construction so a misconfigured policy can't make the
    SDK retry hundreds of times under outage.

    `on_retry_pending` is an optional callback invoked when a STREAMING
    request emits a `LlmDelta(meta={"kind": "retry_pending", ...})`
    frame. By default the SDK swallows those — agent code only sees
    content deltas. Set the callback to surface the spinner-hint
    payload to a UI.
    """

    max_attempts: int = 3
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 10.0
    backoff_multiplier: float = 2.0
    jitter: bool = True
    # Hard ceiling. `max_attempts` is clamped at construction.
    total_attempts_cap: int = 8
    # Codes the SDK retries on. Defaults to RETRIABLE_LLM_CODES from
    # the protocol; agents can broaden / narrow.
    retry_codes: frozenset[str] = field(
        default_factory=lambda: frozenset(_RETRIABLE_LLM_CODES)
    )
    # Streaming-only: invoked when a meta delta arrives from the
    # router. Default None = silently swallowed; the SDK iterator
    # only yields content deltas.
    on_retry_pending: Any | None = None  # Callable[[dict], None]

    def __post_init__(self) -> None:
        # 1. Coerce `max_attempts` to int. YAML / JSON config layers
        #    occasionally produce floats (`3.0`); without coercion,
        #    `range(self.max_attempts)` raises `TypeError` at retry
        #    time — much harder to debug than at construction.
        try:
            self.max_attempts = int(self.max_attempts)
        except (TypeError, ValueError):
            self.max_attempts = 1
        # 2. Clamp `total_attempts_cap` at >= 1. A negative or zero
        #    cap would wrong-foot the `max_attempts` clamp below.
        if not isinstance(self.total_attempts_cap, int):
            try:
                self.total_attempts_cap = int(self.total_attempts_cap)
            except (TypeError, ValueError):
                self.total_attempts_cap = 1
        if self.total_attempts_cap < 1:
            self.total_attempts_cap = 1
        # 3. Clamp `max_attempts` at the cap. A user setting
        #    `RetryPolicy(max_attempts=100)` shouldn't be able to
        #    bypass the design's worst-case bound.
        if self.max_attempts > self.total_attempts_cap:
            self.max_attempts = self.total_attempts_cap
        if self.max_attempts < 1:
            self.max_attempts = 1


def _compute_backoff(
    attempt_idx: int,
    *,
    policy: RetryPolicy,
    retry_after_seconds: float | None = None,
    rng: Any | None = None,  # `random.Random | None` — typed Any
                                # so the import stays lazy.
) -> float:
    """Same shape as `bp_router.llm.retry_classification.compute_backoff`,
    duplicated here so the SDK doesn't depend on bp_router.

    `rng` is injectable so tests can drive deterministic outputs;
    production calls leave it None and use the module-global RNG.
    Mirrors the router-side helper's signature exactly.
    """
    import random  # noqa: PLC0415

    # Defensive clamp on a misconfigured cap. `random.uniform(0, -5)`
    # returns a negative number, which `asyncio.sleep` silently
    # rounds to 0. Clamping here keeps the contract obvious.
    max_backoff_s = max(0.0, policy.max_backoff_s)
    if retry_after_seconds is not None:
        return min(max(retry_after_seconds, 0.0), max_backoff_s)
    raw = policy.initial_backoff_s * (policy.backoff_multiplier ** attempt_idx)
    capped = min(raw, max_backoff_s)
    if not policy.jitter:
        return capped
    r = rng if rng is not None else random
    return r.uniform(0.0, capped)


# ---------------------------------------------------------------------------
# Multimodal — neutral content-part helpers
# ---------------------------------------------------------------------------

# Image and document are the two modalities the SDK standardises today:
# base64 inline encoding works across every provider that supports them
# (Gemini, Anthropic, OpenAI Responses), and the binary stays inside
# the existing WS frame channel — no separate upload step.  Audio /
# video / provider-native file references stay at the agent level for
# now; they vary too much between providers to model neutrally
# without leaking quirks.
#
# Neutral schemas:
#   {"image":    {"mime_type": "...", "data": "<base64>", "display_name"?}}
#   {"document": {"mime_type": "...", "data": "<base64>", "display_name"?}}
#
# Provider adapters detect the discriminator key and rewrite natively:
#   image:
#     Gemini    → {"inline_data": {"mime_type": ..., "data": ..., "display_name"?}}
#     Anthropic → {"type": "image", "source": {"type": "base64", ...}}
#     OpenAI    → {"type": "input_image", "image_url": "data:..."}
#   document:
#     Gemini    → {"inline_data": {...}}  (same shape; MIME drives interpretation)
#     Anthropic → {"type": "document", "source": {"type": "base64", ...}}
#     OpenAI    → {"type": "input_file", "filename": ..., "file_data": "data:..."}
#
# Both helpers accept the same source forms (bytes / path) and produce
# the same envelope shape — the discriminator key is the only
# difference. Agents pick `document_part` over `image_part` to signal
# semantic intent; MIME type alone isn't always enough (Anthropic
# wants distinct `image` vs `document` block types).

ImageSource = bytes | str | os.PathLike[str]
DocumentSource = bytes | str | os.PathLike[str]


def image_part(
    source: ImageSource,
    *,
    mime_type: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Build a neutral image content-part for an LLM message.

    Use inside a multi-part `Message.content` list:

        await ctx.llm.generate([
            Message(role="user", content=[
                {"text": "What is in this picture?"},
                image_part("photo.jpg"),
            ]),
        ])

    Symmetric on the tool-result side — neutral image parts pack
    inside a `Message.tool_response(response=[...])` list and each
    provider adapter renders them natively (Gemini
    `function_response.parts[].inline_data`, Anthropic `tool_result`
    image blocks, OpenAI follow-up `input_image`).

    Sources:
      - `bytes` — raw image bytes; `mime_type` required.
      - `str` / `os.PathLike` — file path; `mime_type` inferred from
        the extension via `mimetypes.guess_type`. Pass `mime_type=`
        explicitly when the extension is missing or wrong.

    `display_name` is an optional human-readable label carried
    through to the provider when supported (Gemini surfaces it on
    `inline_data.display_name`; the others ignore it). Defaults to
    the file basename when `source` is a path.

    The image is base64-inlined (≈+33% over the raw bytes) into the
    WS frame. A frame over the router's `max_payload_bytes` cap
    (1 MiB default) is rejected — when such a part rides in a
    spawn/delegate payload the SDK raises `FrameTooLargeError`
    before send. For large media, send it out-of-band instead:
    `ctx.files.put()` and pass the reference through the task's
    attachments (see `docs/backplaned/sdk/core.md`), or use a provider-native
    upload (Gemini File API, Anthropic Files) and pass that
    reference part directly in `content`.
    """
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if mime_type is None:
            guess, _ = mimetypes.guess_type(path.name)
            if guess is None:
                raise ValueError(
                    f"could not infer mime_type for {path.name!r}; "
                    "pass mime_type=... explicitly"
                )
            mime_type = guess
        data = path.read_bytes()
        if display_name is None:
            display_name = path.name
    elif isinstance(source, (bytes, bytearray)):
        if mime_type is None:
            raise ValueError("mime_type is required when source is bytes")
        data = bytes(source)
    else:
        raise TypeError(
            f"image source must be bytes, str, or PathLike; got {type(source).__name__}"
        )

    image: dict[str, Any] = {
        "mime_type": mime_type,
        "data": base64.b64encode(data).decode("ascii"),
    }
    if display_name is not None:
        image["display_name"] = display_name
    return {"image": image}


def document_part(
    source: DocumentSource,
    *,
    mime_type: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Build a neutral document content-part for an LLM message.

    Symmetric to `image_part` for document-typed binaries (PDFs,
    plain text, etc.). Use inside a multi-part `Message.content`
    list, or inside a `Message.tool_response(response=[...])` list
    to return document content from a tool call (Gemini 3+
    multimodal function responses, Anthropic document blocks,
    OpenAI Responses `input_file`).

        await ctx.llm.generate([
            Message(role="user", content=[
                {"text": "Summarise this contract."},
                document_part("contract.pdf"),
            ]),
        ])

    Sources:
      - `bytes` — raw document bytes; `mime_type` required.
      - `str` / `os.PathLike` — file path; `mime_type` inferred from
        the extension via `mimetypes.guess_type`. Pass `mime_type=`
        explicitly when the extension is missing or wrong.

    `display_name` is an optional human-readable label carried
    through to the provider when supported. Gemini 3+ surfaces it
    on `inline_data.display_name` for `{"$ref": "<name>"}`
    substitution inside structured `function_response` payloads;
    OpenAI uses it as `input_file.filename`; Anthropic ignores it.
    Defaults to the file basename when `source` is a path.

    Why a separate `document_part` rather than reusing `image_part`?
    Anthropic's API has distinct `image` vs. `document` block
    types — overloading one envelope to mean both would force a
    MIME-type sniff on every adapter. The discriminator key
    (`"image"` vs `"document"`) is a semantic signal from the
    agent.

    Provider support varies — the SDK accepts any MIME the caller
    specifies and lets providers reject what they can't handle.
    Gemini 3+ enumerates `application/pdf` and `text/plain` for
    multimodal function responses; Anthropic supports
    `application/pdf` in document blocks; OpenAI Responses
    accepts `application/pdf` via `input_file`. Older OpenAI Chat
    Completions deployments will 400 on the `input_file` shape.

    Like `image_part`, the document is base64-inlined (≈+33%) into
    the WS frame; one riding a spawn/delegate payload over the
    router's `max_payload_bytes` cap raises `FrameTooLargeError`
    before send. Send large documents out-of-band via
    `ctx.files.put()` attachments (see `docs/backplaned/sdk/core.md`).
    """
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if mime_type is None:
            guess, _ = mimetypes.guess_type(path.name)
            if guess is None:
                raise ValueError(
                    f"could not infer mime_type for {path.name!r}; "
                    "pass mime_type=... explicitly"
                )
            mime_type = guess
        data = path.read_bytes()
        if display_name is None:
            display_name = path.name
    elif isinstance(source, (bytes, bytearray)):
        if mime_type is None:
            raise ValueError("mime_type is required when source is bytes")
        data = bytes(source)
    else:
        raise TypeError(
            f"document source must be bytes, str, or PathLike; got {type(source).__name__}"
        )

    document: dict[str, Any] = {
        "mime_type": mime_type,
        "data": base64.b64encode(data).decode("ascii"),
    }
    if display_name is not None:
        document["display_name"] = display_name
    return {"document": document}


# NOTE: referencing a router-managed stash file in an LLM message is
# `ctx.files.llm_ref(name)` (→ `{"file_ref": {"name": …}}`) — the
# router resolves the name into the provider call. There is no inline
# file-content helper here; bytes never cross the agent→router frame.


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Sentinel pushed to a streaming queue when cancel_token trips so the
# awaiting `queue.get()` unblocks with a real value (avoids racing two
# coroutines and leaving the unscheduled one warning at GC).
_CANCEL_SENTINEL = object()

# Upper bound on a single LLM stream's delta queue. A draining
# consumer never approaches this (it dequeues far faster than the
# network delivers); hitting it means the stream was abandoned and
# `_handle_llm_delta` drops + warns instead of growing memory
# unboundedly. Generous so legitimate burst-then-drain never trips.
_LLM_STREAM_QUEUE_MAX = 1024

# `embed()` auto-split tuning. An embedding result rides INLINE in the
# LlmResultFrame (`vectors: list[list[float]]`); a JSON double is ~21 bytes
# (measured ~20.8 for arbitrary doubles, rounded up). `embed` sizes each
# sub-request so its result frame stays under a fraction of the negotiated
# payload cap — the binding constraint, since a 100×1536-d result is ~3 MiB
# (over the ~1 MiB cap → the router can't deliver it → the caller hangs).
_DEFAULT_PAYLOAD_CAP = 1_048_576  # mirrors the router's default max_payload_bytes
_EMBED_FLOAT_BYTES = 22
_EMBED_FRAME_BUDGET_FRACTION = 0.6  # leave headroom for the frame envelope
_EMBED_MAX_ASSUMED_DIM = 4096  # worst-case dim for sizing the FIRST batch
_EMBED_INPUT_OVERHEAD = 8  # per-input JSON overhead (quotes/comma/escaping)


class LlmServiceClient:
    """Per-task LLM facade. Routes calls over the agent's WebSocket.

    Lifetime is the task; constructed by the dispatcher. The dispatcher
    routes incoming `LlmDelta` and `LlmResult` frames to the right
    pending future / streaming queue keyed on `correlation_id`.
    """

    def __init__(self, ctx: TaskContext, dispatcher: Dispatcher) -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher

    @property
    def _agent_id(self) -> str:
        return self._dispatcher.agent.info.agent_id

    @property
    def _trace_id(self) -> str:
        return self._ctx.trace_id

    @property
    def _span_id(self) -> str:
        return self._ctx.span_id

    # ------------------------------------------------------------------
    # Cancel-aware await helpers
    # ------------------------------------------------------------------

    async def _send_abort(self, request: LlmRequestFrame) -> None:
        """Tell the router to cancel a specific LLM call."""
        cancel = CancelFrame(
            agent_id=self._agent_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
            task_id=None,
            ref_correlation_id=request.correlation_id,
            reason=self._ctx.cancel_token.reason or "cancelled",
        )
        try:
            await self._dispatcher.transport.send(cancel)
        except Exception:  # noqa: BLE001
            # Cancellation is best-effort — handler is about to bail anyway.
            pass

    async def _await_with_cancel_future(
        self,
        fut: asyncio.Future,
        request: LlmRequestFrame,
    ) -> Any:
        """Await a Future from PendingMap while watching cancel_token.

        On cancel, reject the future so the await unblocks, send abort,
        raise CancellationError. The PendingMap exception path means the
        late-arriving result (if any) is discarded.
        """
        if self._ctx.cancel_token.cancelled:
            await self._send_abort(request)
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")

        async def _watch() -> None:
            await self._ctx.cancel_token.wait()
            if not fut.done():
                fut.set_exception(CancellationError(
                    self._ctx.cancel_token.reason or "cancelled"
                ))

        watcher = asyncio.create_task(_watch())
        try:
            return await fut
        except CancellationError:
            await self._send_abort(request)
            raise
        finally:
            watcher.cancel()
            try:
                await watcher
            except BaseException:  # noqa: BLE001
                pass

    async def _queue_get_or_cancel(
        self,
        queue: asyncio.Queue,
        request: LlmRequestFrame,
    ) -> Any:
        """Pull the next item from `queue` while watching cancel_token.

        On cancel, push a sentinel onto the queue so the get() unblocks
        cleanly with a real value (rather than racing two coroutines
        and leaving one unstarted), send abort, raise CancellationError.
        """
        if self._ctx.cancel_token.cancelled:
            await self._send_abort(request)
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")

        sentinel = _CANCEL_SENTINEL

        async def _watch() -> None:
            await self._ctx.cancel_token.wait()
            queue.put_nowait(sentinel)

        watcher = asyncio.create_task(_watch())
        try:
            item = await queue.get()
            if item is sentinel:
                await self._send_abort(request)
                raise CancellationError(
                    self._ctx.cancel_token.reason or "cancelled"
                )
            return item
        finally:
            watcher.cancel()
            try:
                await watcher
            except BaseException:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Retry orchestration
    # ------------------------------------------------------------------

    async def _run_unary_once(self, request: LlmRequestFrame) -> LlmResultFrame:
        """Issue one non-streaming request, await one terminal result,
        translate any error into `LlmCallError` carrying typed fields.

        Each retry attempt should pass a FRESH `request` (new
        correlation_id) so the dispatcher's pending-results map can
        register a clean future without colliding with the prior
        attempt's late-arriving frame.
        """
        # Routed through `register_for_task` so the future is rejected
        # immediately if the calling handler exits before the LLM
        # response arrives — instead of waiting out
        # `correlation_timeout`. Outside a handler context
        # (`task_id == "<spawn>"`) this falls back to plain
        # `pending_results.register`.
        fut = self._dispatcher.register_for_task(
            self._dispatcher.pending_results,
            request.correlation_id,
            self._ctx.task_id,
        )
        await self._dispatcher.transport.send(request)
        try:
            result: LlmResultFrame = await self._await_with_cancel_future(
                fut, request
            )
        except CancellationError:
            self._dispatcher.pending_results.reject(
                request.correlation_id, CancellationError("cancelled")
            )
            raise
        except TimeoutError as exc:
            raise LlmCallError("LLM request timed out") from exc
        _raise_for_error(result)
        return result

    async def _run_with_retry(
        self,
        make_request: Any,
        retry: RetryPolicy | None,
    ) -> LlmResultFrame:
        """Drive `_run_unary_once` under a `RetryPolicy`.

        `make_request` is a zero-arg callable returning a fresh
        `LlmRequestFrame` (new correlation_id) on each call.

        Retry rules:
          - Stop immediately on a non-retriable `LlmCallError`.code or
            when the code isn't in `policy.retry_codes`.
          - Stop after `policy.max_attempts` total attempts.
          - Honour `LlmCallError.retry_after_seconds` for the inter-
            attempt sleep when set; otherwise apply exponential
            backoff per `policy`.
          - The router has its own retry loop inside
            `_call_with_fallback`, so an SDK retry is an OUTER layer.
            `total_attempts_cap` clamps `max_attempts` at construction
            to keep the SDK × router multiplication bounded.

        With `retry=None` (the default), behaviour is identical to
        a direct `_run_unary_once` call — single attempt, no sleep,
        first error wins.
        """
        if retry is None:
            return await self._run_unary_once(make_request())

        last_exc: LlmCallError | None = None
        for attempt_idx in range(retry.max_attempts):
            try:
                return await self._run_unary_once(make_request())
            except LlmCallError as exc:
                if not exc.retriable or exc.code not in retry.retry_codes:
                    raise
                last_exc = exc
                # Last attempt — surface the error as-is rather than
                # sleeping pointlessly.
                if attempt_idx == retry.max_attempts - 1:
                    raise
                wait = _compute_backoff(
                    attempt_idx,
                    policy=retry,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                # Cancel-aware sleep: a Cancel frame mid-backoff should
                # abort the retry loop instead of waiting out the wait.
                await self._sleep_or_cancel(wait)
        # Unreachable — the loop either returns or re-raises on the
        # final attempt — but mypy / future refactors appreciate the
        # explicit fallback.
        assert last_exc is not None
        raise last_exc

    async def _sleep_or_cancel(self, seconds: float) -> None:
        """Sleep up to `seconds`, but wake immediately on cancel_token
        and raise `CancellationError`. Avoids a multi-second backoff
        outliving an aborted task."""
        if seconds <= 0:
            if self._ctx.cancel_token.cancelled:
                raise CancellationError(
                    self._ctx.cancel_token.reason or "cancelled"
                )
            return
        sleep_task = asyncio.create_task(asyncio.sleep(seconds))
        wait_task = asyncio.create_task(self._ctx.cancel_token.wait())
        try:
            done, _ = await asyncio.wait(
                {sleep_task, wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task in done:
                raise CancellationError(
                    self._ctx.cancel_token.reason or "cancelled"
                )
        finally:
            for t in (sleep_task, wait_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except BaseException:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str | list[Message],
        *,
        preset: str | None = None,
        model: str = "default",
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        provider_options: dict[str, Any] | None = None,
        retry: RetryPolicy | None = None,
    ) -> LlmResponse | AsyncIterator[LlmDelta]:
        """Issue an LLM request via the router.

        `max_tokens` caveat for thinking models: on Gemini 2.5+ and
        Anthropic Claude with extended thinking, `max_tokens` is the
        TOTAL budget the model splits between hidden thoughts and
        visible output. A small cap (e.g. 256) gets eaten almost
        entirely by thoughts on creative prompts and the visible
        answer is truncated to a handful of tokens with
        `finish_reason="length"`. If you don't have a specific reason
        to cap, leave `max_tokens=None` and let the provider's own
        default apply; surface `response.usage.thoughts_tokens`
        alongside `output_tokens` if you need to see the split.
        Surfaced by the examples test drive's first real Gemini call.
        """
        if self._ctx.cancel_token.cancelled:
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")
        if isinstance(prompt, str):
            messages = [Message(role="user", content=prompt)]
        else:
            messages = prompt

        # Default to RetryPolicy() (3 attempts, exp backoff, Retry-After
        # honoured) instead of single-shot. A bare `503 high demand` from
        # Gemini otherwise took down a whole turn. Callers wanting the
        # old single-shot behaviour can pass `RetryPolicy(max_attempts=1)`
        # explicitly.
        if retry is None:
            retry = RetryPolicy()

        # Builder closure: each retry attempt needs a fresh
        # correlation_id. The frame is otherwise identical across
        # attempts — model, preset, messages, tools all unchanged.
        def _build_request() -> LlmRequestFrame:
            return LlmRequestFrame(
                agent_id=self._agent_id,
                trace_id=self._trace_id,
                span_id=self._span_id,
                kind="generate",
                model=model,
                preset=preset,
                messages=[m.model_dump() for m in messages],
                tools=[t.model_dump() for t in tools] if tools else [],
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                provider_options=provider_options,
                user_id=self._ctx.user_id,
                task_id=(
                    self._ctx.task_id
                    if self._ctx.task_id != "<spawn>"
                    else None
                ),
            )

        if stream:
            # Streaming retry is bounded to PRE-FIRST-DELTA per design
            # doc §7. `_stream_with_retry` handles that boundary plus
            # `delta.meta` swallowing / `on_retry_pending` callback.
            return self._stream_with_retry(_build_request, retry)

        result = await self._run_with_retry(_build_request, retry)
        return _result_to_response(result)

    async def _stream_one_attempt(
        self,
        request: LlmRequestFrame,
        *,
        on_retry_pending: Any | None,
    ) -> AsyncIterator[tuple[bool, Any]]:
        """Yield `(first_delta_yielded_so_far, item)` for one streaming
        attempt of `request`.

        `item` is one of:
          - `LlmDelta` with content fields populated (a real chunk)
          - sentinel `None` returned alongside the terminal envelope
            so the caller can decide whether to stop / re-issue
        Raises `LlmCallError` when the router returns a terminal
        `LlmResultFrame.error`. The caller decides whether the error
        is pre- or post-first-delta and handles retry accordingly.

        Meta deltas are intercepted here: when `on_retry_pending` is
        set, the callback fires with the `LlmDeltaMeta` payload;
        otherwise the meta delta is silently swallowed. Either way,
        meta deltas do NOT count as "first delta" for the retry
        boundary — they're status hints, not content.

        Async callbacks (`async def on_retry_pending(meta): ...`) are
        also supported — the coroutine returned from the call is
        awaited inline. This avoids a hard-to-debug silent-no-op
        when an agent passes an `async def` by accident.

        Cleanup contract: when the agent breaks out of `async for d
        in gen:` mid-iteration without reaching the router's terminal
        frame, this generator's `finally` sends a `CancelFrame` so
        the router stops producing deltas to a queue that's about
        to be torn down.
        """
        # Bounded so an ABANDONED stream (handler broke out of the
        # `async for` before this generator's `finally` popped the
        # queue) can't grow one delta per inbound frame for the rest
        # of the recv loop's life. A live consumer drains FIFO far
        # faster than the network delivers, so a healthy stream
        # never approaches this; hitting it means nobody is draining
        # and `_handle_llm_delta` drops + warns (mirrors the
        # deliberate best-effort design of the Progress path).
        queue: asyncio.Queue = asyncio.Queue(maxsize=_LLM_STREAM_QUEUE_MAX)
        self._dispatcher._llm_streams[request.correlation_id] = queue
        # Tracks whether the iterator reached one of the router's
        # terminal frames (success `LlmResultFrame` or error
        # `LlmResultFrame`). When False at finally time the router
        # didn't terminate naturally — we send Cancel to stop the
        # dangling stream.
        terminator_received = False
        first_delta_seen = False
        try:
            await self._dispatcher.transport.send(request)
            while True:
                # Race the next item against cancellation. When the
                # cancel_token trips, `_queue_get_or_cancel` itself
                # sends the Cancel frame and raises CancellationError;
                # we mark `terminator_received=True` so the finally
                # below doesn't double-send.
                try:
                    item = await self._queue_get_or_cancel(queue, request)
                except CancellationError:
                    terminator_received = True
                    raise
                if isinstance(item, LlmResultFrame):
                    # The router's terminal envelope; no more frames
                    # are coming. Set the flag BEFORE `_raise_for_error`
                    # so the finally treats an error-result as a clean
                    # router-side termination, not a user abort.
                    terminator_received = True
                    _raise_for_error(item)
                    return
                # `item` is now an `LlmDelta` (already translated by
                # the dispatcher). Check for meta hints first.
                if isinstance(item, LlmDelta) and item.meta is not None:
                    if on_retry_pending is not None:
                        try:
                            result = on_retry_pending(item.meta)
                            # Support both sync and async callbacks.
                            # Without the await, an `async def` callback
                            # leaves a never-awaited coroutine behind
                            # (RuntimeWarning at GC; no UI hint shown).
                            if inspect.iscoroutine(result):
                                await result
                        except Exception:  # noqa: BLE001
                            # Don't let a misbehaving UI callback bring
                            # down the stream. Log and continue.
                            logger.exception(
                                "RetryPolicy.on_retry_pending callback raised"
                            )
                    # Meta deltas are status hints; don't yield to the
                    # agent and don't flip first_delta_seen.
                    continue
                first_delta_seen = True
                yield first_delta_seen, item
        finally:
            self._dispatcher._llm_streams.pop(request.correlation_id, None)
            if not terminator_received:
                # Agent broke out of the iterator mid-stream (or some
                # other non-router exception unwound us). Best-effort
                # `CancelFrame` so the router stops pushing deltas
                # into a queue we just popped. `_send_abort` swallows
                # transport errors — we don't want a finally to mask
                # the original exception.
                await self._send_abort(request)

    async def _stream_with_retry(
        self,
        make_request: Any,
        retry: RetryPolicy | None,
    ) -> AsyncIterator[LlmDelta]:
        """Streaming generator with bounded pre-first-delta retry.

        Per design doc §7, streaming retry on the SDK side is allowed
        ONLY before the first content delta has been yielded. Once
        the agent has seen a chunk, a downstream failure is
        `stream_interrupted` (NOT retriable) — re-issuing would mean
        a second prefix the agent has to splice manually.

        With `retry=None` we make exactly one attempt and surface any
        error as-is.

        Meta deltas (router-side setup-retry hints) are handled by
        `_stream_one_attempt` and never reach the agent unless
        `retry.on_retry_pending` is set.
        """
        callback = retry.on_retry_pending if retry is not None else None
        max_attempts = retry.max_attempts if retry is not None else 1
        retry_codes = retry.retry_codes if retry is not None else frozenset()

        first_delta_yielded = False
        for attempt_idx in range(max_attempts):
            request = make_request()
            try:
                async for first_seen, delta in self._stream_one_attempt(
                    request, on_retry_pending=callback
                ):
                    first_delta_yielded = first_seen
                    yield delta
                # Stream completed cleanly — no more attempts.
                return
            except LlmCallError as exc:
                # Once any content delta has been delivered, we can't
                # safely re-issue. Surface the error as-is — this is
                # the `stream_interrupted` boundary. NOTE: the router
                # will have classified this as `stream_interrupted` /
                # `internal_error`; the SDK doesn't override.
                if first_delta_yielded:
                    raise
                if retry is None:
                    raise
                if not exc.retriable or exc.code not in retry_codes:
                    raise
                if attempt_idx == max_attempts - 1:
                    raise
                wait = _compute_backoff(
                    attempt_idx,
                    policy=retry,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                await self._sleep_or_cancel(wait)

    # ------------------------------------------------------------------
    # embed
    # ------------------------------------------------------------------

    def _payload_cap(self) -> int:
        """Router-negotiated WS frame cap in bytes (`WelcomeFrame.
        max_payload_bytes`), falling back to the router default when no
        Welcome is bound (in-proc transport / pre-handshake)."""
        transport = getattr(self._dispatcher, "transport", None)
        welcome = getattr(transport, "welcome", None)
        cap = getattr(welcome, "max_payload_bytes", None)
        return cap if isinstance(cap, int) and cap > 0 else _DEFAULT_PAYLOAD_CAP

    async def _embed_once(
        self,
        text_list: list[str],
        *,
        preset: str | None,
        model: str,
        retry: RetryPolicy,
    ) -> list[list[float]]:
        def _build_request() -> LlmRequestFrame:
            return LlmRequestFrame(
                agent_id=self._agent_id,
                trace_id=self._trace_id,
                span_id=self._span_id,
                kind="embed",
                model=model,
                preset=preset,
                text=text_list,
                user_id=self._ctx.user_id,
            )

        result = await self._run_with_retry(_build_request, retry)
        return result.vectors

    async def embed(
        self,
        text: str | list[str],
        *,
        preset: str | None = None,
        # `default` would route to the chat preset (Gemini), which
        # raises NotImplementedError on `embed()`. The embeddings-shaped
        # default points at the canonical embedding preset so an SDK
        # call with no explicit name "just works".
        model: str = "text-embedding-3-small",
        retry: RetryPolicy | None = None,
    ) -> list[list[float]]:
        """Embed one or many texts. A large input list is AUTO-SPLIT into
        sub-requests so neither the request frame (input texts) nor the result
        frame (vectors, which ride INLINE) exceeds the negotiated WS payload
        cap. The result frame is the binding constraint: a vector is ≈ `dim`
        floats × ~21 bytes, so e.g. 100 × 1536-d vectors ≈ 3 MiB — over the
        ~1 MiB cap, which the router can't deliver, hanging the caller. Vectors
        are returned in input order; a small call that already fits is a single
        request (unchanged)."""
        if self._ctx.cancel_token.cancelled:
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")
        if isinstance(text, str):
            text_list = [text]
        else:
            text_list = list(text)
        if not text_list:
            return []
        if retry is None:
            retry = RetryPolicy()

        budget = int(self._payload_cap() * _EMBED_FRAME_BUDGET_FRACTION)
        out: list[list[float]] = []
        # Result-side count cap. The embedding dim is unknown until the first
        # response, so the FIRST batch assumes a worst-case dim (its result is
        # guaranteed to fit); subsequent batches use the dim we actually saw.
        max_by_result = max(1, budget // (_EMBED_MAX_ASSUMED_DIM * _EMBED_FLOAT_BYTES))
        i, n = 0, len(text_list)
        while i < n:
            # Also bound the REQUEST frame by accumulated input-text bytes.
            sub: list[str] = []
            req_bytes = 0
            while i < n and len(sub) < max_by_result:
                tb = len(text_list[i].encode("utf-8")) + _EMBED_INPUT_OVERHEAD
                if sub and req_bytes + tb > budget:
                    break  # this input would overflow the request frame
                sub.append(text_list[i])
                req_bytes += tb
                i += 1
            vectors = await self._embed_once(
                sub, preset=preset, model=model, retry=retry
            )
            out.extend(vectors)
            if vectors:
                dim = len(vectors[0]) or 1
                max_by_result = max(1, budget // (dim * _EMBED_FLOAT_BYTES))
        return out

    # ------------------------------------------------------------------
    # count_tokens
    # ------------------------------------------------------------------

    async def count_tokens(
        self,
        prompt: str | list[Message],
        *,
        preset: str | None = None,
        model: str = "default",
        retry: RetryPolicy | None = None,
    ) -> int:
        if self._ctx.cancel_token.cancelled:
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")
        if isinstance(prompt, str):
            messages = [Message(role="user", content=prompt).model_dump()]
        else:
            messages = [m.model_dump() for m in prompt]

        if retry is None:
            retry = RetryPolicy()

        def _build_request() -> LlmRequestFrame:
            return LlmRequestFrame(
                agent_id=self._agent_id,
                trace_id=self._trace_id,
                span_id=self._span_id,
                kind="count_tokens",
                model=model,
                preset=preset,
                messages=messages,
            )

        result = await self._run_with_retry(_build_request, retry)
        return result.total_tokens

    async def aclose(self) -> None:
        # Frame channel doesn't own a connection; nothing to close.
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raise_for_error(result: LlmResultFrame) -> None:
    """Raise `LlmCallError` carrying the typed wire fields when the
    router returned an error frame. No-op on success.

    Centralizes the dict-vs-typed-model translation so the three
    method bodies (generate / embed / count_tokens) all see the
    same `.code` / `.retriable` / `.retry_after_seconds` /
    `.upstream_class` surface — the SDK retry loop reads those
    flags directly without re-parsing strings.
    """
    err = result.error
    if err is None:
        return
    raise LlmCallError(
        f"{err.code}: {err.message}",
        code=err.code,
        # `retriable` is auto-derived by `LlmResultError._derive_retriable`
        # so it's never None on the wire by the time we read it; the
        # `or False` guard keeps the SDK robust against a future model
        # change that loosens the validator.
        retriable=bool(err.retriable),
        retry_after_seconds=err.retry_after_seconds,
        upstream_class=err.upstream_class,
    )


def _result_to_response(result: LlmResultFrame) -> LlmResponse:
    _raise_for_error(result)
    return LlmResponse(
        text=result.text,
        tool_calls=[
            ToolCall(
                id=tc["id"],
                name=tc["name"],
                args=tc.get("args", {}),
                thought_signature=tc.get("thought_signature"),
            )
            for tc in result.tool_calls
        ],
        finish_reason=result.finish_reason,
        usage=TokenUsage(
            input_tokens=result.usage.get("input_tokens", 0),
            output_tokens=result.usage.get("output_tokens", 0),
            thoughts_tokens=result.usage.get("thoughts_tokens", 0),
            # Mirror the router-side wire shape end-to-end.
            # The router started putting these fields on the wire
            # for streaming results;
            # without reading them here, agent code couldn't make
            # caching-aware decisions.
            cache_read_tokens=result.usage.get("cache_read_tokens", 0),
            cache_write_tokens=result.usage.get("cache_write_tokens", 0),
            cost_microusd=result.usage.get("cost_microusd", 0),
        ),
        raw=result.raw,
        thought_summary=result.thought_summary,
        thought_signature=result.thought_signature,
        reasoning_blocks=list(result.reasoning_blocks),
    )


def _frame_delta_to_delta(frame: LlmDeltaFrame) -> LlmDelta:
    tool_call = None
    if frame.tool_call:
        tc = frame.tool_call
        tool_call = ToolCall(
            id=tc["id"],
            name=tc["name"],
            args=tc.get("args", {}),
            thought_signature=tc.get("thought_signature"),
        )
    usage = None
    if frame.usage:
        usage = TokenUsage(
            input_tokens=frame.usage.get("input_tokens", 0),
            output_tokens=frame.usage.get("output_tokens", 0),
            thoughts_tokens=frame.usage.get("thoughts_tokens", 0),
            cache_read_tokens=frame.usage.get("cache_read_tokens", 0),
            cache_write_tokens=frame.usage.get("cache_write_tokens", 0),
            cost_microusd=frame.usage.get("cost_microusd", 0),
        )
    return LlmDelta(
        text=frame.text,
        tool_call=tool_call,
        finish_reason=frame.finish_reason,
        usage=usage,
        thought=frame.thought,
        thought_signature=frame.thought_signature,
        reasoning_block=frame.reasoning_block,
        meta=frame.meta,
    )
