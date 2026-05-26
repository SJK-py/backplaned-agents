"""bp_router.llm.providers.openai_compatible — Chat Completions adapter
for OpenAI-compatible local LLM servers (vLLM, LM Studio, llama.cpp's
``--server``, text-generation-inference, Ollama in OpenAI-mode, etc.).

Why a separate adapter from `bp_router.llm.providers.openai`:

1. **Endpoint.** Local servers near-universally implement the older
   ``/v1/chat/completions`` API. Few implement OpenAI's newer
   ``/v1/responses``. Picking Chat Completions is the
   lowest-common-denominator that "just works" across the local
   ecosystem.

2. **Capabilities.** Reasoning round-trip blocks, encrypted reasoning
   tokens, the `include` parameter, server-side tool nodes
   (``web_search`` / ``file_search`` / ``code_interpreter``), and
   structured-outputs (``response_format``) are OpenAI-specific
   surfaces. Most local servers don't implement them; we don't try to
   ferry them through.

3. **API key**. Local servers typically don't authenticate. The
   OpenAI SDK requires *some* string, so the service defaults to
   ``"EMPTY"`` for these adapters when no key is configured.

What's wired:
  - non-streaming `generate` (text + tools + vision)
  - streaming `generate` (text + tool calls; finish reason)
  - `embed` (sibling adapter `OpenAICompatibleEmbeddingsAdapter`
    for ``/v1/embeddings``)

What's NOT wired:
  - ``count_tokens``: no universal endpoint across local servers.
    Raises ``NotImplementedError``. Agents that need a budget should
    keep a running tally from streamed deltas, or call the hosted
    OpenAI ``count_tokens`` adapter for an estimate.
  - reasoning round-trip: most local models surface reasoning at the
    text level (``<think>...</think>``) without protocol round-trip.
    Falls through into the answer text; agents can split if needed.
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


# Top-level chat-completions kwargs we pass through from
# `provider_options`. Local servers' support varies — pass the whole
# set; the upstream will ignore unknown kwargs or 400 loudly.
_PASSTHROUGH_KWARGS = (
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "seed",
    "n",
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "user",
    "response_format",
    "parallel_tool_calls",
)


# ---------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------


def _convert_user_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a neutral content-part to a chat-completions content
    block. Returns None to drop the part."""
    if not isinstance(part, dict):
        return part
    t = part.get("type")
    # Already-native chat content (e.g., {"type": "text", "text": ...}
    # or {"type": "image_url", "image_url": {"url": ...}}).
    if t in ("text", "image_url"):
        return part
    # Neutral image → image_url with data URL.
    if "image" in part:
        img = part["image"]
        mime = img.get("mime_type", "image/jpeg")
        data = img.get("data", "")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}"},
        }
    # Neutral document → OpenAI-compatible Chat Completions
    # `file` content part with a base64 data URL. Most OpenAI-
    # compatible servers (vLLM, llama.cpp, etc.) follow the same
    # shape as OpenAI's Responses API for input_file. Older
    # servers that don't recognise the type will reject — caller
    # responsibility.
    if "document" in part:
        doc = part["document"]
        mime = doc.get("mime_type", "application/pdf")
        data = doc.get("data", "")
        return {
            "type": "file",
            "file": {
                "filename": doc.get("display_name") or "document.pdf",
                "file_data": f"data:{mime};base64,{data}",
            },
        }
    # Neutral text part.
    if "text" in part:
        return {"type": "text", "text": part["text"]}
    # Drop reasoning blocks from other providers — they're meaningless
    # to a local server and would confuse it.
    if t in ("thinking", "redacted_thinking", "reasoning"):
        return None
    return part


