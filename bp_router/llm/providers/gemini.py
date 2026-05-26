"""bp_router.llm.providers.gemini — Gemini provider adapter.

Wraps `google-genai` (deferred import). Translates neutral `Message`
to Gemini `Content` parts; honours `provider_options` for native
features (grounding, code execution, image/video generation, thinking
config, media resolution).

Multi-turn function calling on Gemini 3 requires `thought_signature`
to be round-tripped on the first function call of each step. We extract
signatures from response parts, ferry them through the neutral types,
and re-emit them when agents reconstruct the assistant turn. Same goes
for the `id` field on each function call → function response — we
preserve provider-supplied ids verbatim.
"""

from __future__ import annotations

import base64
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


def _gemini_retry_after(exc: BaseException) -> float | None:
    """Extract a retry hint from a Gemini `ResourceExhausted` error.

    Two SDK paths to handle:

    1. **google-api-core** (`google.api_core.exceptions.ResourceExhausted`):
       attaches structured `error_details.proto` messages on the
       `.details` property — a list of protos including `RetryInfo`.
       NOT `.errors`, which is a separate list of free-form error
       strings; reading `.errors` (the previous implementation)
       silently returned None on every 429 hint.

           exc.details = [..., RetryInfo(retry_delay=...), ...]
           retry_info.retry_delay.seconds + nanos / 1e9

    2. **google-genai** (`google.genai.errors.ClientError`):
       `.details` is the JSON-decoded response body (a dict), with
       a standard `google.rpc.Status` shape carrying RetryInfo as
       a duration string:

           {"error": {"code": 429, "details": [
               {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": "30s"}
           ]}}

    Best-effort: any inability to parse falls back to None and the
    exponential schedule kicks in. Returning None is always safe —
    the SDK retry loop has its own backoff cap.
    """
    details = getattr(exc, "details", None)
    if details is None:
        return None

    # Path 1 — proto list (google-api-core). Iterable of objects with
    # `.retry_delay` attributes, NOT a string / bytes / dict.
    if (
        not isinstance(details, (str, bytes, dict))
        and hasattr(details, "__iter__")
    ):
        for entry in details:
            retry_delay = getattr(entry, "retry_delay", None)
            if retry_delay is None:
                continue
            seconds = getattr(retry_delay, "seconds", None)
            nanos = getattr(retry_delay, "nanos", 0) or 0
            if seconds is None:
                continue
            try:
                return float(seconds) + float(nanos) / 1e9
            except (TypeError, ValueError):
                continue

    # Path 2 — JSON dict (google-genai). Walk the standard
    # `error.details[].retryDelay` shape.
    if isinstance(details, dict):
        return _parse_genai_retry_delay(details)

    return None


