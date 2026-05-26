"""bp_router.llm.providers.openai — OpenAI Responses API adapter.

Wraps the `openai` Python SDK (deferred import). Translates neutral
`Message` to the Responses API's flat `input` array of typed items;
honours `provider_options` for native features (reasoning effort,
allowed tools, web/file search, structured outputs).

Streaming is NOT yet wired — the request needs the full event-stream
taxonomy (the docs in scope only cover the function-call streaming
events). Non-streaming `generate`, `count_tokens`, and reasoning
round-trip via `include=["reasoning.encrypted_content"]` are wired.
`embed` is unsupported on this adapter (OpenAI exposes a separate
`/v1/embeddings` endpoint that doesn't fit the ProviderAdapter
contract today).

Three shape differences from Gemini / Anthropic the adapter bridges:

  - **Output is a flat `output[]` of mixed-type items** (`message`,
    `function_call`, `reasoning`, `custom_tool_call`) rather than a
    single content array per response. Function calls are first-class
    top-level items, not blocks inside the assistant message.
  - **`call_id` is the round-trip key** for the function-call ↔
    function-call-output pairing. The item's own `id` is separate and
    not used for mapping. Our neutral `ToolCall.id` stores `call_id`.
  - **`arguments` is a JSON-encoded string** in the wire format, not
    a parsed object. Parse on extract; serialize on emit.

System messages map to the top-level `instructions=` kwarg (cleanly
separated from the input array) — equivalent to a `developer`-role
message but easier to reason about.

Reasoning round-trip works exactly like Anthropic's: the adapter
populates `LlmResponse.reasoning_blocks` with provider-shaped opaque
items, and `Message.assistant_from_response` carries them back. We
always pass `include=["reasoning.encrypted_content"]` so stateless
deployments (no `previous_response_id`) get the encrypted blob needed
for round-trip.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from bp_router.llm.providers._openai_client import (
    classify_openai_exception,
    make_async_openai,
)
from bp_router.llm.providers.base import ProviderAdapter
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


# Top-level Responses kwargs that pass through `provider_options`
# unchanged. Everything else is either modelled as a first-class
# kwarg or has its own translation path.
_PASSTHROUGH_KWARGS = (
    "reasoning",          # {"effort": "...", "summary": "auto", ...}
    "metadata",
    "previous_response_id",
    "store",
    "service_tier",
    "text",               # response_format / structured outputs config
    "include",            # caller-supplied includes (we merge with our default)
    "phase",              # assistant-message phase signal
    "max_output_tokens",  # alternative spelling — we also accept top-level max_tokens
    "background",
    "parallel_tool_calls",
    "top_p",
    "top_logprobs",
    "logprobs",
    "user",
    "safety_identifier",
)


# Always include encrypted_content so stateless callers (no
# `previous_response_id`) get the encrypted blob they need to round-
# trip reasoning items on the next turn. No-op on non-reasoning models.
_DEFAULT_INCLUDES = ("reasoning.encrypted_content",)


# Item types that get dropped from neutral assistant content during
# round-trip — they're foreign to the OpenAI Responses API and would
# either 400 the request or silently confuse the model.
_FOREIGN_ITEM_TYPES = frozenset({"thinking", "redacted_thinking"})


# ---------------------------------------------------------------------------
# Stop reason mapping
# ---------------------------------------------------------------------------


def _derive_finish_reason(resp: Any) -> str:
    """Map a Response object to our neutral finish_reason.

    Responses doesn't expose a single `stop_reason` field. We infer:
      - `incomplete` + reason `max_output_tokens` → `length`
      - `incomplete` + reason `content_filter`     → `content_filter`
      - any function_call item in output           → `tool_calls`
      - otherwise (status `completed`)             → `stop`
    """
    status = getattr(resp, "status", None)
    if status == "incomplete":
        details = getattr(resp, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details else None
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        return "stop"

    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) == "function_call":
            return "tool_calls"

    return "stop"


# ---------------------------------------------------------------------------
# Part / item translation
# ---------------------------------------------------------------------------


def _convert_user_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one neutral content-part to an OpenAI user-message
    content block (`input_text` / `input_image`).

    Returns `None` if the part should be dropped (e.g., foreign-
    provider reasoning blocks accidentally routed here).
    """
    if not isinstance(part, dict):
        return part

    # Already-native OpenAI input block — pass through.
    t = part.get("type")
    if t and t.startswith("input_"):
        return part

    # Drop foreign-provider blocks that don't belong in user content.
    if t in _FOREIGN_ITEM_TYPES:
        return None

    # Neutral image → OpenAI input_image with base64 data URL.
    if "image" in part:
        img = part["image"]
        mime = img.get("mime_type", "image/jpeg")
        data = img.get("data", "")
        return {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{data}",
        }

    # Neutral document → OpenAI Responses `input_file` with a
    # base64 data URL. `filename` is required by the Responses API
    # — use the caller's `display_name` when supplied, otherwise
    # fall back to a generic name. Older Chat Completions
    # deployments don't support input_file and will 400; users
    # hitting that should send the document content as text or
    # use the file-upload API.
    if "document" in part:
        doc = part["document"]
        mime = doc.get("mime_type", "application/pdf")
        data = doc.get("data", "")
        return {
            "type": "input_file",
            "filename": doc.get("display_name") or "document.pdf",
            "file_data": f"data:{mime};base64,{data}",
        }

    # Neutral / Gemini-flavoured text → OpenAI input_text.
    if "text" in part:
        return {"type": "input_text", "text": part["text"]}

    # Unknown — pass through and let the upstream surface the error.
    return part