def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate neutral `Message` list to chat-completions ``messages``.

    System messages stay inline (chat completions has no
    ``instructions=`` separation). Assistant messages with tool calls
    are preserved with the tool_calls array. Tool-result messages use
    role=tool with tool_call_id.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "tool":
            # Chat-completions `role="tool"` content is a string. For
            # multimodal tool results we emit the tool message with a
            # sentinel summary AND a synthesized follow-up user
            # message carrying the actual parts (image_url /
            # text) so vision-capable models see the images.
            # Non-vision-capable endpoints will ignore or 4xx — same
            # outcome as a user message with an image part today.
            if isinstance(msg.content, list):
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "",
                    "content": (
                        f"[binary payload from {msg.name or 'tool'} "
                        "tool result follows in next user message]"
                    ),
                })
                converted = [_convert_user_part(p) for p in msg.content]
                content_parts = [c for c in converted if c]
                if content_parts:
                    out.append({"role": "user", "content": content_parts})
                continue
            content = (
                msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            )
            out.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id or "",
                "content": content,
            })
            continue

        if msg.role == "assistant":
            # Assistant content can be a string OR a list with embedded
            # tool_call markers. Split tool calls out into the
            # `tool_calls` array; flatten remaining text.
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            if isinstance(msg.content, str):
                text_parts.append(msg.content)
            else:
                for part in msg.content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_use":
                        # Anthropic-native shape.
                        tool_calls.append({
                            "id": part.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": part.get("name") or "",
                                "arguments": json.dumps(
                                    part.get("input") or {}
                                ),
                            },
                        })
                    elif "function_call" in part:
                        # Neutral / SDK round-trip shape, as emitted by
                        # `Message.assistant_from_response` (Gemini/SDK
                        # style: `{"function_call": {"id", "name",
                        # "args"}}`). Pre-R8 this fell through every
                        # branch — neither `type=="tool_use"` nor a
                        # `"text"` key — so a cross-provider OR
                        # SDK-helper-built assistant turn carrying tool
                        # calls was DROPPED entirely (empty content +
                        # no tool_calls → the no-info-turn skip below).
                        # The following `role="tool"` message then
                        # referenced a call the model never saw,
                        # corrupting the conversation. Mirror
                        # openai.py's neutral-function_call arm.
                        fc = part["function_call"]
                        fc_args = fc.get("args")
                        if fc_args is None:
                            fc_args = fc.get("input") or {}
                        tool_calls.append({
                            "id": fc.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": fc.get("name") or "",
                                "arguments": (
                                    fc_args
                                    if isinstance(fc_args, str)
                                    else json.dumps(fc_args)
                                ),
                            },
                        })
                    elif "text" in part:
                        text_parts.append(part["text"])
            entry: dict[str, Any] = {"role": "assistant"}
            text = "".join(text_parts)
            if text:
                entry["content"] = text
            elif tool_calls:
                # Chat completions accepts content=null when tool_calls present.
                entry["content"] = None
            else:
                # Empty assistant content with NO tool_calls is a no-
                # information turn. Most chat-completions servers
                # accept it but a few (vLLM strict mode, llama.cpp's
                # server, several local model wrappers) 400 on it.
                # Skip rather than emit; the message would carry no
                # signal even if accepted.
                continue
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        # System / user / developer (developer→system for local servers).
        role = "system" if msg.role == "developer" else msg.role
        if isinstance(msg.content, str):
            # Drop empty system/user messages — strict chat-
            # completions servers (vLLM, llama.cpp) reject
            # `{"role": "user", "content": ""}` and an empty
            # system message is meaningless. Routers should
            # never feed these; the skip is defence-in-depth.
            if not msg.content:
                continue
            out.append({"role": role, "content": msg.content})
            continue
        # List content: convert each part; drop Nones.
        parts: list[dict[str, Any]] = []
        for raw in msg.content:
            converted = _convert_user_part(raw)
            if converted is not None:
                parts.append(converted)
        # If the resulting list is purely text-only, collapse to a flat
        # string (more compatible with stricter local servers).
        if all(p.get("type") == "text" for p in parts):
            joined = "".join(p.get("text", "") for p in parts)
            # Same empty-content guard as the str branch above —
            # after collapsing, an all-empty-text list becomes "".
            if not joined:
                continue
            out.append({"role": role, "content": joined})
        else:
            if not parts:
                continue
            out.append({"role": role, "content": parts})
    return out


# ---------------------------------------------------------------------------
# Tool translation
# ---------------------------------------------------------------------------


def _convert_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _convert_tool_choice(
    tool_choice: ToolChoice | None,
) -> str | dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, dict):
        # Caller supplied a native chat-completions shape — pass through.
        return tool_choice
    if tool_choice in ("auto", "none", "required"):
        return tool_choice
    # Treat anything else as a forced function-name choice.
    return {"type": "function", "function": {"name": str(tool_choice)}}


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------