def _parse_genai_retry_delay(payload: dict[str, Any]) -> float | None:
    """Pull a `retryDelay: "Ns"` field out of a google-genai error
    payload. Returns None on any parse miss."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        # Some genai paths put `details` at the top level instead of
        # nested under `error`; tolerate that.
        error = payload
    details_arr = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details_arr, list):
        return None
    for entry in details_arr:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("retryDelay")
        if not isinstance(raw, str) or not raw:
            continue
        # Protobuf duration format: a decimal number with a trailing 's'.
        token = raw.rstrip("s").strip()
        try:
            return float(token)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Part translation (neutral → Gemini)
# ---------------------------------------------------------------------------


def _convert_part(part: dict[str, Any]) -> dict[str, Any]:
    """Translate one neutral content-part to Gemini's part schema.

    The SDK emits images via the neutral tag
    `{"image": {"mime_type": "...", "data": "<base64>"}}`. Function
    calls coming back round-trip as `{"function_call": {...},
    "thought_signature": "..."}` — we preserve the signature on the
    Gemini-native part, since multi-turn function calling requires it.

    Anthropic-only reasoning blocks (`{"type": "thinking", ...}`,
    `{"type": "redacted_thinking", ...}`) returned by
    `Message.assistant_from_response` for cross-provider portability
    are dropped here — Gemini doesn't accept them and would 400.

    Everything else (`{"text": ...}`, native `{"inline_data": ...}`,
    `{"file_data": ...}`) passes through verbatim.
    """
    if not isinstance(part, dict):
        return part

    # Drop Anthropic-only reasoning blocks. Empty dict is a no-op part
    # the caller filters out; signalled here so the message conversion
    # loop knows to skip it.
    if part.get("type") in {"thinking", "redacted_thinking"}:
        return {}

    # Neutral image OR document → Gemini inline_data. Both neutral
    # envelopes share Gemini's single shape — MIME drives upstream
    # interpretation. `display_name` (when set) maps onto the
    # `inline_data.display_name` field that Gemini 3+ uses for
    # `{"$ref": "<name>"}` substitution inside `function_response`
    # payloads (covers both image-result and document-result
    # multimodal function responses).
    for key in ("image", "document"):
        if key in part:
            blob = part[key]
            inline: dict[str, Any] = {
                "mime_type": blob.get("mime_type", "application/octet-stream"),
                "data": blob.get("data", ""),
            }
            if blob.get("display_name"):
                inline["display_name"] = blob["display_name"]
            return {"inline_data": inline}

    # Round-trip parts already shaped for Gemini (text, function_call,
    # function_response, inline_data, file_data) plus their optional
    # thought_signature companion.
    #
    # `thought_signature` arrived from us via `_encode_signature` (raw
    # SDK bytes → base64 str on the wire). The Gemini SDK on the
    # round-trip expects raw bytes — passing the base64 string back
    # silently breaks multi-turn function calling on Gemini 3 (the
    # SDK either drops the field or coerces it and corrupts the
    # encrypted blob, surfacing as upstream 400s on the second turn
    # of any tool-use sequence).
    #
    # Decode here so the SDK receives bytes. _decode_signature is
    # tolerant of already-bytes inputs (idempotent) so calling it on
    # a part that didn't go through wire-encode is harmless.
    if "thought_signature" in part:
        decoded = _decode_signature(part.get("thought_signature"))
        if decoded is None:
            # Drop the key entirely rather than send None — the SDK
            # treats None and missing differently in some versions
            # and missing is the safer null state.
            part = {k: v for k, v in part.items() if k != "thought_signature"}
        else:
            part = {**part, "thought_signature": decoded}
    return part


# ---------------------------------------------------------------------------
# Config assembly (neutral → GenerateContentConfig kwargs)
# ---------------------------------------------------------------------------


# Top-level GenerateContentConfig fields that the agent may set
# verbatim through `provider_options`. Anything else is either modelled
# as a first-class kwarg (temperature, max_tokens) or has its own
# translation path (thinking_config below; tools above).
_PASSTHROUGH_CONFIG_KEYS = (
    "safety_settings",
    "response_mime_type",
    "response_schema",
    "stop_sequences",
    "media_resolution",   # Gemini 3+: low | medium | high
    "response_modalities",
)


def _build_config_kwargs(
    *,
    tools: list[ToolSpec] | None,
    tool_choice: ToolChoice | None,
    temperature: float | None,
    max_tokens: int | None,
    provider_options: dict[str, Any] | None,
    system_instruction: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure assembly of GenerateContentConfig + ThinkingConfig kwargs.

    Returns `(cfg_kwargs, thinking_kwargs)`. The caller wraps each in
    its types class (or passes the dict through to the SDK, which
    accepts the dict shape too).

    Split out so it can be unit-tested without google-genai installed.
    """
    cfg_kwargs: dict[str, Any] = {}
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    if max_tokens is not None:
        cfg_kwargs["max_output_tokens"] = max_tokens
    if system_instruction:
        cfg_kwargs["system_instruction"] = system_instruction

    # Tool list: combine neutral ToolSpec functions + provider-specific
    # blocks from provider_options["tools"] (e.g. {"google_search": {}}).
    tool_blocks: list[Any] = []
    if tools:
        from bp_sdk.tools import gemini_strip_schema  # noqa: PLC0415

        tool_blocks.append(
            {
                "function_declarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": gemini_strip_schema(t.parameters),
                    }
                    for t in tools
                ]
            }
        )

    thinking_kwargs: dict[str, Any] = {}
    if provider_options:
        extra = provider_options.get("tools") or []
        tool_blocks.extend(extra)

        for k in _PASSTHROUGH_CONFIG_KEYS:
            if k in provider_options:
                cfg_kwargs[k] = provider_options[k]

        # ThinkingConfig assembly. Shape:
        #   GenerateContentConfig(
        #       thinking_config=ThinkingConfig(
        #           thinking_level="low" | "medium" | "high",  # Gemini 3
        #           thinking_budget=<int tokens>,              # 2.5+
        #           include_thoughts=<bool>,
        #       )
        #   )
        # The legacy `thinking_budget_tokens` key (which we used to
        # pass straight as a top-level kwarg — broken on the SDK) maps
        # to `thinking_budget` here.
        if "thinking_level" in provider_options:
            thinking_kwargs["thinking_level"] = provider_options["thinking_level"]
        if "thinking_budget" in provider_options:
            thinking_kwargs["thinking_budget"] = provider_options["thinking_budget"]
        elif "thinking_budget_tokens" in provider_options:
            thinking_kwargs["thinking_budget"] = provider_options["thinking_budget_tokens"]
        if "include_thoughts" in provider_options:
            thinking_kwargs["include_thoughts"] = provider_options["include_thoughts"]

    if tool_blocks:
        cfg_kwargs["tools"] = tool_blocks

    # tool_choice mapping (best-effort).
    if tool_choice == "required":
        cfg_kwargs["tool_config"] = {"function_calling_config": {"mode": "ANY"}}
    elif tool_choice == "none":
        cfg_kwargs["tool_config"] = {"function_calling_config": {"mode": "NONE"}}
    elif tool_choice == "auto":
        cfg_kwargs["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}
    elif isinstance(tool_choice, dict):
        cfg_kwargs["tool_config"] = tool_choice

    return cfg_kwargs, thinking_kwargs


# ---------------------------------------------------------------------------
# Thought signature codec
# ---------------------------------------------------------------------------


def _encode_signature(raw: Any) -> str | None:
    """Encode a thought_signature for transport (base64 str).

    Gemini returns the signature as raw `bytes` in the SDK part. Wire
    it across our protocol as base64 so it survives JSON serialisation.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw  # already encoded (or a debug placeholder)
    if isinstance(raw, (bytes, bytearray)):
        return base64.b64encode(bytes(raw)).decode("ascii")
    return None


def _decode_signature(encoded: Any) -> bytes | None:
    """Decode a transport-form signature back into bytes for the SDK."""
    if encoded is None:
        return None
    if isinstance(encoded, (bytes, bytearray)):
        return bytes(encoded)
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded)
        except (ValueError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"

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
                from google import genai  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "google-genai not installed; `pip install google-genai`"
                ) from exc
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                # Custom endpoint — regional Vertex / EU mirrors / a
                # corporate gateway. The google-genai SDK takes this
                # via http_options; leave it off for the default URL.
                from google.genai import types as _genai_types  # noqa: PLC0415

                kwargs["http_options"] = _genai_types.HttpOptions(
                    base_url=self._base_url
                )
            self._client = genai.Client(**kwargs)
        return self._client

    @staticmethod
    def _classify(exc: BaseException) -> RetryHint:
        """Map google-genai / google-api-core exceptions to typed
        `RetryHint`.

        google-genai surfaces upstream errors as `google.api_core.
        exceptions.*` in most paths and `google.genai.errors.*` in a
        few newer code paths. We match by class name (lazy import
        avoidance) — both module trees have stable class names.

        Mapping:
          ResourceExhausted   → upstream_rate_limited (429 / quota)
          DeadlineExceeded    → upstream_timeout
          ServiceUnavailable  → upstream_unavailable (503)
          InternalServerError → upstream_unavailable (500)
          Unauthenticated     → upstream_auth_failed (401)
          PermissionDenied    → upstream_auth_failed (403)
          InvalidArgument     → upstream_invalid_request (400)
          NotFound            → upstream_invalid_request (404)
          FailedPrecondition  → upstream_invalid_request (412)
        """
        cls_name = type(exc).__name__

        if cls_name == "ResourceExhausted":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                retry_after_seconds=_gemini_retry_after(exc),
                upstream_class=cls_name,
            )
        if cls_name == "DeadlineExceeded":
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
                upstream_class=cls_name,
            )
        if cls_name in ("ServiceUnavailable", "InternalServerError"):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
                upstream_class=cls_name,
            )
        if cls_name in ("Unauthenticated", "PermissionDenied"):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
                upstream_class=cls_name,
            )
        if cls_name in (
            "InvalidArgument",
            "NotFound",
            "FailedPrecondition",
            "AlreadyExists",
        ):
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
                upstream_class=cls_name,
            )

        # `google.genai.errors.{ClientError, ServerError, APIError}`
        # — the newer SDK's parallel hierarchy. They carry the HTTP
        # status code on `exc.code`. Bucket the same way the
        # google-api-core path does, just keyed on the integer
        # status. ClientError → 4xx, ServerError → 5xx, APIError
        # is the parent (rare but possible).
        if cls_name in ("ClientError", "ServerError", "APIError"):
            status = getattr(exc, "code", None)
            if status == 429:
                return RetryHint(
                    code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                    retry_after_seconds=_gemini_retry_after(exc),
                    upstream_class=cls_name,
                )
            if status in (401, 403):
                return RetryHint(
                    code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
                    upstream_class=cls_name,
                )
            if status == 408 or (
                isinstance(status, int) and 500 <= status < 600 and status != 503
            ):
                # 408 Request Timeout, generic 5xx that aren't 503
                # specifically. 503 falls through to the unavailable
                # branch below.
                return RetryHint(
                    code=(
                        ErrorCode.LLM_UPSTREAM_TIMEOUT
                        if status == 408
                        else ErrorCode.LLM_UPSTREAM_UNAVAILABLE
                    ),
                    upstream_class=cls_name,
                )
            if status == 503:
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

        contents, system_instruction = self._convert_messages(messages)
        config = self._build_config(
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_options=provider_options,
            system_instruction=system_instruction,
        )

        if stream:
            return self._generate_stream(client, contents, config)

        # Non-streaming.
        resp = await client.aio.models.generate_content(
            model=self.concrete_model,
            contents=contents,
            config=config,
        )
        return self._convert_response(resp)

    async def _generate_stream(
        self, client: Any, contents: Any, config: Any
    ) -> AsyncIterator[LlmDelta]:
        """Stream Gemini chunks as neutral LlmDeltas.

        Per the docs FAQ: "during a model response not containing a FC
        with a streaming request, the model may return the thought
        signature in a part with an empty text content part" — so we
        walk every part of every chunk rather than relying on
        `chunk.text` aggregation.
        """
        # google-genai migrated generate_content_stream from a function
        # returning an async iterator to a coroutine that returns one;
        # await first, then iterate. `inspect.iscoroutinefunction` is
        # True against this method under google-genai >= 1.14.
        stream = await client.aio.models.generate_content_stream(
            model=self.concrete_model,
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            usage = self._extract_usage(chunk)
            finish = self._extract_finish(chunk)

            for cand in getattr(chunk, "candidates", None) or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", None) or []:
                    text = getattr(part, "text", None)
                    is_thought = bool(getattr(part, "thought", False))
                    sig = _encode_signature(getattr(part, "thought_signature", None))
                    fc = getattr(part, "function_call", None)
                    tool_call: ToolCall | None = None
                    sig_for_delta: str | None = sig
                    if fc is not None:
                        tool_call = ToolCall(
                            id=getattr(fc, "id", "") or "",
                            name=getattr(fc, "name", ""),
                            args=dict(getattr(fc, "args", {}) or {}),
                            thought_signature=sig,
                        )
                        # Signature ferried on tool_call; don't
                        # double-emit at delta level.
                        sig_for_delta = None

                    if text or tool_call or sig_for_delta:
                        yield LlmDelta(
                            text=text,
                            tool_call=tool_call,
                            thought=is_thought,
                            thought_signature=sig_for_delta,
                        )

            # Usage / finish_reason are per-chunk metadata, not per-part.
            if usage or finish:
                yield LlmDelta(usage=usage, finish_reason=finish)

    # ------------------------------------------------------------------
    # embed / count_tokens
    # ------------------------------------------------------------------

    async def embed(
        self, text: str | list[str], *, provider_options: dict[str, Any] | None = None
    ) -> list[list[float]]:
        client = self._get_client()
        if isinstance(text, str):
            text = [text]
        # `output_dimensionality` (from the preset's provider_options) picks
        # the vector width — Gemini embeddings support 128–3072 (MRL) and
        # otherwise return the model's full default size. It must match the
        # suite's `embedding_dim` / the stored vector column. A plain dict is
        # accepted as the `config` (the SDK coerces it to EmbedContentConfig).
        config = None
        dim = (provider_options or {}).get("output_dimensionality")
        if dim is not None:
            config = {"output_dimensionality": dim}
        result = await client.aio.models.embed_content(
            model=self.concrete_model,
            contents=text,
            config=config,
        )
        # google-genai returns Embeddings — extract the .values lists.
        return [list(e.values) for e in result.embeddings]

    async def count_tokens(self, messages: list[Message]) -> int:
        client = self._get_client()
        contents, _ = self._convert_messages(messages)
        result = await client.aio.models.count_tokens(
            model=self.concrete_model, contents=contents
        )
        return int(getattr(result, "total_tokens", 0))

    # ------------------------------------------------------------------
    # Translation: messages → Gemini contents
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Turn neutral Messages into Gemini contents + system_instruction.

        `tool` messages become function_response parts. Gemini 3
        requires the matching call `id` on each response so the model
        can map results back to in-flight calls — we read it from
        `Message.tool_call_id`.
        """
        contents: list[dict[str, Any]] = []
        system_instruction: str | None = None
        for m in messages:
            if m.role == "system":
                # Gemini accepts a single system instruction string.
                if isinstance(m.content, str):
                    system_instruction = (
                        f"{system_instruction}\n{m.content}"
                        if system_instruction
                        else m.content
                    )
                continue
            if m.role == "tool":
                fr: dict[str, Any] = {"name": m.name or ""}
                if isinstance(m.content, str):
                    fr["response"] = {"result": m.content}
                elif isinstance(m.content, list):
                    # Multimodal tool result. Gemini 3+ accepts a
                    # `parts` array on function_response carrying
                    # `inline_data` for binary content. We translate
                    # each neutral part via `_convert_part` so a
                    # neutral image envelope becomes
                    # `{"inline_data": {...}}`. The legacy `response`
                    # field carries a short string stub plus any
                    # display_name `$ref`s for text fallback on
                    # adapters that ignore `parts`.
                    converted_parts: list[dict[str, Any]] = []
                    refs: list[str] = []
                    for p in m.content:
                        converted = _convert_part(p)
                        if not converted:
                            continue
                        converted_parts.append(converted)
                        inline = converted.get("inline_data")
                        if isinstance(inline, dict) and inline.get("display_name"):
                            refs.append(inline["display_name"])
                    fr["parts"] = converted_parts
                    fr["response"] = {
                        "result": (
                            "binary result; see function_response.parts"
                            if not refs
                            else {ref: {"$ref": ref} for ref in refs}
                        ),
                    }
                else:
                    fr["response"] = m.content
                # Gemini 3: include the matching call id for mapping.
                if m.tool_call_id:
                    fr["id"] = m.tool_call_id
                contents.append(
                    {
                        "role": "tool",
                        "parts": [{"function_response": fr}],
                    }
                )
                continue
            role = "user" if m.role == "user" else "model"
            if isinstance(m.content, str):
                parts: list[Any] = [{"text": m.content}]
            else:
                parts = [
                    converted
                    for converted in (_convert_part(p) for p in m.content)
                    if converted  # filter out dropped Anthropic-only parts
                ]
            # Skip empty assistant turns.
            # When a fallback round-trip carries Anthropic `thinking` /
            # `redacted_thinking` parts back to a Gemini preset, the
            # `_convert_part` filter above drops them — leaving
            # `parts == []`. Gemini rejects `{role: "model", parts: []}`
            # with a 400. Drop the whole turn rather than appending an
            # empty one; the conversation loses no semantic content
            # since the parts were already opaque-to-Gemini reasoning
            # blocks.
            if not parts:
                continue
            contents.append({"role": role, "parts": parts})
        return contents, system_instruction

    def _build_config(
        self,
        *,
        tools: list[ToolSpec] | None,
        tool_choice: ToolChoice | None,
        temperature: float | None,
        max_tokens: int | None,
        provider_options: dict[str, Any] | None,
        system_instruction: str | None,
    ) -> Any:
        cfg_kwargs, thinking_kwargs = _build_config_kwargs(
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_options=provider_options,
            system_instruction=system_instruction,
        )
        try:
            from google.genai import types as gtypes  # noqa: PLC0415
            if thinking_kwargs:
                cfg_kwargs["thinking_config"] = gtypes.ThinkingConfig(**thinking_kwargs)
            return gtypes.GenerateContentConfig(**cfg_kwargs)
        except Exception:
            # Dict fallback when the types module shape changes or
            # google-genai isn't importable (tests). The SDK accepts
            # the dict shape too.
            if thinking_kwargs:
                cfg_kwargs["thinking_config"] = thinking_kwargs
            return cfg_kwargs

    # ------------------------------------------------------------------
    # Translation: Gemini response → neutral types
    # ------------------------------------------------------------------

    def _convert_response(self, resp: Any) -> LlmResponse:
        """Walk every part of every candidate to assemble:

        - `text` — concatenation of parts where `thought` is falsy.
        - `thought_summary` — concatenation of parts where
          `thought=True` (only present when `include_thoughts=True`).
        - `tool_calls` — every function_call part, with `id` preserved
          verbatim and the per-call `thought_signature` attached.
        - `thought_signature` — signature on the LAST non-thought part
          (text or function_call). On Gemini 3 the first function call
          carries the only mandatory signature, so it lives on the
          tool_call entry; this top-level field captures the recommended
          (non-mandatory) signature on text-only responses.
        """
        text_parts: list[str] = []
        thought_text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        last_non_thought_signature: str | None = None
        first_tool_call_signature: str | None = None

        for cand in getattr(resp, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                is_thought = bool(getattr(part, "thought", False))
                sig = _encode_signature(getattr(part, "thought_signature", None))
                fc = getattr(part, "function_call", None)
                ptext = getattr(part, "text", None)

                if fc is not None:
                    tc = ToolCall(
                        id=getattr(fc, "id", "") or "",
                        name=getattr(fc, "name", ""),
                        args=dict(getattr(fc, "args", {}) or {}),
                        thought_signature=sig,
                    )
                    tool_calls.append(tc)
                    if first_tool_call_signature is None and sig:
                        first_tool_call_signature = sig
                    if not is_thought and sig:
                        last_non_thought_signature = sig
                    continue

                if ptext:
                    if is_thought:
                        thought_text_parts.append(ptext)
                    else:
                        text_parts.append(ptext)
                if not is_thought and sig:
                    last_non_thought_signature = sig

        usage = self._extract_usage(resp) or TokenUsage()
        finish = self._extract_finish(resp) or "stop"

        return LlmResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish if finish in {
                "stop", "length", "tool_calls", "content_filter", "error"
            } else "stop",
            usage=usage,
            raw=getattr(resp, "model_dump", lambda: {})() or {},
            thought_summary="".join(thought_text_parts) or None,
            thought_signature=last_non_thought_signature,
        )

    def _extract_usage(self, resp: Any) -> TokenUsage | None:
        meta = getattr(resp, "usage_metadata", None)
        if meta is None:
            return None
        # `cached_content_token_count` is google-genai's attribute for
        # the "input tokens that hit the context cache" — the read
        # half of cache accounting.
        # The write half (cache-creation tokens) isn't surfaced as a
        # discrete field on Gemini's UsageMetadata; for hosted Gemini
        # cache creation cost is folded into the regular input_tokens
        # billing. Anthropic and OpenAI both surface
        # cache_read_tokens, so without this Gemini was the lone
        # adapter under-reporting on cached prompts — a real billing /
        # quota gap once an agent suite leans on context caching.
        return TokenUsage(
            input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
            thoughts_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
            cache_read_tokens=int(
                getattr(meta, "cached_content_token_count", 0) or 0
            ),
        )

    def _extract_finish(self, resp: Any) -> str | None:
        cand = (getattr(resp, "candidates", None) or [None])[0]
        fr = getattr(cand, "finish_reason", None) if cand else None
        if fr is None:
            return None
        # Gemini returns enum-ish values; coerce to a known one.
        s = str(fr).lower()
        if "stop" in s:
            return "stop"
        if "max_tokens" in s or "length" in s:
            return "length"
        if "function" in s or "tool" in s:
            return "tool_calls"
        if "safety" in s or "blocked" in s:
            return "content_filter"
        return "stop"