def _convert_messages(
    messages: list[Message],
) -> tuple[list[dict[str, Any]], str | None]:
    """Translate neutral Messages → (OpenAI input items, instructions).

    The Responses API takes a flat `input` array of typed items rather
    than a strict role-based message list. Assistant turns containing
    function calls / reasoning blocks get FLATTENED — text becomes one
    `{"role": "assistant", ...}` item, function calls become top-level
    `{"type": "function_call", ...}` items, and reasoning blocks
    become top-level `{"type": "reasoning", ...}` items in the order
    they were emitted.

    `tool` messages become top-level `function_call_output` items
    keyed by `call_id`.
    """
    items: list[dict[str, Any]] = []
    instructions: str | None = None

    for m in messages:
        if m.role == "system":
            if isinstance(m.content, str):
                instructions = (
                    f"{instructions}\n{m.content}" if instructions else m.content
                )
            continue

        if m.role == "tool":
            # The Responses API's `function_call_output.output` field
            # is text-only. For multimodal tool results we emit the
            # function_call_output with a sentinel summary AND a
            # synthesized follow-up user message carrying the actual
            # parts (input_image / input_text). Models then see the
            # narrative `function_call_output → user` order that the
            # API expects.
            if isinstance(m.content, list):
                items.append({
                    "type": "function_call_output",
                    "call_id": m.tool_call_id or "",
                    "output": (
                        f"[binary payload from {m.name or 'tool'} "
                        "tool result follows in next user message]"
                    ),
                })
                converted = [_convert_user_part(p) for p in m.content]
                content_parts = [c for c in converted if c]
                if content_parts:
                    items.append({"role": "user", "content": content_parts})
                continue
            output_payload: str
            if isinstance(m.content, str):
                output_payload = m.content
            else:
                # Dict — JSON-encode for the text-only field.
                output_payload = json.dumps(m.content)
            items.append({
                "type": "function_call_output",
                "call_id": m.tool_call_id or "",
                "output": output_payload,
            })
            continue

        if m.role == "user":
            content: str | list[dict[str, Any]]
            if isinstance(m.content, str):
                content = m.content
            else:
                converted = [_convert_user_part(p) for p in m.content]
                content = [c for c in converted if c]
            # Empty user content is rejected by the Responses API
            # ("messages: input items must have non-empty content").
            # The list-form can become empty when every part was
            # dropped by `_convert_user_part` (foreign reasoning,
            # unsupported types). Mirror the assistant-empty-string
            # skip below so adapters that accidentally feed empty
            # user turns don't crash the request.
            if isinstance(content, str) and not content:
                continue
            if isinstance(content, list) and not content:
                continue
            items.append({"role": "user", "content": content})
            continue

        # role == "assistant" — flatten content into the input items
        # array. Structured parts become standalone items so the
        # Responses API sees the same shape it emitted.
        #
        # Assistant text MUST be emitted in the canonical Responses
        # shape — a `message` item whose `content` is a list of
        # `{"type": "output_text", "text": ...}` blocks — NOT the
        # easy `{"role": "assistant", "content": "<str>"}` form.
        # The easy form is accepted for a *standalone* assistant
        # turn, but in STATELESS mode (no `previous_response_id` —
        # the mode this adapter is built for, see module docstring)
        # the Responses API pairs each `reasoning` item with the
        # `message` item that immediately follows it. When the
        # assistant turn carries round-tripped reasoning blocks,
        # the bare-string message breaks that pairing: the API
        # either 400s ("reasoning item must be followed by a
        # message") or silently drops the reasoning context,
        # degrading multi-turn tool-use quality. Mirroring the
        # exact output shape (`type: message` + `output_text`
        # blocks) is the documented round-trip contract.
        if isinstance(m.content, str):
            if m.content:
                items.append(_assistant_text_item(m.content))
            continue

        # Accumulate consecutive text fragments into one assistant
        # message but emit them BEFORE any non-text item that follows
        # so the natural ordering of the assistant turn is preserved
        # (e.g., text → function_call stays as text, function_call
        # in the input array — the API treats positional order as
        # meaningful).
        text_chunks: list[str] = []

        def _flush_text(text_chunks: list[str] = text_chunks) -> None:
            if text_chunks:
                joined = "".join(text_chunks)
                if joined:
                    items.append(_assistant_text_item(joined))
                text_chunks.clear()

        for part in m.content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")

            # Reasoning items (round-tripped from a previous response)
            # — emit as standalone items at the same position they
            # appeared in the assistant turn.
            if ptype == "reasoning":
                _flush_text()
                items.append(_serialize_reasoning_block(part))
                continue

            # Foreign reasoning blocks (Anthropic) — drop. OpenAI
            # rejects unknown types and these have no analogue here.
            if ptype in _FOREIGN_ITEM_TYPES:
                continue

            # Already-native OpenAI function_call item — pass through.
            if ptype == "function_call":
                _flush_text()
                items.append(_normalize_function_call_item(part))
                continue

            # Neutral function_call → OpenAI function_call item.
            if "function_call" in part:
                _flush_text()
                fc = part["function_call"]
                args = fc.get("args") or fc.get("input") or {}
                arguments_str = (
                    args if isinstance(args, str) else json.dumps(args)
                )
                items.append({
                    "type": "function_call",
                    "call_id": fc.get("id", ""),
                    "name": fc.get("name", ""),
                    "arguments": arguments_str,
                })
                continue

            # Text fragments — accumulate; flushed by the next non-
            # text item or at end of message.
            if "text" in part:
                if part["text"]:
                    text_chunks.append(part["text"])
                continue

            # Native OpenAI output_text block (rare on round-trip).
            if ptype == "output_text":
                t = part.get("text", "")
                if t:
                    text_chunks.append(t)
                continue

            # Unknown / image-in-assistant-content / etc. — ignore.

        _flush_text()  # any trailing text at end of assistant turn

    return items, instructions