def _usage_from_chat(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


def _finish_reason(raw: str | None) -> str:
    """Map upstream finish_reason to our neutral set.

    OpenAI/most local servers emit ``stop`` | ``length`` | ``tool_calls``
    | ``content_filter`` | ``function_call`` (legacy). vLLM occasionally
    emits ``"abort"`` on cancel — fall through to ``error``.
    """
    if raw in ("stop", "length", "tool_calls", "content_filter"):
        return raw
    if raw == "function_call":
        return "tool_calls"
    if raw is None:
        return "stop"
    return "error"


def _convert_response(resp: Any) -> LlmResponse:
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return LlmResponse(text="", finish_reason="stop")
    choice = choices[0]
    msg = getattr(choice, "message", None)
    text = ""
    tool_calls: list[ToolCall] = []
    if msg is not None:
        # `content` can be a plain string or a list (some servers)
        c = getattr(msg, "content", None)
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else ""
                for part in c
            )
        for tc in getattr(msg, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            args_raw = getattr(fn, "arguments", "") if fn else ""
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                # Some local servers stream slightly malformed JSON or
                # non-JSON args. Surface raw text rather than crashing.
                args = {"_raw": args_raw}
            tool_calls.append(
                ToolCall(
                    id=getattr(tc, "id", "") or "",
                    name=getattr(fn, "name", "") if fn else "",
                    args=args,
                )
            )
    return LlmResponse(
        text=text,
        tool_calls=tool_calls,
        finish_reason=_finish_reason(getattr(choice, "finish_reason", None)),
        usage=_usage_from_chat(getattr(resp, "usage", None)),
        raw=resp.model_dump() if hasattr(resp, "model_dump") else {},
    )


# ---------------------------------------------------------------------------
# Adapter: chat
# ---------------------------------------------------------------------------


class OpenAICompatibleAdapter(ProviderAdapter):
    """Chat Completions adapter for OpenAI-compatible local servers."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        concrete_model: str,
        api_key: str,
        base_url: str,
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

    # Reuses the shared OpenAI-SDK classifier — local servers
    # (vLLM, LM Studio, etc.) raise the same exception types via
    # the same `openai` SDK.
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
        resp = await client.chat.completions.create(**kwargs)
        return _convert_response(resp)

    async def _generate_stream(
        self, client: Any, kwargs: dict[str, Any]
    ) -> AsyncIterator[LlmDelta]:
        """Translate streamed chat-completion chunks into neutral deltas.

        Chunk shape (per OpenAI / vLLM / LM Studio / llama.cpp-server):
          { "choices": [{
              "delta": {
                "role"?: "assistant",
                "content"?: "...",
                "tool_calls"?: [
                  {"index": 0, "id"?: "...", "function": {
                    "name"?: "...", "arguments"?: "..."}}
                ],
              },
              "finish_reason"?: "stop"|"length"|"tool_calls"|...
            }],
            "usage"?: {...}   # only on final chunk if include_usage
          }

        Tool calls arrive piecewise: a `tool_calls[i].function.arguments`
        string accumulates over multiple chunks. We buffer per index and
        emit a `LlmDelta(tool_call=...)` only when the upstream signals
        finish_reason=tool_calls (or the stream ends).
        """
        kwargs = {**kwargs, "stream": True}
        # Many local servers don't send usage in streamed chunks unless
        # asked. Hint via stream_options where supported; ignored elsewhere.
        kwargs.setdefault("stream_options", {"include_usage": True})

        tool_buffers: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: TokenUsage | None = None

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            for ch in choices:
                delta = getattr(ch, "delta", None)
                fr = getattr(ch, "finish_reason", None)
                if fr is not None:
                    finish_reason = fr

                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    yield LlmDelta(text=content)
                elif isinstance(content, list):
                    # Some servers chunk content as parts; flatten text.
                    flat = "".join(
                        p.get("text", "") if isinstance(p, dict) else ""
                        for p in content
                    )
                    if flat:
                        yield LlmDelta(text=flat)

                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(tc, "index", 0) or 0
                    buf = tool_buffers.setdefault(
                        idx, {"id": "", "name": "", "args": ""}
                    )
                    if getattr(tc, "id", None):
                        buf["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            buf["name"] = fn.name
                        args_chunk = getattr(fn, "arguments", None)
                        if isinstance(args_chunk, str):
                            buf["args"] += args_chunk

            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = _usage_from_chat(chunk_usage)

        # Flush completed tool calls.
        mapped_finish = _finish_reason(finish_reason)
        # A stream cut off mid-arguments (`length` / `content_filter`
        # / `error`) leaves `buf["args"]` as truncated JSON. Emitting
        # it as `ToolCall(args={"_raw": "<partial>"})` is worse than
        # emitting nothing — the agent would attempt to execute a
        # tool with a bogus `{"_raw": ...}` payload. On a truncated
        # finish, drop unparseable buffers and let the finish_reason
        # signal the truncation. On a clean `stop`/`tool_calls`
        # finish the server CLAIMED completion, so a still-unparseable
        # buffer is a genuinely malformed server response — keep the
        # `{"_raw": ...}` surfacing there so it's at least visible.
        truncated = mapped_finish in ("length", "content_filter", "error")
        for _, buf in sorted(tool_buffers.items()):
            raw_args = buf["args"]
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                if truncated:
                    logger.warning(
                        "openai_compat_dropped_truncated_tool_call",
                        extra={
                            "event": "openai_compat_dropped_truncated_tool_call",
                            "finish_reason": mapped_finish,
                            "tool_name": buf.get("name") or "",
                        },
                    )
                    continue
                args = {"_raw": raw_args}
            yield LlmDelta(
                tool_call=ToolCall(id=buf["id"], name=buf["name"], args=args),
            )

        yield LlmDelta(
            finish_reason=mapped_finish,
            usage=usage,
        )

    # ------------------------------------------------------------------
    # embed / count_tokens
    # ------------------------------------------------------------------

    async def embed(self, text: str | list[str]) -> list[list[float]]:
        # Embeddings live on the sibling adapter — agents pick the right
        # provider. Routing here would conflate two different
        # `concrete_model` namespaces (chat vs embedding model ids).
        raise NotImplementedError(
            "openai-compatible adapter doesn't support embed; use the "
            "openai-compatible-embeddings provider instead."
        )

    async def count_tokens(self, messages: list[Message]) -> int:
        # No universal token-count endpoint on local servers.
        raise NotImplementedError(
            "openai-compatible adapter doesn't support count_tokens. "
            "Either count from streamed usage deltas, or route the "
            "estimate to a hosted provider's count_tokens."
        )


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
    kwargs: dict[str, Any] = {
        "model": concrete_model,
        "messages": _convert_messages(messages),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    converted_tools = _convert_tools(tools)
    if converted_tools:
        kwargs["tools"] = converted_tools
    converted_choice = _convert_tool_choice(tool_choice)
    if converted_choice is not None:
        kwargs["tool_choice"] = converted_choice
    if provider_options:
        for k in _PASSTHROUGH_KWARGS:
            if k in provider_options:
                kwargs[k] = provider_options[k]
        # Anything else under a special `extra_body` key gets forwarded
        # raw — vLLM uses this for sampling extensions like top_k,
        # repetition_penalty, etc.
        if "extra_body" in provider_options:
            kwargs["extra_body"] = provider_options["extra_body"]
    return kwargs


# ---------------------------------------------------------------------------
# Adapter: embeddings
# ---------------------------------------------------------------------------


class OpenAICompatibleEmbeddingsAdapter(ProviderAdapter):
    """Embeddings adapter for OpenAI-compatible local servers
    (``/v1/embeddings``). vLLM and LM Studio both expose this with the
    same shape as OpenAI's official endpoint."""

    provider_name = "openai-compatible-embeddings"

    def __init__(
        self,
        *,
        concrete_model: str,
        api_key: str,
        base_url: str,
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
            "openai-compatible-embeddings adapter doesn't support generate; "
            "route chat requests to an openai-compatible preset instead."
        )

    async def embed(self, text: str | list[str]) -> list[list[float]]:
        client = self._get_client()
        inputs: list[str] = [text] if isinstance(text, str) else list(text)
        resp = await client.embeddings.create(
            model=self.concrete_model, input=inputs
        )
        return [item.embedding for item in resp.data]

    async def count_tokens(self, messages: list[Message]) -> int:
        raise NotImplementedError(
            "openai-compatible-embeddings adapter doesn't support count_tokens."
        )
