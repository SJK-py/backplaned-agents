"""bp_router.llm.providers.anthropic — Anthropic provider adapter.

Wraps the `anthropic` Python SDK (deferred import). Translates neutral
`Message` to Anthropic's content-block shape; honours `provider_options`
for native features (web search, code execution, prompt caching).

Streaming and extended-thinking round-trip are NOT yet wired — the
agent-facing surface for those will land when the corresponding doc
sections arrive. `embed` is unsupported (Anthropic recommends Voyage AI
for embeddings); `count_tokens` is a stub.

Key shape differences from Gemini that the adapter bridges:

  - **System** is a top-level kwarg on `messages.create`, not a message
    role. Strip and concatenate any `role="system"` messages and pass
    via `system=`.
  - **Content blocks** (`text` / `image` / `tool_use` / `tool_result`)
    are the wire format — different from Gemini's part dicts. Translate
    each.
  - **Parallel tool results** MUST be in a single user message, with
    all `tool_result` blocks first and any text after. We accumulate
    consecutive `role="tool"` messages and emit one user message.
  - **Tool definitions** use `input_schema` (not `parameters`) and tool
    calls use `input` (not `args`).
  - **`tool_use_id`** matches `tool_use.id`; `is_error: true` signals a
    failed tool execution.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from bp_protocol.frames import ErrorCode
from bp_router.llm.providers.base import ProviderAdapter
from bp_router.llm.retry_classification import RetryHint
from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolSpec,
)

logger = logging.getLogger(__name__)


def _anthropic_retry_after(exc: BaseException) -> float | None:
    """Extract `Retry-After` from an Anthropic `RateLimitError`.

    Same shape as the OpenAI SDK (both wrap httpx). Delegated to the
    shared `parse_http_retry_after` helper, which handles both
    delta-seconds and HTTP-date forms."""
    from bp_router.llm.retry_classification import parse_http_retry_after  # noqa: PLC0415

    return parse_http_retry_after(exc)


# Anthropic's `messages.create` requires `max_tokens`. Pick a default
# tall enough for typical responses but not so tall that it eats budget.
_DEFAULT_MAX_TOKENS = 4096


# Top-level Anthropic kwargs that pass through `provider_options`
# unchanged. Other native features (e.g. web_search server tool) come
# in via `provider_options["tools"]`.
_PASSTHROUGH_KWARGS = (
    "metadata",
    "stop_sequences",
    "top_p",
    "top_k",
    "thinking",          # extended / adaptive thinking config
    "output_config",     # adaptive-thinking effort guidance, etc.
    "container",         # for code execution server tool
    "service_tier",
    "betas",             # opt-in beta features
)


# Thinking-block types that must round-trip through the assistant
# turn to keep multi-turn tool use working. `redacted_thinking`
# blocks are easy to silently drop (filtering by `type=="thinking"`
# alone misses them) — surface them on `LlmResponse.reasoning_blocks`
# so the SDK helper carries them back unchanged.
_REASONING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def _is_thinking_enabled(provider_options: dict[str, Any] | None) -> bool:
    """True iff the request opts into extended or adaptive thinking.

    Used to gate features that Anthropic rejects when thinking is on
    (forced tool choice, response prefill, temperature/top_k tweaks).
    """
    if not provider_options:
        return False
    cfg = provider_options.get("thinking")
    if not isinstance(cfg, dict):
        return False
    t = cfg.get("type")
    return t in {"enabled", "adaptive"}


# ---------------------------------------------------------------------------
# Stop reason mapping
# ---------------------------------------------------------------------------


_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",       # extended-thinking pause; treated as stop here
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}


def _map_stop_reason(stop_reason: str | None) -> str:
    if not stop_reason:
        return "stop"
    return _STOP_REASON_MAP.get(stop_reason, "stop")


def _usage_from_anthropic(usage_meta: Any) -> TokenUsage:
    """Build a neutral TokenUsage from an Anthropic `usage` object.

    Used in both non-streaming `_convert_response` and the streaming
    event handler. Anthropic's `message_delta.usage` is cumulative
    (per the docs) — `max(...)` aggregation in the dispatch loop
    handles that correctly.
    """
    return TokenUsage(
        input_tokens=int(getattr(usage_meta, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage_meta, "output_tokens", 0) or 0),
        cache_read_tokens=int(
            getattr(usage_meta, "cache_read_input_tokens", 0) or 0
        ),
        cache_write_tokens=int(
            getattr(usage_meta, "cache_creation_input_tokens", 0) or 0
        ),
    )


# ---------------------------------------------------------------------------
# Part / block translation
# ---------------------------------------------------------------------------


def _convert_part(part: dict[str, Any]) -> dict[str, Any]:
    """Translate one neutral content-part to Anthropic's block schema.

    The SDK round-trips assistant turns using neutral keys produced by
    `Message.assistant_from_response`; we rewrite those to Anthropic's
    `type`-tagged blocks. Native Anthropic blocks (already shaped
    `{"type": "...", ...}`) pass through.

    `thought_signature` (Gemini-only) is dropped — Anthropic uses
    `thinking` content blocks for the same purpose.
    """
    if not isinstance(part, dict):
        return part

    # Already-native Anthropic block — pass through unchanged. Includes
    # `text`, `image`, `tool_use`, `tool_result`, AND `thinking` /
    # `redacted_thinking` blocks that the agent round-tripped from a
    # previous LlmResponse.reasoning_blocks list. Drop the Gemini-only
    # thought_signature companion if present.
    if "type" in part:
        out = dict(part)
        out.pop("thought_signature", None)
        return out

    # Neutral image → Anthropic image block.
    if "image" in part:
        img = part["image"]
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("mime_type", "application/octet-stream"),
                "data": img.get("data", ""),
            },
        }

    # Neutral document → Anthropic document block. Distinct from
    # image because Anthropic's API gates each on a different
    # block type — PDFs go through `type: document`, not
    # `type: image`. `display_name` is dropped (Anthropic has no
    # corresponding surface).
    if "document" in part:
        doc = part["document"]
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": doc.get("mime_type", "application/octet-stream"),
                "data": doc.get("data", ""),
            },
        }

    # Gemini-flavoured text part → Anthropic text block.
    if "text" in part:
        return {"type": "text", "text": part["text"]}

    # Gemini-flavoured function_call → Anthropic tool_use block.
    if "function_call" in part:
        fc = part["function_call"]
        return {
            "type": "tool_use",
            "id": fc.get("id", ""),
            "name": fc.get("name", ""),
            "input": fc.get("args") or fc.get("input") or {},
        }

    # Unknown — pass through and let the upstream surface the error.
    return part


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def _tool_result_block(m: Message) -> dict[str, Any]:
    """Build one tool_result content block from a `role="tool"` message.

    Per Anthropic docs the `content` field accepts a string OR a list
    of nested content blocks (text / image / document). For list
    content, run each part through `_convert_part` so neutral
    `{"image": ...}` and `{"text": ...}` envelopes (as produced by
    the SDK's `image_part()` helper) get rewritten into Anthropic's
    `{"type": "image", "source": ...}` / `{"type": "text", ...}`
    blocks. Already-native blocks pass through.
    """
    out: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": m.tool_call_id or "",
    }
    if isinstance(m.content, str):
        out["content"] = m.content
    elif isinstance(m.content, list):
        out["content"] = [_convert_part(p) for p in m.content]
    else:
        out["content"] = json.dumps(m.content)
    return out


def _convert_messages(
    messages: list[Message],
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]] | None]:
    """Translate neutral Messages → (Anthropic messages, system).

    System messages get extracted and concatenated into the top-level
    `system` kwarg. Consecutive `role="tool"` messages are merged into
    a single user message with one `tool_result` block per call (per
    Anthropic's parallel-tool-use formatting rule).

    A user message immediately following one or more `tool` messages
    has its content prepended with all accumulated `tool_result`
    blocks — keeping the docs' "tool_result first, text after"
    invariant inside a single user message.

    **System shape.** Anthropic's `system=` kwarg accepts either:
      - a plain string (no caching support), or
      - a list of `{"type": "text", "text": ..., "cache_control": ...}`
        blocks. The list form is **required** for prompt caching;
        passing a string with `cache_control` embedded in the text
        does nothing.
    If every system message has string content we keep the string
    form (back-compat with the original output shape). If any
    system message has list content we upgrade to the list form,
    converting earlier string content to a single text block and
    appending each list message's blocks verbatim. Crucially, blocks
    with `cache_control` flow through unchanged — without that, an
    agent that supplies a cached system prompt silently loses
    Anthropic prompt caching (5-min ephemeral cache, 90% input-token
    discount).
    """
    converted: list[dict[str, Any]] = []
    system_str: str | None = None
    system_blocks: list[dict[str, Any]] | None = None
    pending_results: list[dict[str, Any]] = []

    def _promote_system_to_blocks() -> None:
        """Convert accumulated `system_str` to a list of blocks so we
        can append a list-form system message. Called the first time
        we see a list-form system message."""
        nonlocal system_str, system_blocks
        if system_blocks is not None:
            return
        system_blocks = []
        if system_str:
            system_blocks.append({"type": "text", "text": system_str})
            system_str = None

    def flush_pending_alone() -> None:
        """Emit accumulated tool_results as their own user message."""
        nonlocal pending_results
        if pending_results:
            converted.append({"role": "user", "content": list(pending_results)})
            pending_results = []

    for m in messages:
        if m.role == "system":
            if isinstance(m.content, str):
                if system_blocks is not None:
                    # We already upgraded to blocks; append a new text
                    # block rather than rebuild the string.
                    if m.content:
                        system_blocks.append(
                            {"type": "text", "text": m.content}
                        )
                else:
                    system_str = (
                        f"{system_str}\n{m.content}" if system_str else m.content
                    )
                continue
            if isinstance(m.content, list):
                _promote_system_to_blocks()
                for block in m.content:
                    if isinstance(block, dict):
                        # Pass through native Anthropic system blocks
                        # (`{"type": "text", "text": ..., "cache_control": ...}`)
                        # verbatim so prompt-caching markers survive.
                        # Translate the neutral text shape (`{"text": ...}`)
                        # to Anthropic's tagged form too.
                        if "type" in block:
                            system_blocks.append(dict(block))
                        elif "text" in block:
                            out_block: dict[str, Any] = {
                                "type": "text", "text": block["text"],
                            }
                            if "cache_control" in block:
                                out_block["cache_control"] = block["cache_control"]
                            system_blocks.append(out_block)
                continue

        if m.role == "tool":
            pending_results.append(_tool_result_block(m))
            continue

        if m.role == "user":
            user_content: str | list
            if isinstance(m.content, str):
                user_content = m.content
            else:
                user_content = [_convert_part(p) for p in m.content]

            if pending_results:
                # Merge: tool_results FIRST, then the user's text /
                # content blocks. Per Anthropic docs this is the
                # required order inside one user message.
                content_blocks: list[dict[str, Any]] = list(pending_results)
                if isinstance(user_content, str):
                    if user_content:
                        content_blocks.append(
                            {"type": "text", "text": user_content}
                        )
                else:
                    content_blocks.extend(user_content)
                converted.append({"role": "user", "content": content_blocks})
                pending_results = []
            else:
                # Anthropic's API rejects `{"role": "user", "content": ""}`
                # AND `{"role": "user", "content": []}` with a 400
                # ("messages: at least one message is required" / "content:
                # ... at least one block"). The empty-list case can arise
                # when every part filtered through `_convert_part` was
                # dropped (e.g. an opaque foreign reasoning block). Skip
                # the message rather than send a request that's
                # guaranteed to fail.
                if isinstance(user_content, str) and not user_content:
                    continue
                if isinstance(user_content, list) and not user_content:
                    continue
                converted.append({"role": "user", "content": user_content})
            continue

        # assistant — no merging; flush pending defensively, then emit.
        flush_pending_alone()
        if isinstance(m.content, str):
            assistant_content: str | list = m.content
        else:
            assistant_content = [_convert_part(p) for p in m.content]
        # Same constraint as user — empty assistant content (`""` or
        # `[]`) is rejected by Anthropic with a 400. This happens
        # most often via `Message.assistant_from_response(resp)` when
        # `resp` was empty (no text, no tool calls — content_filter
        # / length / error finish reasons): the helper falls back to
        # `content=""` and the next turn's API call dies. Defence-
        # in-depth: drop the message; the conversation remains valid
        # because an empty assistant turn carries no information.
        if isinstance(assistant_content, str) and not assistant_content:
            continue
        if isinstance(assistant_content, list) and not assistant_content:
            continue
        converted.append({"role": "assistant", "content": assistant_content})

    flush_pending_alone()  # any trailing tool_results
    # Return list-form when any list-form system message was seen
    # (preserves cache_control / future native fields), otherwise
    # keep the legacy str form so existing callers see identical
    # output shape.
    system: str | list[dict[str, Any]] | None
    if system_blocks is not None:
        system = system_blocks if system_blocks else None
    else:
        system = system_str
    return converted, system


# ---------------------------------------------------------------------------
# Tool config
# ---------------------------------------------------------------------------


def _convert_tools(
    tools: list[ToolSpec] | None,
    provider_options: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build Anthropic's `tools` param from neutral specs + native
    blocks via provider_options.

    Anthropic uses `input_schema` (not Gemini's `parameters`) for the
    JSON Schema and accepts native server-tool blocks (web_search,
    code_execution, etc.) alongside user-defined ones."""
    blocks: list[dict[str, Any]] = []
    if tools:
        for t in tools:
            blocks.append({
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            })
    if provider_options:
        for extra in provider_options.get("tools") or []:
            blocks.append(extra)
    return blocks


def _convert_tool_choice(
    tool_choice: ToolChoice | None,
    *,
    thinking_enabled: bool = False,
) -> dict[str, Any] | None:
    """Map neutral tool_choice to Anthropic's shape.

      - "auto"     → {"type": "auto"}
      - "required" → {"type": "any"}     (must use SOME tool)
      - "none"     → {"type": "none"}
      - dict       → passthrough (caller knows Anthropic's shape, e.g.
                     {"type": "tool", "name": "..."} or
                     {"type": "auto", "disable_parallel_tool_use": True})

    When `thinking_enabled` is True, only `auto` and `none` are valid
    per the docs. We raise locally rather than send a guaranteed-400
    payload upstream — gives agents a cleaner error message.
    """
    if tool_choice is None:
        return None
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        if thinking_enabled:
            raise ValueError(
                "tool_choice='required' is not compatible with extended/"
                "adaptive thinking on Anthropic — only 'auto' and 'none' "
                "are accepted when thinking is enabled."
            )
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        if thinking_enabled:
            tc_type = tool_choice.get("type")
            if tc_type in {"any", "tool"}:
                raise ValueError(
                    f"tool_choice {tc_type!r} is not compatible with "
                    "extended/adaptive thinking on Anthropic — only "
                    "'auto' and 'none' are accepted when thinking is "
                    "enabled."
                )
        # A Gemini-shaped `tool_choice` ({"function_calling_config":
        # {...}}) reaching the Anthropic adapter is unambiguously a
        # cross-provider misconfiguration — most commonly a shared
        # `provider_options`/`tool_choice` reused across a fallback
        # chain. Forwarding it is a GUARANTEED 400 with an opaque
        # upstream message. Raise locally with an actionable error
        # instead, matching the thinking-incompatible precedent
        # above (which also raises rather than send a known-400).
        # Only the unambiguous Gemini key is rejected — any other
        # dict is assumed Anthropic-shaped and passed through (a
        # caller who knows Anthropic's shape may legitimately send
        # one we don't enumerate).
        if "function_calling_config" in tool_choice:
            raise ValueError(
                "tool_choice has a Gemini-shaped "
                "'function_calling_config' key but this is the "
                "Anthropic adapter — use the neutral string form "
                "('auto' / 'none' / 'required') for cross-provider "
                "portability, or an Anthropic-shaped dict "
                "({'type': 'auto'|'none'|'any'|'tool', ...})."
            )
        # Already Anthropic-shaped (or a shape we don't enumerate but
        # the caller vouches for) — pass through.
        return tool_choice
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AnthropicAdapter(ProviderAdapter):
    provider_name = "anthropic"

    def __init__(
        self,
        *,
        concrete_model: str,
        api_key: str,
        base_url: str | None = None,
    ) -> None:
        self.concrete_model = concrete_model
        self._api_key = api_key
        self._base_url = base_url
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "anthropic not installed; `pip install anthropic`"
                ) from exc
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                # Custom proxy / regional endpoint (Bedrock proxy,
                # LiteLLM, etc.). Leave unset for the SDK default.
                kwargs["base_url"] = self._base_url
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    @staticmethod
    def _classify(exc: BaseException) -> RetryHint:
        """Map Anthropic SDK exceptions to typed `RetryHint`.

        Class-name match (rather than `isinstance`) so we don't have
        to import `anthropic` here — keeps the SDK a deferred dep.
        Anthropic's exception names are stable across recent SDK
        versions; future renames fall through to the default hint
        (still retriable, so transients don't go un-retried).
        """
        cls_name = type(exc).__name__

        if cls_name == "RateLimitError":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                retry_after_seconds=_anthropic_retry_after(exc),
                upstream_class=cls_name,
            )
        if cls_name in ("APITimeoutError", "APIConnectionError"):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
                upstream_class=cls_name,
            )
        # 503 / 529 from Anthropic. The 529 "Overloaded" was a
        # response-class error in early SDK versions; current SDKs
        # surface it via the class below.
        if cls_name in ("InternalServerError", "OverloadedError"):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
                upstream_class=cls_name,
            )
        if cls_name in ("AuthenticationError", "PermissionDeniedError"):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
                upstream_class=cls_name,
            )
        if cls_name in (
            "BadRequestError",
            "UnprocessableEntityError",
            "NotFoundError",
            "ConflictError",
        ):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
                upstream_class=cls_name,
            )

        # The SDK raises this when the response body fails its Pydantic
        # validation — usually a CDN injecting an HTML error page or a
        # stale proxy returning a partial JSON body. Treat as a
        # transient timeout-class problem; one retry typically clears it.
        if cls_name == "APIResponseValidationError":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
                upstream_class=cls_name,
            )

        # `APIStatusError` is the parent of every typed HTTP-status
        # exception above. A CDN-fronted Anthropic deployment can
        # surface untyped 4xx/5xx (e.g. Cloudflare 530, Akamai 504)
        # that the SDK lifts into the parent class with `status_code`
        # populated. Without this branch they'd fall through to
        # `internal_error` (retriable) — which over-retries a 4xx,
        # under-aggressively backs off a 5xx.
        if cls_name == "APIStatusError":
            status = getattr(exc, "status_code", None)
            if status == 429:
                return RetryHint(
                    code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                    retry_after_seconds=_anthropic_retry_after(exc),
                    upstream_class=cls_name,
                )
            if isinstance(status, int) and 500 <= status < 600:
                return RetryHint(
                    code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
                    upstream_class=cls_name,
                )
            if isinstance(status, int) and 400 <= status < 500:
                return RetryHint(
                    code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
                    upstream_class=cls_name,
                )
            # Status missing / nonsense — fall through to default.

        return RetryHint(
            code=ErrorCode.INTERNAL_ERROR,
            upstream_class=cls_name,
        )

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        provider_options: dict[str, Any] | None = None,
    ) -> LlmResponse | AsyncIterator[LlmDelta]:
        client = self._get_client()
        kwargs = _build_create_kwargs(
            concrete_model=self.concrete_model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_options=provider_options,
        )
        if stream:
            return self._generate_stream(client, kwargs)

        resp = await client.messages.create(**kwargs)
        return _convert_response(resp)

    async def _generate_stream(
        self, client: Any, kwargs: dict[str, Any]
    ) -> AsyncIterator[LlmDelta]:
        """Translate Anthropic's SSE event stream into neutral LlmDeltas.

        Per the official docs, the event flow is::

            message_start
            (per content block:
                content_block_start
                content_block_delta+   (text_delta | input_json_delta |
                                       thinking_delta | signature_delta)
                content_block_stop
            )+
            message_delta              (stop_reason + cumulative usage)
            message_stop

        Plus optional `ping` and `error` events.

        State per block index is required because:
          - `tool_use.input` arrives as `input_json_delta` partials
            ("partial JSON strings"); we accumulate and `json.loads` on
            block stop.
          - `thinking` blocks need both their `thinking_delta` text AND
            a separate `signature_delta` to round-trip; we emit a
            single completed `reasoning_block` on block stop.

        Server-side tool blocks (`server_tool_use`,
        `web_search_tool_result`) are recognised and skipped — agents
        don't drive those, the upstream resolves them server-side.
        """
        block_state: dict[int, dict[str, Any]] = {}
        # Anthropic only reports `input_tokens` /
        # `cache_read_input_tokens` / `cache_creation_input_tokens`
        # on `message_start`. The terminal `message_delta.usage`
        # carries the final `output_tokens` but ZEROS for input/cache
        # (it is NOT cumulative for those — only output grows). A
        # consumer that reads the LAST usage delta (rather than
        # max-aggregating across deltas) would therefore see
        # `input_tokens=0`, corrupting cost/quota accounting. Carry
        # the message_start input/cache figures forward and merge
        # them into the message_delta usage so every emitted usage
        # delta is self-consistent regardless of how downstream
        # aggregates.
        start_input_tokens = 0
        start_cache_read = 0
        start_cache_write = 0

        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    usage_meta = getattr(msg, "usage", None) if msg else None
                    if usage_meta is not None:
                        u = _usage_from_anthropic(usage_meta)
                        start_input_tokens = u.input_tokens
                        start_cache_read = u.cache_read_tokens
                        start_cache_write = u.cache_write_tokens
                        yield LlmDelta(usage=u)
                    continue

                if etype == "content_block_start":
                    idx = getattr(event, "index", -1)
                    cb = getattr(event, "content_block", None)
                    cbtype = getattr(cb, "type", None) if cb else None
                    state: dict[str, Any] = {"type": cbtype}
                    if cbtype == "tool_use":
                        state["id"] = getattr(cb, "id", "") or ""
                        state["name"] = getattr(cb, "name", "") or ""
                        state["partial_json"] = []
                    elif cbtype == "thinking":
                        state["thinking_chunks"] = []
                        state["signature"] = ""
                    elif cbtype == "redacted_thinking":
                        # `data` arrives in the start event itself —
                        # there are no `redacted_thinking_delta`
                        # events. Capture verbatim for round-trip.
                        state["data"] = getattr(cb, "data", "") or ""
                    block_state[idx] = state
                    continue

                if etype == "content_block_delta":
                    idx = getattr(event, "index", -1)
                    state = block_state.get(idx, {})
                    d = getattr(event, "delta", None)
                    dtype = getattr(d, "type", None) if d else None
                    if dtype == "text_delta":
                        text = getattr(d, "text", None)
                        if text:
                            yield LlmDelta(text=text)
                    elif dtype == "thinking_delta":
                        chunk = getattr(d, "thinking", None) or ""
                        if "thinking_chunks" in state:
                            state["thinking_chunks"].append(chunk)
                        if chunk:
                            yield LlmDelta(text=chunk, thought=True)
                    elif dtype == "signature_delta":
                        sig = getattr(d, "signature", None) or ""
                        if "signature" in state:
                            state["signature"] = sig
                    elif dtype == "input_json_delta":
                        partial = getattr(d, "partial_json", None) or ""
                        if "partial_json" in state:
                            state["partial_json"].append(partial)
                    # Unknown delta types — ignore per docs guidance:
                    # "new event types may be added, and your code
                    # should handle unknown event types gracefully."
                    continue

                if etype == "content_block_stop":
                    idx = getattr(event, "index", -1)
                    state = block_state.pop(idx, {})
                    btype = state.get("type")
                    if btype == "tool_use":
                        args_str = "".join(state.get("partial_json", []))
                        try:
                            args = json.loads(args_str) if args_str else {}
                        except json.JSONDecodeError:
                            args = {}  # malformed; surface empty
                        yield LlmDelta(tool_call=ToolCall(
                            id=state.get("id", ""),
                            name=state.get("name", ""),
                            args=args if isinstance(args, dict) else {},
                        ))
                    elif btype == "thinking":
                        yield LlmDelta(reasoning_block={
                            "type": "thinking",
                            "thinking": "".join(state.get("thinking_chunks", [])),
                            "signature": state.get("signature", ""),
                        })
                    elif btype == "redacted_thinking":
                        yield LlmDelta(reasoning_block={
                            "type": "redacted_thinking",
                            "data": state.get("data", ""),
                        })
                    # text / server_tool_use / web_search_tool_result /
                    # other server-side blocks: nothing to emit at
                    # block-stop time (text was already streamed via
                    # text_delta).
                    continue

                if etype == "message_delta":
                    d = getattr(event, "delta", None)
                    stop_reason = getattr(d, "stop_reason", None) if d else None
                    usage_meta = getattr(event, "usage", None)
                    finish = _map_stop_reason(stop_reason) if stop_reason else None
                    if finish or usage_meta is not None:
                        usage = (
                            _usage_from_anthropic(usage_meta)
                            if usage_meta is not None
                            else None
                        )
                        if usage is not None:
                            # message_delta.usage zeros input/cache —
                            # backfill from message_start so this
                            # terminal delta is self-consistent. Use
                            # `or start_*` so a (rare) non-zero
                            # message_delta input still wins.
                            usage.input_tokens = (
                                usage.input_tokens or start_input_tokens
                            )
                            usage.cache_read_tokens = (
                                usage.cache_read_tokens or start_cache_read
                            )
                            usage.cache_write_tokens = (
                                usage.cache_write_tokens or start_cache_write
                            )
                        yield LlmDelta(finish_reason=finish, usage=usage)
                    continue

                # `ping`, `message_stop`, `error`, and any future event
                # type fall through to the no-op tail. The error event
                # is logged and swallowed: the caller will still see
                # finish_reason from the preceding message_delta when
                # one was emitted; otherwise the iterator simply ends.
                if etype == "error":
                    err = getattr(event, "error", None)
                    logger.warning(
                        "anthropic_stream_error",
                        extra={
                            "event": "anthropic_stream_error",
                            "error_type": getattr(err, "type", None) if err else None,
                            "error_message": (
                                getattr(err, "message", None) if err else None
                            ),
                        },
                    )

    # ------------------------------------------------------------------
    # embed / count_tokens
    # ------------------------------------------------------------------

    async def embed(
        self, text: str | list[str], *, provider_options: dict[str, Any] | None = None
    ) -> list[list[float]]:
        # Anthropic doesn't ship a first-party embeddings API; their
        # docs recommend Voyage AI as the embeddings partner. Wire a
        # separate `voyage` provider if you need this.
        raise NotImplementedError(
            "Anthropic doesn't provide an embeddings API; "
            "use a different provider (e.g. Voyage AI) for embeddings."
        )

    async def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens via Anthropic's `messages.count_tokens` endpoint.

        The endpoint accepts the same structured inputs as
        `messages.create` (system, tools, images, PDFs, thinking) but
        our neutral `count_tokens` signature only takes messages. We
        send the converted messages + system; the response is a
        single `input_tokens` integer.

        Per the docs: "The token count should be considered an
        estimate. In some cases, the actual number of input tokens
        used when creating a message may differ by a small amount."
        """
        client = self._get_client()
        converted_messages, system = _convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.concrete_model,
            "messages": converted_messages,
        }
        if system:
            kwargs["system"] = system
        result = await client.messages.count_tokens(**kwargs)
        return int(getattr(result, "input_tokens", 0) or 0)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without `anthropic` installed)
# ---------------------------------------------------------------------------


def _build_create_kwargs(
    *,
    concrete_model: str,
    messages: list[Message],
    tools: list[ToolSpec] | None,
    tool_choice: ToolChoice | None,
    temperature: float | None,
    max_tokens: int | None,
    provider_options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the kwargs dict for `messages.create`.

    Pure function — no SDK import, no network — so tests can exercise
    the full translation without Anthropic installed.
    """
    converted_messages, system = _convert_messages(messages)
    thinking_enabled = _is_thinking_enabled(provider_options)
    kwargs: dict[str, Any] = {
        "model": concrete_model,
        "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
        "messages": converted_messages,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        # The docs flag temperature as incompatible with thinking
        # ("not compatible with `temperature` or `top_k` modifications").
        # We forward what the caller asked for; the API will reject it
        # cleanly when thinking is on. Logging here would help diagnose,
        # but raising would over-constrain — Anthropic accepts default
        # temperature with thinking, just not deviations.
        kwargs["temperature"] = temperature

    tool_blocks = _convert_tools(tools, provider_options)
    if tool_blocks:
        kwargs["tools"] = tool_blocks
    tc = _convert_tool_choice(tool_choice, thinking_enabled=thinking_enabled)
    if tc is not None:
        kwargs["tool_choice"] = tc

    if provider_options:
        for k in _PASSTHROUGH_KWARGS:
            if k in provider_options:
                kwargs[k] = provider_options[k]

    return kwargs


def _convert_response(resp: Any) -> LlmResponse:
    """Walk the response's content blocks → neutral LlmResponse.

    Thinking blocks (`thinking` and `redacted_thinking`) land in
    `reasoning_blocks` verbatim — preserving the exact upstream shape
    is critical because the `signature` / `data` field is what the
    next turn's API call decrypts to reconstruct context. Visible
    thinking text (when `display="summarized"`) is also concatenated
    into `thought_summary` for agent display.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    reasoning_blocks: list[dict[str, Any]] = []
    thought_text_parts: list[str] = []

    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            t = getattr(block, "text", None)
            if t:
                text_parts.append(t)
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=getattr(block, "id", "") or "",
                    name=getattr(block, "name", "") or "",
                    args=dict(getattr(block, "input", {}) or {}),
                )
            )
        elif btype in _REASONING_BLOCK_TYPES:
            # Preserve the full block verbatim — the signature/data
            # field is what the next turn's API call needs unchanged.
            reasoning_blocks.append(_block_to_dict(block))
            if btype == "thinking":
                t = getattr(block, "thinking", None)
                if t:
                    thought_text_parts.append(t)

    usage_meta = getattr(resp, "usage", None)
    usage = _usage_from_anthropic(usage_meta) if usage_meta is not None else TokenUsage()

    finish = _map_stop_reason(getattr(resp, "stop_reason", None))

    raw: dict[str, Any] = {}
    md = getattr(resp, "model_dump", None)
    if callable(md):
        try:
            raw = md() or {}
        except Exception:  # noqa: BLE001
            raw = {}

    return LlmResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=finish if finish in {
            "stop", "length", "tool_calls", "content_filter", "error"
        } else "stop",
        usage=usage,
        raw=raw,
        thought_summary="".join(thought_text_parts) or None,
        reasoning_blocks=reasoning_blocks,
    )


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Render an Anthropic content block as a JSON-friendly dict.

    Preserves the canonical fields per type so the dict is suitable
    for round-tripping back through `messages.create`. We intentionally
    do NOT call the SDK's `model_dump` because some SDK versions
    serialise extra metadata that the API rejects on round-trip.
    """
    btype = getattr(block, "type", None)
    if btype == "thinking":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", "") or "",
            "signature": getattr(block, "signature", "") or "",
        }
    if btype == "redacted_thinking":
        return {
            "type": "redacted_thinking",
            "data": getattr(block, "data", "") or "",
        }
    # Fallback for an UNHANDLED reasoning block subtype (a future
    # Anthropic addition someone wired into `_REASONING_BLOCK_TYPES`
    # without adding an explicit branch above). The previous code
    # called `block.model_dump()` here — which directly contradicts
    # this function's contract (see docstring): some SDK versions
    # serialise extra metadata that the API REJECTS on round-trip,
    # producing a 400 on the very next turn. Project a whitelist of
    # the round-trip-critical opaque fields via getattr instead
    # (never SDK metadata), and log so a maintainer adds a proper
    # branch. Unknown fields a future shape needs would still be
    # lost, but a clean minimal block is far better than a
    # guaranteed-400 metadata-laden one.
    logger.warning(
        "anthropic_unhandled_reasoning_block_type",
        extra={
            "event": "anthropic_unhandled_reasoning_block_type",
            "block_type": btype,
        },
    )
    out: dict[str, Any] = {"type": btype} if btype else {}
    for field in ("thinking", "signature", "data", "id"):
        val = getattr(block, field, None)
        if val is not None:
            out[field] = val
    return out