def _normalize_function_call_item(part: dict[str, Any]) -> dict[str, Any]:
    """Pass-through for already-native OpenAI function_call items, but
    re-encode `arguments` if the caller handed us a dict."""
    out = dict(part)
    args = out.get("arguments")
    if isinstance(args, dict):
        out["arguments"] = json.dumps(args)
    return out


def _assistant_text_item(text: str) -> dict[str, Any]:
    """Build a Responses-API `message` input item for assistant text.

    Canonical round-trip shape — `{"type": "message", "role":
    "assistant", "content": [{"type": "output_text", "text": ...}]}`
    — mirrors exactly what the Responses API emits in `output[]`.
    Using the structured form (rather than the easy bare-string
    `{"role": "assistant", "content": "<str>"}`) keeps the
    reasoning↔message pairing intact in stateless mode."""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _serialize_reasoning_block(part: dict[str, Any]) -> dict[str, Any]:
    """Pass through a reasoning item verbatim, keeping the canonical
    fields. The `encrypted_content` is the bit that matters for round-
    trip — preserve it whether the caller passed it as bytes or str."""
    out: dict[str, Any] = {"type": "reasoning"}
    for k in ("id", "summary", "content", "encrypted_content"):
        if k in part:
            out[k] = part[k]
    return out


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def _convert_tools(
    tools: list[ToolSpec] | None,
    provider_options: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build the `tools` array. Function tools use OpenAI's shape
    (`type: function` plus `name`, `description`, `parameters`). Native
    blocks (built-in `web_search`, custom tools, etc.) come in via
    `provider_options["tools"]` and append after function tools.

    `strict` is intentionally NOT set by default. Per the docs:
        "If you omit `strict`, ... Responses requests will normalize
         your schema into strict mode."
    Letting OpenAI normalize matches their recommendation while
    keeping our schema verbatim. Agents who need non-strict semantics
    can set `strict: false` on a per-tool basis via provider_options.
    """
    blocks: list[dict[str, Any]] = []
    if tools:
        for t in tools:
            blocks.append({
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            })
    if provider_options:
        for extra in provider_options.get("tools") or []:
            blocks.append(extra)
    return blocks


def _convert_tool_choice(
    tool_choice: ToolChoice | None,
) -> str | dict[str, Any] | None:
    """Map neutral tool_choice to OpenAI's shape.

      - "auto"     → "auto"      (string)
      - "required" → "required"
      - "none"     → "none"
      - dict       → passthrough — caller knows OpenAI's shape, e.g.
                     {"type": "function", "name": "..."} to force a
                     specific function, or {"type": "allowed_tools",
                     "mode": "auto", "tools": [...]}.

    OpenAI accepts the bare strings; no wrapping needed.
    """
    if tool_choice is None:
        return None
    # Dict short-circuits — passing through verbatim. Must come BEFORE
    # the string membership check to avoid `TypeError: unhashable`.
    if isinstance(tool_choice, dict):
        return tool_choice
    if isinstance(tool_choice, str) and tool_choice in {"auto", "required", "none"}:
        return tool_choice
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"

    def __init__(
        self,
        *,
        concrete_model: str,
        api_key: str,
        base_url: str | None = None,
    ) -> None:
        self.concrete_model = concrete_model
        self._api_key = api_key
        # Azure OpenAI proxy, LiteLLM gateway, Portkey, etc. Leave None
        # for the official OpenAI endpoint.
        self._base_url = base_url
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = make_async_openai(
                api_key=self._api_key, base_url=self._base_url
            )
        return self._client

    # Map OpenAI SDK exceptions → typed `RetryHint`. Static so
    # `LlmService._call_with_fallback` can call without an instance.
    # Shared across the four OpenAI-SDK adapters via the same helper.
    _classify = staticmethod(classify_openai_exception)

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

        resp = await client.responses.create(**kwargs)
        return _convert_response(resp)

    async def _generate_stream(
        self, client: Any, kwargs: dict[str, Any]
    ) -> AsyncIterator[LlmDelta]:
        """Translate the Responses API SSE event stream into neutral
        LlmDeltas.

        Per the docs, the documented event types we handle here:

          - response.output_item.added       — a new output item starts
          - response.output_text.delta       — text chunk for a message
          - response.function_call_arguments.delta — partial JSON args
          - response.function_call_arguments.done  — finalised tool call
          - response.refusal.delta           — refusal text chunk
          - response.output_item.done        — output item complete
          - response.completed               — final response object
          - response.failed                  — generation failed
          - error                            — overload / rate-limit etc.

        Other documented events (response.created, response.in_progress,
        content_part.added/done, output_text.annotation.added,
        file_search_call.* / code_interpreter.* progress events) are
        consumed and ignored — they're either lifecycle no-ops for our
        purposes or server-side tool activity the agent doesn't drive.

        Per-output-index state is required because:
          - function_call.arguments arrives as `partial_json` chunks
            (mirroring Anthropic's `input_json_delta` shape); we
            accumulate and `json.loads` on .done.
          - response.output_text.delta carries text without saying
            whether the parent is a regular `message` item or a
            `reasoning` item. We track parent type via
            `output_item.added` and flag thought=True for reasoning
            text deltas.
        """
        item_state: dict[int, dict[str, Any]] = {}

        async with client.responses.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "response.output_item.added":
                    idx = getattr(event, "output_index", -1)
                    item = getattr(event, "item", None)
                    item_type = getattr(item, "type", None) if item else None
                    state: dict[str, Any] = {"type": item_type}
                    if item_type == "function_call":
                        state["call_id"] = getattr(item, "call_id", "") or ""
                        state["name"] = getattr(item, "name", "") or ""
                        state["partial_args"] = []
                    item_state[idx] = state
                    continue

                if etype == "response.function_call_arguments.delta":
                    idx = getattr(event, "output_index", -1)
                    state = item_state.get(idx, {})
                    chunk = getattr(event, "delta", None) or ""
                    if "partial_args" in state and chunk:
                        state["partial_args"].append(chunk)
                    continue

                if etype == "response.function_call_arguments.done":
                    idx = getattr(event, "output_index", -1)
                    state = item_state.get(idx, {})
                    args_str = "".join(state.get("partial_args", []))
                    try:
                        args = json.loads(args_str) if args_str else {}
                    except json.JSONDecodeError:
                        args = {}  # malformed; surface empty
                    yield LlmDelta(tool_call=ToolCall(
                        id=state.get("call_id", ""),
                        name=state.get("name", ""),
                        args=args if isinstance(args, dict) else {},
                    ))
                    continue

                if etype == "response.output_text.delta":
                    idx = getattr(event, "output_index", -1)
                    parent = item_state.get(idx, {})
                    is_thought = parent.get("type") == "reasoning"
                    text = getattr(event, "delta", None) or ""
                    if text:
                        yield LlmDelta(text=text, thought=is_thought)
                    continue

                if etype == "response.refusal.delta":
                    # Refusals stream as their own delta type but
                    # still represent assistant-visible text. Surface
                    # via the same channel; the final finish_reason
                    # (derived from response.status) signals that the
                    # message was a refusal vs a normal answer.
                    text = getattr(event, "delta", None) or ""
                    if text:
                        yield LlmDelta(text=text)
                    continue

                if etype == "response.output_item.done":
                    idx = getattr(event, "output_index", -1)
                    item = getattr(event, "item", None)
                    item_type = getattr(item, "type", None) if item else None
                    if item_type == "reasoning":
                        # The dispatch streaming aggregator collects
                        # these into LlmResultFrame.reasoning_blocks
                        # so agents using the final-message API get
                        # the round-trip data. The encrypted_content
                        # is what matters for stateless reasoning.
                        yield LlmDelta(
                            reasoning_block=_reasoning_item_to_dict(item),
                        )
                    item_state.pop(idx, None)
                    continue

                if etype == "response.completed":
                    resp = getattr(event, "response", None)
                    if resp is not None:
                        usage_meta = getattr(resp, "usage", None)
                        usage = (
                            _usage_from_openai(usage_meta)
                            if usage_meta is not None
                            else None
                        )
                        finish = _derive_finish_reason(resp)
                        yield LlmDelta(finish_reason=finish, usage=usage)
                    continue

                if etype == "response.failed":
                    resp = getattr(event, "response", None)
                    err = getattr(resp, "error", None) if resp is not None else None
                    logger.warning(
                        "openai_stream_failed",
                        extra={
                            "event": "openai_stream_failed",
                            "error_type": (
                                getattr(err, "code", None) if err else None
                            ),
                            "error_message": (
                                getattr(err, "message", None) if err else None
                            ),
                        },
                    )
                    continue

                if etype == "error":
                    err = getattr(event, "error", None) or getattr(
                        event, "message", None
                    )
                    logger.warning(
                        "openai_stream_error",
                        extra={
                            "event": "openai_stream_error",
                            "error": str(err) if err else None,
                        },
                    )
                    continue

                # Ignored events (per docs):
                #   response.created, response.in_progress,
                #   response.content_part.added/done,
                #   response.output_text.done,
                #   response.output_text.annotation.added,
                #   response.refusal.done,
                #   response.file_search_call.* (in_progress / searching / completed),
                #   response.code_interpreter.* (in_progress / delta / done /
                #     interpreting / completed),
                #   any future event type. Per the docs versioning
                #   policy we handle unknown events gracefully.

    # ------------------------------------------------------------------
    # embed / count_tokens
    # ------------------------------------------------------------------

    async def embed(self, text: str | list[str]) -> list[list[float]]:
        # OpenAI exposes a separate `/v1/embeddings` endpoint
        # (`client.embeddings.create`). It doesn't share the Responses
        # API surface this adapter wraps, so the right place for
        # embeddings support is a dedicated `openai-embeddings`
        # provider rather than overloading this one.
        raise NotImplementedError(
            "OpenAI embeddings live on /v1/embeddings, not the Responses "
            "API; wire a dedicated embeddings provider when needed."
        )

    async def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens via `responses.input_tokens.count`.

        The endpoint accepts the same input shape as `responses.create`
        (with tools, instructions, images, files). Our neutral
        `count_tokens` signature only takes messages today, so we send
        messages + instructions and let agents add tools-aware counting
        through provider_options later if needed.
        """
        client = self._get_client()
        items, instructions = _convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.concrete_model,
            "input": items,
        }
        if instructions:
            kwargs["instructions"] = instructions
        result = await client.responses.input_tokens.count(**kwargs)
        return int(getattr(result, "input_tokens", 0) or 0)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without `openai` installed)
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
    """Assemble the kwargs for `responses.create`.

    Pure function — no SDK import, no network — so tests can exercise
    the full translation without OpenAI installed.
    """
    items, instructions = _convert_messages(messages)
    kwargs: dict[str, Any] = {
        "model": concrete_model,
        "input": items,
    }
    if instructions:
        kwargs["instructions"] = instructions
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        # Responses uses `max_output_tokens`, not `max_tokens`.
        kwargs["max_output_tokens"] = max_tokens

    tool_blocks = _convert_tools(tools, provider_options)
    if tool_blocks:
        kwargs["tools"] = tool_blocks
    tc = _convert_tool_choice(tool_choice)
    if tc is not None:
        kwargs["tool_choice"] = tc

    # Merge our default include with any caller-supplied includes,
    # de-duplicated. `reasoning.encrypted_content` is always on so
    # stateless callers (no previous_response_id) can round-trip
    # reasoning items on the next turn.
    caller_include = (provider_options or {}).get("include") or []
    merged = list(_DEFAULT_INCLUDES) + [
        x for x in caller_include if x not in _DEFAULT_INCLUDES
    ]
    kwargs["include"] = merged

    if provider_options:
        for k in _PASSTHROUGH_KWARGS:
            if k == "include":
                continue  # already merged above
            if k in provider_options:
                kwargs[k] = provider_options[k]

    return kwargs


def _convert_response(resp: Any) -> LlmResponse:
    """Walk `resp.output[]` → neutral LlmResponse.

    Reasoning items become opaque entries on `reasoning_blocks` so the
    SDK helper carries them back unchanged on the next assistant turn.
    Function calls become typed `ToolCall`s with `args` parsed from
    the JSON-encoded `arguments` string. Output text is concatenated
    across all `message` items.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    reasoning_blocks: list[dict[str, Any]] = []
    thought_summary_chunks: list[str] = []

    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", None)

        if itype == "message":
            for block in getattr(item, "content", None) or []:
                btype = getattr(block, "type", None)
                if btype == "output_text":
                    t = getattr(block, "text", None)
                    if t:
                        text_parts.append(t)
            continue

        if itype == "function_call":
            args_str = getattr(item, "arguments", "") or ""
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(ToolCall(
                # Per docs: `call_id` is the round-trip key, NOT `id`.
                id=getattr(item, "call_id", "") or "",
                name=getattr(item, "name", "") or "",
                args=args,
            ))
            continue

        if itype == "reasoning":
            reasoning_blocks.append(_reasoning_item_to_dict(item))
            for s in getattr(item, "summary", None) or []:
                stype = getattr(s, "type", None)
                if stype == "summary_text":
                    txt = getattr(s, "text", None)
                    if txt:
                        thought_summary_chunks.append(txt)
            continue

        # custom_tool_call, web_search_call, code_interpreter_call,
        # etc. — recognised but not surfaced through neutral types
        # (they're either provider-specific extensions or server-side
        # tools the model invokes itself).

    usage_meta = getattr(resp, "usage", None)
    usage = _usage_from_openai(usage_meta) if usage_meta is not None else TokenUsage()

    raw: dict[str, Any] = {}
    md = getattr(resp, "model_dump", None)
    if callable(md):
        try:
            raw = md() or {}
        except Exception:  # noqa: BLE001
            raw = {}

    finish = _derive_finish_reason(resp)
    return LlmResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=finish if finish in {
            "stop", "length", "tool_calls", "content_filter", "error"
        } else "stop",
        usage=usage,
        raw=raw,
        thought_summary="".join(thought_summary_chunks) or None,
        reasoning_blocks=reasoning_blocks,
    )


def _reasoning_item_to_dict(item: Any) -> dict[str, Any]:
    """Render an OpenAI reasoning item as a JSON-friendly dict.

    Pins to the canonical fields needed for round-trip
    (`type`, `id`, `summary`, `encrypted_content`) so the dict is
    safe to send back through `responses.create` verbatim.
    """
    out: dict[str, Any] = {"type": "reasoning"}
    for k in ("id", "encrypted_content"):
        v = getattr(item, k, None)
        # Treat empty string the same as missing — `encrypted_content`
        # is meaningless without a real opaque blob, and including
        # it just clutters the round-trip dict.
        if v:
            out[k] = v
    summary = getattr(item, "summary", None) or []
    out["summary"] = [
        {
            "type": getattr(s, "type", "summary_text") or "summary_text",
            "text": getattr(s, "text", "") or "",
        }
        for s in summary
    ]
    content = getattr(item, "content", None)
    if content is not None:
        # Round-trip whatever the SDK exposes; we don't normalise.
        out["content"] = (
            content if isinstance(content, list) else list(content)
        )
    return out


def _usage_from_openai(usage_meta: Any) -> TokenUsage:
    """Build a neutral TokenUsage from a Responses `usage` object.

    Responses reports `input_tokens`, `output_tokens`, and
    `output_tokens_details.reasoning_tokens` for reasoning models.
    We map reasoning_tokens onto our `thoughts_tokens` field so it
    surfaces consistently across providers.
    """
    input_tokens = int(getattr(usage_meta, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage_meta, "output_tokens", 0) or 0)
    cached = 0
    input_details = getattr(usage_meta, "input_tokens_details", None)
    if input_details is not None:
        cached = int(getattr(input_details, "cached_tokens", 0) or 0)
    reasoning = 0
    output_details = getattr(usage_meta, "output_tokens_details", None)
    if output_details is not None:
        reasoning = int(getattr(output_details, "reasoning_tokens", 0) or 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thoughts_tokens=reasoning,
        cache_read_tokens=cached,
    )


# ---------------------------------------------------------------------------
# Embeddings adapter
# ---------------------------------------------------------------------------


class OpenAIEmbeddingsAdapter(ProviderAdapter):
    """OpenAI embeddings adapter — separate from OpenAIAdapter.

    OpenAI's embeddings live in a different model namespace
    (`text-embedding-3-small`, `text-embedding-3-large`,
    `text-embedding-ada-002`) and on a separate endpoint
    (`/v1/embeddings`) from the Responses API. Wiring them onto the
    same adapter would conflate two different `concrete_model`
    concerns; a dedicated adapter keeps the binding clean:

        ctx.llm.embed(["text"], model="text-embedding-3-small")
            → resolves to OpenAIEmbeddingsAdapter via the alias map
            → calls client.embeddings.create(...)

    `generate` is unsupported on this adapter (and would route to
    `OpenAIAdapter` via the alias map anyway). `count_tokens` is
    deferred — the right surface is `tiktoken` locally, but our
    `ProviderAdapter.count_tokens` signature doesn't expose it.
    """

    provider_name = "openai-embeddings"

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
            self._client = make_async_openai(
                api_key=self._api_key, base_url=self._base_url
            )
        return self._client

    _classify = staticmethod(classify_openai_exception)

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
        raise NotImplementedError(
            "OpenAI embeddings adapter doesn't support generate; route "
            "chat / Responses requests to a `gpt-*` alias instead."
        )

    async def embed(self, text: str | list[str]) -> list[list[float]]:
        client = self._get_client()
        if isinstance(text, str):
            inputs: list[str] = [text]
        else:
            inputs = list(text)
        result = await client.embeddings.create(
            input=inputs,
            model=self.concrete_model,
        )
        return [list(d.embedding) for d in result.data]

    async def count_tokens(self, messages: list[Message]) -> int:
        raise NotImplementedError(
            "Token counting for OpenAI embeddings is best done locally "
            "via tiktoken; this provider doesn't expose a count endpoint."
        )
