# SDK — Services and Examples

> Part 2 of the agent SDK design. Covers the LLM service, file-store
> handling, progress emission, cancellation, tool builders, embedded
> vs. external deployment, testing helpers, and worked agent examples.
> See [`core.md`](./core.md) for the agent surface and dispatch model.

## 1. LLM service (`ctx.llm`)

The LLM bridge is promoted from "embedded agent" to a first-class
SDK service. Handlers call it directly, never through a peer
`spawn`. Centralised here for telemetry, quota enforcement, caching,
and consistent error mapping across providers.

```python
class LLMService:
    async def generate(
        self,
        prompt: str | list[Message],
        *,
        model: str = "default",
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        provider_options: dict[str, Any] | None = None,
    ) -> LLMResponse | AsyncIterator[LLMDelta]: ...

    async def embed(self, text: str | list[str], *,
                    model: str = "default") -> list[list[float]]: ...

    async def count_tokens(self, prompt: str | list[Message], *,
                           model: str = "default") -> int: ...
```

### 1.1 Provider routing — presets

The LLM service maps **preset** names to bundled
`(provider, concrete_model, api_key_ref, sampling defaults,
provider_options defaults, min_user_level)` configurations. Agent
code never sees raw API keys, never picks providers by name, and
doesn't repeat per-call configuration:

```python
# The recommended call shape — all the provider config bundled by name.
await ctx.llm.generate(messages, preset="quick-chat", stream=True)

# Equivalent legacy call (kept for back-compat).
await ctx.llm.generate(messages, model="quick-chat", stream=True)
```

Presets live in the `llm_presets` DB table and are admin-managed via
the webUI (`/admin/llm/presets`) or the `/v1/admin/llm/presets`
JSON API. On first router startup the table is auto-seeded with
default presets that match the pre-preset alias map exactly, so
existing agents using `model="..."` keep working unchanged. The seed
list is a **commentable JSONC catalogue** —
`bp_router/llm/presets_catalog.jsonc` (bundled), or the file named by
`ROUTER_LLM_PRESET_CATALOG_PATH` — so the model list is easy to maintain
as models change. The catalogue is only read on first boot (empty table)
and as the in-memory fallback; once seeded, edits go through the admin
surface, not the file.

**Tier gate.** Each preset carries a `min_user_level` that gates
which callers may use it. The grammar matches ACL rules:

| Required | Admits |
| --- | --- |
| `*` | any caller (default for built-in presets) |
| `admin` / `service` | exact match |
| `tierN` | tier N or stricter (lower number = more privileged) |

The router resolves the calling user's level via the JWT carried on
the originating task admit, caches it for 60 seconds, and rejects
disallowed calls with `LlmResultFrame.error.code = "preset_not_allowed"`.

**Override semantics.** Call-time kwargs override preset defaults:

| Kwarg | Override behaviour |
| --- | --- |
| `temperature`, `max_tokens` | Call-time wins when set; otherwise preset default flows through. `0.0` and `0` count as "set". **Caveat (Gemini 2.5+ / Anthropic extended thinking)**: `max_tokens` is the TOTAL budget the model splits between hidden thoughts and visible output. A small cap (e.g. 256) gets eaten almost entirely by thoughts on creative prompts and the visible answer truncates with `finish_reason="length"`. Leave `max_tokens=None` to let the provider default apply, or raise it well above the thinking estimate. Surface `response.usage.thoughts_tokens` to see the split. See `examples/test_drive/gemini_agent.py` for the recommended shape. |
| `provider_options` | Call-time **replaces** the preset's default dict entirely (not merged). To keep some defaults, spread them yourself at call time. |
| `tools`, `tool_choice` | Always call-time only — no preset default. |

**Errors.** Two new `LlmResultFrame.error.code` values from preset
resolution:

| Code | When | Retriable? |
| --- | --- | --- |
| `preset_unknown` | The agent referenced a preset name not in the table. | no |
| `preset_not_allowed` | The caller's user level doesn't satisfy the preset's `min_user_level`. | no |
| `auth_lookup_failed` | DB unreachable while resolving the caller's user_level for a tier-gated preset. | yes |
| `upstream_timeout` | Provider timed out (`APITimeoutError`, `DeadlineExceeded`, `APIConnectionError`). | **yes** |
| `upstream_rate_limited` | 429 / `RateLimitError`. `error.retry_after_seconds` mirrors the upstream's `Retry-After`. | **yes** |
| `upstream_unavailable` | Provider 5xx (`InternalServerError`, `ServiceUnavailable`, Anthropic `OverloadedError`). | **yes** |
| `upstream_invalid_request` | 400 / `BadRequestError` / `InvalidArgument` — bad prompt, oversized message, malformed tool spec. | no |
| `upstream_auth_failed` | 401 / 403 — wrong API key, expired credential. | no |
| `upstream_content_filter` | Provider blocked the prompt or response on content-policy grounds. | no |
| `upstream_quota_exhausted` | Account-level quota out (admin must rotate). | no |
| `stream_interrupted` | Connection dropped mid-stream after deltas had been delivered (PR #3). | no |
| `internal_error` | Unclassified router-side or upstream failure (catch-all bucket). | yes |

The `LlmResultFrame.error` object also carries a `retriable: bool`
flag (auto-derived from `code`) and `retry_after_seconds` when
applicable. SDK retry logic switches on the flag without enumerating
codes; ops dashboards filter by code for cause breakdowns. See
`docs/router/protocol.md` §5 for the full wire shape.

**Per-provider exception classification.** Each adapter has a
`_classify(exc) -> RetryHint` static method that maps SDK-native
exception types into the typed codes above. Class-name match
(rather than `isinstance`) keeps the SDKs deferred dependencies —
the classifier never imports `openai` / `anthropic` /
`google-genai`. Stable across recent SDK versions; future renames
fall through to `internal_error` (still retriable), so transient
failures from a brand-new exception class still get at least one
router-side retry.

**Coverage matrix** (rows are typed `LLM_*` codes; cells list the
provider exception class names that map there). The OpenAI column
also covers the four OpenAI-SDK adapters: `openai`,
`openai-embeddings`, `openai-compatible`,
`openai-compatible-embeddings`.

| Typed code | OpenAI SDK | Anthropic SDK | Google `genai` / `api_core` |
| --- | --- | --- | --- |
| `upstream_rate_limited` | `RateLimitError`; `APIStatusError(429)` | `RateLimitError`; `APIStatusError(429)` | `ResourceExhausted`; `ClientError`/`APIError(code=429)` |
| `upstream_timeout` | `APITimeoutError`, `APIConnectionError`, `APIResponseValidationError` | `APITimeoutError`, `APIConnectionError`, `APIResponseValidationError` | `DeadlineExceeded`; `ClientError(code=408)` |
| `upstream_unavailable` | `InternalServerError`; `APIStatusError(5xx)` | `InternalServerError`, `OverloadedError`; `APIStatusError(5xx incl. 529)` | `ServiceUnavailable`, `InternalServerError`; `ServerError(code=5xx)` |
| `upstream_auth_failed` | `AuthenticationError`, `PermissionDeniedError`, `OAuthError` | `AuthenticationError`, `PermissionDeniedError` | `Unauthenticated`, `PermissionDenied`; `ClientError(code=401\|403)` |
| `upstream_invalid_request` | `BadRequestError`, `UnprocessableEntityError`, `ConflictError`, `NotFoundError`, `LengthFinishReasonError`; `APIStatusError(other 4xx)` | `BadRequestError`, `UnprocessableEntityError`, `NotFoundError`, `ConflictError`; `APIStatusError(other 4xx)` | `InvalidArgument`, `NotFound`, `FailedPrecondition`, `AlreadyExists`; `ClientError(other 4xx)` |
| `upstream_content_filter` | `ContentFilterFinishReasonError` | — (model-side filter manifests as `BadRequestError`) | — (Gemini's `BlockedPromptError` not yet classified) |
| `internal_error` (default) | any other / unknown | any other / unknown | any other / unknown |

Notes on the cells:

- **OpenAI `BadRequestError`** is genuinely overloaded — vLLM and
  LM Studio return 400 on content-policy blocks too. Without
  inspecting the body we can't disambiguate, so we land in the
  general `upstream_invalid_request` bucket. Operators looking
  specifically for content-policy blocks should match on the
  `upstream_class` field of the `LlmResultError` (telemetry only;
  not used for routing) plus the `message` text.
- **OpenAI finish-reason exceptions** (`LengthFinishReasonError`,
  `ContentFilterFinishReasonError`) come from `client.parse(...)`
  and the structured-output helpers when the model stops on a
  finish reason the helper considers an error rather than a normal
  stop. Both are TERMINAL — retrying just burns tokens for the same
  outcome. `LengthFinishReasonError` buckets as
  `upstream_invalid_request` (caller should raise `max_tokens` or
  shrink the schema); `ContentFilterFinishReasonError` is the only
  classifier today that emits the typed `upstream_content_filter`
  code.
- **`APIStatusError` parent fallback** (OpenAI + Anthropic) catches
  CDN-fronted responses where an intermediary swaps the typed
  exception for a generic status error — Cloudflare 530s, Akamai
  504s, the WAF returning 429 without a body the SDK can match on.
  Bucketed by `getattr(exc, "status_code", None)`. Same for
  `google.genai.errors.{ClientError, ServerError, APIError}` keyed
  on `exc.code`.
- **`APIResponseValidationError`** — the SDK raises this when the
  response body fails its Pydantic validation (proxy injecting an
  HTML error page, partial JSON from a flaky endpoint, etc.).
  Treated as transient timeout-class so the bounded retry kicks
  in; one retry against the official endpoint usually clears
  proxy weirdness.
- **Anthropic `OverloadedError`** is the typed surface for the 529
  "Overloaded" response. Older SDK versions exposed it as a generic
  `InternalServerError`; recent versions can also surface it via
  the `APIStatusError(status_code=529)` parent when an intermediary
  is involved. All three paths map to `upstream_unavailable` so the
  alert PromQL keeps working across SDK upgrades.
- **Gemini's 412 / 409** (`FailedPrecondition` / `AlreadyExists`)
  show up on file-API and tuning-job paths most agents never hit;
  they're listed here for completeness and bucket as
  `upstream_invalid_request` because retrying won't help.
- **`google.genai.errors.*`** (the newer SDK's parallel hierarchy)
  carry HTTP status on `.code` rather than `.status_code`, and
  their `.details` is a JSON-decoded response body rather than a
  list of protos — both shapes are handled by the classifier.

**`Retry-After` extraction.** When the classifier maps a class to
`upstream_rate_limited`, it also tries to populate
`error.retry_after_seconds` so SDK retry policy can honour the
provider's hint verbatim instead of falling back to the
exponential schedule. The wire-level value is capped at the
policy's `max_backoff_s` (default 10s) so a misconfigured upstream
returning `Retry-After: 3600` can't pause a request for an hour.

Per-provider parsing:

| Provider | Source attribute | Format handled |
| --- | --- | --- |
| OpenAI / openai-compatible | `exc.response.headers["retry-after"]` (httpx Headers mapping) | Both spec-permitted forms: delta-seconds (`Retry-After: 5`) AND HTTP-date (`Retry-After: Fri, 31 Dec 1999 23:59:59 GMT`). Date form matters for WAF-fronted endpoints (Cloudflare, Akamai) that ban-list with multi-minute waits in the date format. Past dates clamp to 0; the policy `max_backoff_s` cap (default 10s) keeps extreme waits bounded. |
| Anthropic | `exc.response.headers["retry-after"]` (same shape as OpenAI — both wrap httpx) | Same parser, both forms. |
| Gemini (google-api-core) | `exc.details[*].retry_delay.{seconds,nanos}` (google-api-core `RetryInfo` proto on `.details`, NOT `.errors` — the latter is a list of free-form error strings) | integer seconds + nanos for sub-second precision. |
| Gemini (google-genai) | `exc.details["error"]["details"][*].retryDelay` (JSON `google.rpc.Status` shape, e.g. `"30s"`) | protobuf duration string format; trailing `s` stripped, parsed as a float. |

The OpenAI and Anthropic parsers share a single
`parse_http_retry_after` helper in
`bp_router.llm.retry_classification` — both SDKs wrap httpx
identically, so the parsing logic was duplicated and is now DRY.

All three parsers are best-effort. Any inability to find or parse
the hint returns None, and the SDK falls back to the schedule —
which is itself capped at 10s, so the worst case is a slightly
shorter wait than the upstream wanted.

**Inline `api_key`.** Each preset has both an `api_key_ref` (e.g.
`env://OPENAI_API_KEY`) and an optional inline `api_key`. When the
inline value is set it wins; otherwise the router resolves the ref
via `bp_router.security.secrets.resolve_secret_ref`. Inline keys are
useful when one preset needs to use a *different* account than the
default env var — e.g. routing `gpt-5.5-pro` to a high-tier billing
account while the rest of the OpenAI presets share the org-default
key. The admin API never returns the inline value; instead each row
carries `has_api_key: bool` so admins can see which presets ship
their own key. Setting a new value via PATCH replaces the existing
one. To remove an inline key, send `clear_api_key: true`.

**Retry + fallback chain.** Each preset has `max_retries` (0..10) and
optional `fallback_preset` (another preset name). On a non-streaming
call (`generate(stream=False)`, `embed`, `count_tokens`):

1. The router tries the requested preset up to `max_retries+1` times.
2. If all attempts fail and `fallback_preset` is set, it walks to
   that preset and starts the same retry loop. Walks transitively
   along the chain.
3. The chain ends at a preset with no `fallback_preset`, or at an
   unknown name (treated as terminal — no further fallback).
4. If the whole chain is exhausted, the **last** error is re-raised.

Cycle protection: the chain is a singly-linked DAG. Cycles are
rejected at save time (admin API returns 400) and again at load time
(startup logs `llm_preset_fallback_cycle` and keeps the previous
in-memory map). The runtime walker also caps walks via a `seen` set
so direct in-memory mutation can't infinite-loop.

Tier gate on fallback targets:

- The **first** preset (the one the caller explicitly asked for) is
  tier-gated; failure surfaces `preset_not_allowed` immediately.
  We never silently route a denied caller to a different preset.
- **Mid-chain** fallback targets are tier-gated too, but a denial
  silently *skips* that target and walks to its own fallback. This
  lets admins put a permissive preset upstream of a restricted one,
  knowing low-tier callers will fall past it.

**Streaming retry semantics** (M6 PR #3):

- The **fallback chain** is bypassed for streaming. Walking to a
  different preset mid-stream would silently switch the agent to a
  different model — confusing for both UI display and round-trip
  consistency.
- The **same preset's `max_retries`** still applies, but only
  **before the first delta** has been yielded. Once any chunk has
  reached the agent, subsequent failures raise
  `stream_interrupted` (NOT retriable — the agent has partial
  output and we can't safely re-issue).
- During the wait between setup-retry attempts, the wrapper emits
  a status-only `LlmDelta(meta={"kind": "retry_pending", ...})`
  so UI clients can show a spinner during the backoff. Mutex with
  content fields per the protocol — `text` / `tool_call` / etc.
  stay None on meta deltas.
- The first preset's tier gate still applies.

```jsonc
// Wire example: streaming setup-retry between two failed attempts
{"type":"LlmDelta","meta":{"kind":"retry_pending","attempt":1,
                           "max_attempts":3,"retry_after_seconds":2.0,
                           "reason_code":"upstream_rate_limited"}}
// ... backoff wait ...
{"type":"LlmDelta","text":"Hello "}      // first content chunk
{"type":"LlmDelta","text":"world"}       // subsequent chunks
{"type":"LlmDelta","finish_reason":"stop"}
```

**Built-in aliases:**

| Alias | Provider | Concrete model |
| --- | --- | --- |
| `default` | gemini | `gemini-2.5-flash` |
| `gemini-2.5` / `gemini-2.5-pro` | gemini | `gemini-2.5-pro` |
| `gemini-2.5-flash` | gemini | `gemini-2.5-flash` |
| `gemini-3` / `gemini-3-flash` | gemini | `gemini-3-flash-preview` |
| `claude` / `claude-opus` / `claude-opus-4-7` | anthropic | `claude-opus-4-7` |
| `claude-sonnet` / `claude-sonnet-4-6` | anthropic | `claude-sonnet-4-6` |
| `claude-haiku` / `claude-haiku-4-5` | anthropic | `claude-haiku-4-5` |
| `openai` / `gpt` / `gpt-5.5` | openai | `gpt-5.5` |
| `gpt-5.5-pro` | openai | `gpt-5.5-pro` |
| `gpt-5.4` / `gpt-5.4-mini` | openai | `gpt-5.4*` |
| `gpt-5` / `gpt-5-mini` | openai | `gpt-5*` |
| `gpt-4.1` / `gpt-4o` | openai | `gpt-4.1` / `gpt-4o` |
| `o4-mini` | openai | `o4-mini` |
| `text-embedding-3-small` | openai-embeddings | `text-embedding-3-small` |
| `text-embedding-3-large` | openai-embeddings | `text-embedding-3-large` |
| `text-embedding-ada-002` | openai-embeddings | `text-embedding-ada-002` |

These presets are seeded into an empty `llm_presets` table on first
startup. Operators tighten access (e.g., gating
`gpt-5.5-pro` to `min_user_level=tier1`) via the admin webUI; the
router reloads the in-memory map after every mutation so the next
call sees the new shape immediately.

**API key sourcing:**

| Provider | Default `secret_ref` | Env var read |
| --- | --- | --- |
| gemini | `env://GEMINI_API_KEY` | `GEMINI_API_KEY` |
| anthropic | `env://ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` |
| openai | `env://OPENAI_API_KEY` | `OPENAI_API_KEY` |

**Provider feature parity:**

Six providers; each has a `generate`-side adapter (chat) and the
two `*-embeddings` providers expose only `embed`. The matrix breaks
them out so `openai-compatible` (chat-only) and
`openai-compatible-embeddings` (embed-only) don't get conflated:

| Feature | `gemini` | `anthropic` | `openai` (Responses) | `openai-embeddings` | `openai-compatible` (local chat) | `openai-compatible-embeddings` (local) |
| --- | --- | --- | --- | --- | --- | --- |
| `generate` (non-streaming) | ✅ | ✅ | ✅ | ❌ NotImpl | ✅ | ❌ NotImpl |
| `generate` (streaming) | ✅ | ✅ | ✅ | — | ✅ | — |
| Vision input via `image_part()` | ✅ | ✅ | ✅ | — | ✅ (model-dependent; sent as `image_url` data URL) | — |
| Function tools | ✅ | ✅ | ✅ | — | ✅ (model-dependent; not all local models support tools) | — |
| Parallel function calls | ✅ | ✅ | ✅ (controllable via `parallel_tool_calls`) | — | ✅ (whatever the upstream supports) | — |
| Server tools | ✅ `google_search` / `code_execution` | ✅ `web_search` / `code_execution` | ✅ `web_search` / `file_search` / `code_interpreter` (via `provider_options["tools"]`) | — | ❌ | — |
| `embed` | ✅ | ❌ recommends Voyage AI | ❌ NotImpl (use `openai-embeddings`) | ✅ `text-embedding-3-*` | ❌ NotImpl (use `openai-compatible-embeddings`) | ✅ `/v1/embeddings` of the local server |
| `count_tokens` | ✅ | ✅ | ✅ | ❌ NotImpl | ❌ no universal endpoint | ❌ NotImpl |
| Reasoning round-trip | ✅ `thought_signature` | ✅ `reasoning_blocks` (thinking) | ✅ `reasoning_blocks` (encrypted) | — | ❌ (some models surface reasoning as `<think>` text inline) | — |
| Custom `base_url` | ✅ | ✅ | ✅ | ✅ | ✅ (required) | ✅ (required) |

`—` = surface doesn't apply (embeddings adapters don't have a
`generate` path; chat adapters have an `embed` path that always raises
`NotImplementedError` to steer callers to the correct provider).

### 1.1.1 Local servers — OpenAI-compatible providers

Two providers cover OpenAI-compatible local servers (vLLM, LM Studio,
llama.cpp's `--server`, text-generation-inference, Ollama in
OpenAI-mode, etc.):

| Provider | Endpoint | Use for |
| --- | --- | --- |
| `openai-compatible` | `POST {base_url}/chat/completions` | text generation, tools, vision |
| `openai-compatible-embeddings` | `POST {base_url}/embeddings` | embeddings |

Each preset for these providers carries a `base_url` field — the
local server's OpenAI-compatible root, e.g.:

| Server | Typical `base_url` |
| --- | --- |
| vLLM | `http://localhost:8000/v1` |
| LM Studio | `http://localhost:1234/v1` |
| llama.cpp `--server` | `http://localhost:8080/v1` |
| Ollama (OpenAI-mode) | `http://localhost:11434/v1` |
| text-generation-inference | `http://localhost:3000/v1` |

The router includes `base_url` in the adapter cache key, so two
presets pointing at *different* local servers stay isolated even if
they share provider + model name.

**API key.** Local servers usually don't authenticate. Leave
`api_key_ref` blank and skip the inline `api_key`; the adapter sends
the placeholder `"EMPTY"` (the OpenAI SDK requires *some* string).
If your server *does* require a key (LiteLLM proxy, hosted vLLM
behind an auth proxy), set `api_key_ref` or `api_key` like any other
provider.

**vLLM-specific sampling kwargs.** Anything under
`provider_options.extra_body` is forwarded raw to the upstream — vLLM
uses this for `top_k`, `repetition_penalty`, `min_p`,
`guided_choice`, etc.:

```python
preset = "qwen-2.5-32b-vllm"   # base_url=http://vllm:8000/v1
await ctx.llm.generate(
    messages,
    preset=preset,
    provider_options={"top_p": 0.95, "extra_body": {"top_k": 50}},
)
```

**Limitations.** Reasoning round-trip blocks, server-side tools
(`web_search` / `file_search` / `code_interpreter`), encrypted
reasoning, and `count_tokens` are not portable across local servers
and are not wired. Streaming usage chunks rely on the server
honouring `stream_options.include_usage=True` (vLLM does; some older
local servers ignore it).

### 1.1.2 Hosted providers via custom endpoints

`base_url` is also accepted on **hosted** presets (`gemini`,
`anthropic`, `openai`, `openai-embeddings`) — left blank, the
upstream SDK uses its built-in default URL; set, it overrides the
endpoint. Useful for:

| Use case | Provider | Typical `base_url` |
| --- | --- | --- |
| Azure OpenAI proxy | `openai` | `https://<resource>.openai.azure.com/openai/deployments/<deploy>` |
| AWS Bedrock-fronted Anthropic | `anthropic` | `https://<bedrock-proxy>/anthropic` |
| Vertex / EU Gemini | `gemini` | `https://<region>-aiplatform.googleapis.com` |
| LiteLLM / Portkey gateway | any | `https://<gateway>/v1` |
| Self-hosted enterprise auth proxy | any | site-specific |

The cross-field rule is:

| Provider | `base_url` |
| --- | --- |
| `gemini` / `anthropic` / `openai` / `openai-embeddings` | optional — blank = SDK default |
| `openai-compatible` / `openai-compatible-embeddings` | required — no default endpoint |

The adapter cache key always includes `base_url`, so two presets
that differ only in endpoint (e.g. `openai-direct` vs `openai-azure`)
stay in distinct cache slots and hit the right upstream.

**SSRF defenses on `base_url`.** Validation at admin save time
rejects URLs that would let a compromised admin token exfiltrate
provider keys to internal services:

- Hosted providers (`gemini` / `anthropic` / `openai` / `openai-embeddings`)
  must use `https://`. The official endpoints are TLS-only, and
  `http://` would mean the SDK ferries the API key in cleartext.
- Hosted providers reject IP literals in the loopback (127/8, ::1),
  private (RFC1918, ULA), or link-local (169.254/16) ranges.
  Hostnames pass through (no DNS lookup at validation time).
- Local providers (`openai-compatible*`) accept loopback and private
  ranges (the whole point), but link-local is blocked for
  *everyone* — that range covers the cloud-metadata endpoint
  (169.254.169.254).
- Cloud-metadata hostnames (`metadata.google.internal`,
  `metadata.azure.com`, `instance-data.ec2.internal`) are blocked
  for all providers regardless of address class.
- Operators with private-VPC LiteLLM / Portkey gateways at known
  hostnames can carve exceptions via `ROUTER_BASE_URL_ALLOWED_HOSTS`
  (comma-separated, case-insensitive).

Validation is intentionally pure — no DNS lookup. The trade-off is
that a hostname like `mycorp-internal` could pass and later resolve
to a private IP (DNS rebinding risk). That's the operator's domain
to constrain via the explicit allowlist.

### 1.2 Streaming

`stream=True` returns an `AsyncIterator[LlmDelta]`. The SDK plumbs
deltas straight into the active task's `Progress` channel
automatically (event type `chunk`), so UI clients receive tokens
without extra agent code:

```python
async for delta in await ctx.llm.generate(prompt, stream=True):
    if delta.thought:
        thoughts_pane.append(delta.text)            # part.thought / thinking_delta
    elif delta.text:
        answer_pane.append(delta.text)              # text_delta
    elif delta.tool_call:
        # Provider-finalised tool call (Anthropic emits at
        # content_block_stop after accumulating partial JSON;
        # Gemini emits per chunk).
        await dispatch_tool_call(delta.tool_call.name, delta.tool_call.args)
    elif delta.reasoning_block:
        # Anthropic-only: a completed `thinking` / `redacted_thinking`
        # block. Accumulate these for round-trip if you intend to
        # continue the conversation with tool use (see §1.3.3).
        reasoning.append(delta.reasoning_block)
    elif delta.finish_reason:
        # Stream complete. usage is cumulative on Anthropic.
        ...
```

**Provider differences worth knowing:**

| Concern | Gemini | Anthropic | OpenAI (Responses) |
| --- | --- | --- | --- |
| Underlying transport | per-chunk parts | SSE events with per-block-index state | SSE events with per-output-index state |
| `tool_call` delta timing | streamed per chunk as the call resolves | one consolidated delta on `content_block_stop` after `input_json_delta` partials | one consolidated delta on `response.function_call_arguments.done` after partial-JSON delta accumulation |
| Thinking text | `LlmDelta(text=..., thought=True)` per `part.thought=True` chunk | same — sourced from `thinking_delta` events | same — `response.output_text.delta` events under a `reasoning` parent item are flagged `thought=True` |
| Signature delivery | per-part `thought_signature` field | full `reasoning_block` emitted on block stop (assembled from `thinking_delta` + `signature_delta`) | full `reasoning_block` emitted on `response.output_item.done` for reasoning items (carries `encrypted_content`) |
| `usage` fields on deltas | output_tokens monotonically increasing | input_tokens on `message_start`, cumulative output_tokens on `message_delta` | one final `usage` delta on `response.completed` (full final state) |
| `finish_reason` source | from final part's finish enum | from `message_delta.stop_reason` | derived on `response.completed` from `status` + `incomplete_details.reason` + presence of `function_call` items |

**Round-tripping after a streaming call**: the streaming iterator
gives you raw deltas, not an `LlmResponse`, so
`Message.assistant_from_response` doesn't apply directly. Either
collect deltas into a synthetic response yourself, or use the
non-streaming path when you intend to continue with tool use. (For
admin / UI-only "show tokens as they arrive" cases, the streaming
path is the right tool.)

### 1.2.1 Agent-side retry policy (`RetryPolicy`)

The router already retries each call inside `_call_with_fallback`
and walks the fallback chain on its own (§1.1).

**`generate` / `embed` / `count_tokens` retry by default.** When
`retry` is omitted the SDK applies `RetryPolicy()` (the defaults
table below — 3 attempts, exponential backoff, `Retry-After`
honoured), **not** single-shot: a bare `503 high demand` from a
provider would otherwise take down a whole turn. To restore the
old single-shot behaviour, pass `RetryPolicy(max_attempts=1)`
explicitly. To customise (e.g. an interactive UI that wants more
aggressive retries), pass a tuned `RetryPolicy` to
`generate / embed / count_tokens`:

```python
from bp_sdk.llm import RetryPolicy

resp = await ctx.llm.generate(
    messages,
    preset="claude-haiku",
    retry=RetryPolicy(max_attempts=3),
)
```

**Defaults** (matches design doc §11):

| Field | Default | Notes |
| --- | --- | --- |
| `max_attempts` | 3 | Total attempts at the SDK layer (initial + retries). Clamped at `total_attempts_cap`. |
| `initial_backoff_s` | 0.5 | First-retry wait. Subsequent waits grow per `backoff_multiplier`. |
| `max_backoff_s` | 10.0 | Hard cap on the schedule and on a `Retry-After: N` hint. |
| `backoff_multiplier` | 2.0 | `wait = initial × multiplier^attempt`. |
| `jitter` | True | Full jitter — sample uniformly in `[0, capped]`. |
| `total_attempts_cap` | 8 | Defensive ceiling. SDK × router × chain-length attempts under outage would otherwise multiply. |
| `retry_codes` | `RETRIABLE_LLM_CODES` | Single source of truth, shared with the router classifiers. |
| `on_retry_pending` | `None` | Streaming-only callback for `delta.meta` hints. |

**What gets retried.** The SDK reads the typed `error.code` and
`error.retriable` flag off the wire. A retry happens when both
hold:

  - `code` is in `policy.retry_codes` (defaults to the protocol-
    side `RETRIABLE_LLM_CODES`).
  - `retriable` is True. Codes in `RETRIABLE_LLM_CODES` auto-derive
    this; agent-built `LlmCallError`s honour the explicit value.

A non-retriable code (e.g., `llm_upstream_invalid_request`,
`auth_failed`, `acl_denied`) surfaces immediately even with a
policy in place. Retrying a misshapen request 8 times wastes
quota.

**Retry-After.** When the upstream returns
`error.retry_after_seconds` (typically populated from a 429's
`Retry-After` header), the SDK uses that verbatim for the inter-
attempt wait, capped at `max_backoff_s`. The exponential schedule
is bypassed entirely for that attempt.

**Streaming.** Streaming retry is bounded to **pre-first-delta
only**. Once any content chunk has reached the agent, a downstream
failure raises with `error.code = stream_interrupted` (NOT in
`RETRIABLE_LLM_CODES`) — re-issuing would mean the agent has to
splice two prefixes manually. The SDK propagates the iterator
without re-issuing.

**Meta deltas during streaming.** When the router's setup-retry
loop pauses between attempts (§1.1, "Streaming retry semantics"),
it emits status-only `LlmDelta(meta={"kind": "retry_pending", ...})`
frames. The SDK **swallows these by default** — agent code only
sees content deltas, preserving the appearance of a transparent
retry. To surface the spinner-hint payload to a UI, set
`on_retry_pending`:

```python
def show_retry_hint(meta):
    # meta: LlmDeltaMeta
    print(f"upstream {meta.reason_code}; "
          f"retrying attempt {meta.attempt}/{meta.max_attempts} "
          f"in {meta.retry_after_seconds:.1f}s")

policy = RetryPolicy(on_retry_pending=show_retry_hint)
async for delta in await ctx.llm.generate(prompt, stream=True,
                                          retry=policy):
    ...  # only content deltas; meta hints went to the callback.
```

A misbehaving callback (raises) is logged and ignored — it cannot
break the stream.

**`LlmCallError` typed fields.** When the SDK gives up, it raises
`LlmCallError` carrying the wire-level information:

```python
try:
    await ctx.llm.generate(messages, retry=RetryPolicy())
except LlmCallError as exc:
    log.warning(
        "llm_giving_up",
        code=exc.code,                        # ErrorCode.LLM_*
        retriable=exc.retriable,              # auto-derived
        retry_after_seconds=exc.retry_after_seconds,
        upstream_class=exc.upstream_class,    # telemetry — never routing
    )
```

**Cancellation interaction.** A `CancelToken` trip during the
inter-attempt sleep aborts the wait immediately and raises
`CancellationError` — agents tearing down don't pay for the rest
of a 10-second backoff.

### 1.3 Tool calls

Tool definitions use a provider-neutral shape (`ToolSpec`). The
service translates to provider-native formats. Tool call results are
typed:

```python
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: Literal["stop","length","tool_calls","content_filter"]
    usage: TokenUsage
    raw: dict[str, Any]                # provider-specific extras
```

For Gemini-native features (grounding, code execution, URL context,
image/video generation), use `provider_options`:

```python
await ctx.llm.generate(
    prompt,
    model="gemini-3-flash",
    provider_options={
        "tools": [{"google_search": {}}, {"code_execution": {}}],
        "thinking_level": "low",       # Gemini 3 — low | medium | high
        "thinking_budget": 8192,       # explicit token budget (any model)
        "media_resolution": "high",    # Gemini 3 — low | medium | high
    },
)
```

`provider_options` is opaque to the SDK; the LLM service forwards it
as-is to the provider client. This is the deliberate escape hatch for
provider-tailored agents — keep the typed surface narrow, let the
provider-specific blob carry capabilities the neutral shape doesn't
cover.

**Recognised `provider_options` keys (Gemini):**

| Key | Type | Effect |
| --- | --- | --- |
| `tools` | `list[dict]` | Native tool blocks (`{"google_search": {}}`, `{"code_execution": {}}`, `{"url_context": {}}`). Combined with neutral `tools=` — function declarations first, then native blocks. |
| `thinking_level` | `"low" \| "medium" \| "high"` | Gemini 3 thinking budget tier (default `high`; `minimal` available on 3 Flash / 3 Flash-Lite). |
| `thinking_budget` | `int` | Explicit token budget. `0` disables (where supported), `-1` enables dynamic budgeting. Overrides legacy `thinking_budget_tokens`. |
| `include_thoughts` | `bool` | Include the model's thought summary in the response — surface as `LlmResponse.thought_summary` (or `LlmDelta.thought=True` chunks while streaming). |
| `media_resolution` | `"low" \| "medium" \| "high"` | Gemini 3 — max tokens allocated per input image / video frame. |
| `safety_settings` | provider-shape | List of `{"category": ..., "threshold": ...}`. |
| `response_mime_type` | `str` | E.g. `"application/json"` to force structured output. |
| `response_schema` | dict | JSON schema for structured output (paired with `response_mime_type`). |
| `response_modalities` | `list[str]` | E.g. `["IMAGE", "TEXT"]` for image-generating models. |
| `stop_sequences` | `list[str]` | Up to 5 stop strings. |

> **Gemini 3 temperature**: Google recommends keeping `temperature`
> at the default `1.0` for Gemini 3 models — lower values can cause
> looping or degraded reasoning, especially on math / complex
> reasoning. The SDK does not second-guess; pass `temperature=` only
> if you've measured the tradeoff.

> **`max_tokens` is a TOTAL budget on Gemini 2.5+** (not a cap on
> visible output alone). Hidden thinking tokens count against it,
> so a small cap truncates the visible answer with
> `finish_reason="length"` even when the model would have stopped
> naturally with a larger budget. Either drop the cap (provider
> default applies — typically generous), or raise it well above the
> thinking estimate. To control the thinking side directly, use
> `provider_options["thinking_level"]` or
> `provider_options["thinking_budget"]` from the table above. The
> `examples/test_drive/gemini_agent.py` example surfaces
> `response.usage.thoughts_tokens` in its output metadata so the
> budget split is visible. (Surfaced as Bug 13 by the test drive —
> see `tests/test_upstream_bugs_10_to_13.py`.)

### 1.3.1 Thinking and thought signatures

Gemini 3 and 2.5 series models think before responding. The thinking
configuration is set via `provider_options` (see table above):

```python
# Gemini 3 — tier-based.
provider_options={"thinking_level": "low"}   # fastest
provider_options={"thinking_level": "high"}  # default; deepest reasoning

# Gemini 2.5 — explicit budget in tokens.
provider_options={"thinking_budget": 1024}   # bound
provider_options={"thinking_budget": 0}      # off (Flash / Flash Lite)
provider_options={"thinking_budget": -1}     # dynamic (default)

# Both: surface the reasoning trace in the response.
provider_options={"include_thoughts": True}
```

When `include_thoughts=True`, `LlmResponse.thought_summary` is
populated with the concatenated thought text. While streaming,
`LlmDelta.thought=True` flags chunks belonging to the thought summary
so you can render them in a separate pane:

```python
async for delta in await ctx.llm.generate(prompt, stream=True,
                                          provider_options={"include_thoughts": True}):
    if delta.thought:
        thoughts_pane.append(delta.text)
    elif delta.text:
        answer_pane.append(delta.text)
```

**Thought signatures** are encrypted blobs the model returns to keep
its reasoning context across turns. The Gemini API is stateless, so
agents must pass them back on the next call. The SDK does this for
you when you use the helper:

```python
# Multi-turn function calling on Gemini 3 — the right way.
messages = [Message(role="user", content="Check AA100 and book a taxi if delayed.")]
while True:
    resp = await ctx.llm.generate(messages, tools=[check_flight, book_taxi])
    # Round-trip the assistant turn — preserves thought_signature on
    # the first function call, which Gemini 3 REQUIRES.
    messages.append(Message.assistant_from_response(resp))
    if not resp.tool_calls:
        print(resp.text)
        break
    for tc in resp.tool_calls:
        result = await dispatch_tool_call(tc.name, tc.args)
        messages.append(Message.tool_response(
            tool_call_id=tc.id,           # Gemini 3 maps results by id
            name=tc.name,
            response=result,
        ))
```

Failure mode: if the assistant turn drops the signature (e.g., agent
manually rebuilds the message and forgets), Gemini 3 returns a 400 —
"Function call FCx in the N. content block is missing a
thought_signature". `Message.assistant_from_response` mirrors the
docs' rules: signature on the FIRST function call only (parallel
calls 2..N carry no signature), or on the text part for
text-only responses.

#### 1.3.1.1 Tool calls that spawn agents — and the multimodal-result helper

When the LLM's tool call is dispatched to a peer agent
(`ctx.peers.spawn_from_tool_call(tc)`), the child returns a
`ResultFrame` whose `output.content` is text and whose
`attachments` may carry files the child wants the LLM to see.
`Message.tool_response_from_result(...)` packages both into a
single tool-response message without per-tool branching:

```python
for tc in resp.tool_calls:
    child = await ctx.peers.spawn_from_tool_call(tc)
    messages.append(Message.tool_response_from_result(
        tool_call_id=tc.id, name=tc.name, result=child,
    ))
```

The helper reads `result.output.files` — the file-store NAMES the
child surfaced — and threads each as a `file_ref` part:

- **No `output.files`** → `response=result.output.content` (plain
  text). Identical wire output to the bare-text path; zero
  multimodal-envelope cost.
- **One or more names** → `response=[{"text": content?},
  {"file_ref": {"name": n1}}, {"file_ref": {"name": n2}}, ...]`.
  The ROUTER resolves each name into the provider call and infers
  the modality (`image` for `image/*`, else `document` — Anthropic
  gates the two distinctly) from the named blob's mime type. Bytes
  never cross the agent→router frame.

This makes the helper safe for **auto-discovered tools**: a newly-
onboarded agent or freshly-bridged MCP server becomes callable via
`build_tools()` without code change, because the receiver-side loop
above doesn't need per-tool knowledge of the child's files — every
name in `output.files` is threaded uniformly. A child that wants a
file available but NOT shown to the model simply omits it from
`output.files` (it stays in the stash, reachable by name).

### 1.3.2 Anthropic provider options

Same `provider_options` escape hatch, different recognised keys —
the LLM service forwards them through to `messages.create`. The most
useful surface is **prompt caching** (anchor cache breakpoints by
adding `cache_control` to tool result blocks or system content) and
**native server tools** (web search, code execution).

```python
await ctx.llm.generate(
    [Message(role="user", content="What's new in Python 3.13?")],
    model="claude-opus",
    provider_options={
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "stop_sequences": ["END"],
    },
)
```

**Recognised `provider_options` keys (Anthropic):**

| Key | Type | Effect |
| --- | --- | --- |
| `tools` | `list[dict]` | Native tool blocks (`web_search`, `code_execution`, etc.). Combined with neutral `tools=` — function declarations first, then native blocks. |
| `metadata` | dict | E.g. `{"user_id": "..."}` for upstream observability. |
| `stop_sequences` | `list[str]` | Custom stop strings. |
| `top_p` / `top_k` | float / int | Sampling controls. Note: not compatible with thinking enabled (Anthropic 400s). |
| `thinking` | dict | Extended / adaptive thinking config — see §1.3.3 below. |
| `output_config` | dict | Adaptive-thinking effort (`{"effort": "low" \| "medium" \| "high" \| "xhigh" \| "max"}`). |
| `container` / `service_tier` / `betas` | various | Native passthrough for code execution, latency tier, beta flags. |

**`tool_choice` mapping** — neutral string forms map to Anthropic's
shape:

| Neutral | Anthropic |
| --- | --- |
| `"auto"` | `{"type": "auto"}` |
| `"required"` | `{"type": "any"}` (must use SOME tool) |
| `"none"` | `{"type": "none"}` |
| `dict` | passthrough — caller knows Anthropic's shape, e.g. `{"type": "tool", "name": "..."}` to force a specific tool, or `{"type": "auto", "disable_parallel_tool_use": True}` |

When extended / adaptive thinking is enabled, **only `auto` and
`none` are accepted** — `required`, `{type: any}`, and
`{type: tool, name: ...}` raise locally with a clear error rather
than sending a guaranteed-400 payload upstream.

**Image inputs** translate cleanly: `image_part(...)` produces
`{"image": {"mime_type", "data"}}` and the Anthropic adapter rewrites
to `{"type": "image", "source": {"type": "base64", "media_type", "data"}}`.
URL- and File-API-sourced images can be passed directly as native
blocks in `Message.content`:

```python
Message(role="user", content=[
    {"type": "image",
     "source": {"type": "url", "url": "https://example.com/photo.jpg"}},
    {"type": "text", "text": "Describe this."},
])
```

**Tool-result formatting** is handled automatically: consecutive
`Message(role="tool", ...)` entries (e.g., from parallel tool calls)
get merged into a single user message with all `tool_result` blocks
first, per Anthropic's parallel-tool-use rules. Use the same
`Message.tool_response(tool_call_id=, name=, response=)` helper that
Gemini agents use.

**`thought_signature`** in neutral parts is **dropped** by the
Anthropic adapter — it's a Gemini-only construct. Anthropic's
analogue (`thinking` content blocks) is round-tripped via
`LlmResponse.reasoning_blocks`; see §1.3.3.

### 1.3.3 Extended and adaptive thinking (Anthropic)

Anthropic's reasoning surface differs from Gemini's: instead of an
opaque signature on individual content parts, the model emits whole
**`thinking`** content blocks (visible reasoning + an opaque
`signature`) and, for safety-redacted reasoning,
**`redacted_thinking`** blocks (opaque `data` only).

**The hard rule, from the docs:** when extended / adaptive thinking
is enabled AND tools are used, **every** `thinking` and
`redacted_thinking` block returned by the model **must** be passed
back unchanged on the next assistant turn. Drop any of them and the
upstream returns a 400.

**Two thinking modes:**

```python
# Manual (any model EXCEPT Opus 4.7+, where it 400s):
provider_options={"thinking": {"type": "enabled", "budget_tokens": 4096}}

# Adaptive (recommended on Opus 4.7+, Opus 4.6, Sonnet 4.6):
provider_options={
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},   # max | xhigh | high | medium | low
}

# Off:
provider_options={"thinking": {"type": "disabled"}}   # or omit entirely
```

**Two display modes:**

| `thinking.display` | Behaviour |
| --- | --- |
| `"summarized"` (default on Sonnet 4.6, Opus 4.6, earlier 4.x) | Visible thinking text in `LlmResponse.thought_summary`. |
| `"omitted"` (default on Opus 4.7) | `thought_summary` is None, but `signature` still round-trips for context continuity. Lower latency. |

**Round-trip is automatic** when you use the SDK helper:

```python
messages = [Message(role="user", content="What's the weather in Paris?")]
while True:
    resp = await ctx.llm.generate(
        messages,
        model="claude-sonnet",
        tools=[get_weather],
        provider_options={"thinking": {"type": "adaptive"}},
    )
    # Helper prepends resp.reasoning_blocks (thinking +
    # redacted_thinking) to the rebuilt assistant turn — exactly what
    # Anthropic requires for the next call to succeed.
    messages.append(Message.assistant_from_response(resp))
    if not resp.tool_calls:
        print(resp.text)
        break
    for tc in resp.tool_calls:
        result = await dispatch_tool_call(tc.name, tc.args)
        messages.append(Message.tool_response(
            tool_call_id=tc.id,
            name=tc.name,
            response=result,
        ))
```

**Inspecting reasoning manually**:

```python
resp = await ctx.llm.generate(messages, model="claude-sonnet",
                              provider_options={"thinking": {"type": "adaptive"}})

# Visible thinking text (when display="summarized").
if resp.thought_summary:
    print("Thinking:", resp.thought_summary)

# Opaque round-trip blocks. Don't try to interpret signature/data;
# treat them as bytes you must echo back.
for block in resp.reasoning_blocks:
    if block["type"] == "thinking":
        print(f"  thinking sig: {block['signature'][:32]}...")
    elif block["type"] == "redacted_thinking":
        print(f"  redacted blob: {len(block['data'])} chars")
```

**Caveats from the docs:**

- `tool_choice` `required` / `{type: any}` / `{type: tool}` are
  rejected with a local `ValueError` when thinking is enabled — only
  `auto` and `none` work.
- `temperature` and `top_k` modifications aren't compatible with
  thinking; `top_p` is allowed in `[0.95, 1.0]`.
- `max_tokens` must be greater than `thinking.budget_tokens` (manual
  mode); both count against the same output budget.
- Toggling thinking on/off mid-conversation invalidates message
  caches. Plan thinking strategy at the start of each turn.

### 1.3.4 OpenAI Responses provider options

The OpenAI provider wraps the **Responses API** (`responses.create`),
not the legacy Chat Completions. Three structural quirks worth
internalising:

1. **`output[]` is a flat array of mixed-type items**: `message`,
   `function_call`, `reasoning`, `custom_tool_call`. Function calls
   are first-class top-level items, not blocks inside the assistant
   message. Round-trip mirrors this — `Message.assistant_from_response`
   produces neutral parts that the OpenAI adapter then *flattens*
   into the input array.
2. **`call_id` is the round-trip key** for `function_call` ↔
   `function_call_output`. Our neutral `ToolCall.id` stores `call_id`
   verbatim. The item's own `id` is internal and not used for
   mapping.
3. **`arguments` is a JSON-encoded string** in the wire format. The
   adapter parses it on extract and re-serialises on emit. Malformed
   JSON falls back to `{}` rather than crashing.

```python
# Single-turn function call.
await ctx.llm.generate(
    [Message(role="user", content="Weather in Paris?")],
    model="gpt-5.5",
    tools=[ToolSpec(name="get_weather", description="...", parameters={...})],
    provider_options={
        "reasoning": {"effort": "low"},     # for reasoning models
        "metadata": {"user_id": "u123"},
    },
)
```

**Recognised `provider_options` keys (OpenAI):**

| Key | Type | Effect |
| --- | --- | --- |
| `tools` | `list[dict]` | Native tool blocks (`{"type": "web_search"}`, `{"type": "file_search"}`, `{"type": "code_interpreter"}`, custom tools, etc.). Combined with neutral `tools=` — function declarations first, then native blocks. |
| `reasoning` | dict | `{"effort": "minimal" \| "low" \| "medium" \| "high" \| "xhigh", "summary": "auto" \| "concise" \| "detailed"}`. See §1.3.5. |
| `parallel_tool_calls` | bool | Set `False` for "exactly zero or one tool" semantics. |
| `metadata` | dict | E.g. `{"user_id": "..."}` for upstream observability. |
| `previous_response_id` | str | Stateful chaining alternative to manual round-trip. Our protocol is one-shot, so most agents won't use this. |
| `store` | bool | Server-side state retention. Default off (we always pass `include=["reasoning.encrypted_content"]` for stateless round-trip). |
| `text` | dict | Structured-outputs / `response_format` config. |
| `include` | `list[str]` | Additional includes (e.g., `["file_search_call.results"]`) — merged with the default `["reasoning.encrypted_content"]`. |
| `phase` | str | `"commentary"` for intermediate updates, `"final_answer"` for completion. Recommended on long agentic flows per the docs. |
| `max_output_tokens` | int | Alternative to top-level `max_tokens=` kwarg. |
| `top_p` / `top_logprobs` / `logprobs` | various | Standard sampling controls. |
| `service_tier` / `background` / `safety_identifier` / `user` | various | Native passthroughs. |

**`tool_choice` mapping** — neutral string forms map directly to
OpenAI's bare-string form:

| Neutral | OpenAI |
| --- | --- |
| `"auto"` | `"auto"` |
| `"required"` | `"required"` |
| `"none"` | `"none"` |
| `dict` | passthrough — caller knows OpenAI's shape, e.g. `{"type": "function", "name": "..."}` to force a specific function, or `{"type": "allowed_tools", "mode": "auto", "tools": [...]}` to restrict the available subset. |

**Image inputs** translate cleanly: `image_part(...)` produces
`{"image": {"mime_type", "data"}}` and the OpenAI adapter rewrites to
`{"type": "input_image", "image_url": "data:<mime>;base64,..."}`.
URL-sourced and File-API-sourced images can be passed directly:

```python
Message(role="user", content=[
    {"type": "input_image", "image_url": "https://example.com/photo.jpg"},
    {"type": "input_image", "file_id": "file-abc"},
    {"type": "input_text", "text": "Describe these."},
])
```

**`strict` mode** is intentionally NOT set on tool definitions by
default. Per the docs, omitting `strict` lets the Responses API
normalize your schema into strict mode automatically — that matches
OpenAI's recommendation while preserving your schema verbatim. To
opt out, set `strict: false` per tool via `provider_options["tools"]`.

**Tool definitions** use OpenAI's `type: function` shape with
`parameters` (NOT Anthropic's `input_schema`, NOT Gemini's
`function_declarations` wrapper):

```python
# What ToolSpec produces under the hood for OpenAI:
{
    "type": "function",
    "name": "get_weather",
    "description": "Retrieves current weather...",
    "parameters": {...JSON Schema...},
}
```

### 1.3.5 Reasoning round-trip (OpenAI)

OpenAI reasoning models (`gpt-5.5`, `gpt-5.4`, `o4-mini`, etc.) emit
`reasoning` items in the output. To round-trip them across turns in
**stateless mode** (no `previous_response_id`), three things have to
line up:

1. The request must include `"reasoning.encrypted_content"` so the
   server populates the opaque blob. **The adapter does this for you
   automatically** — every `create` call carries the include.
2. The `reasoning` items in the response are surfaced on
   `LlmResponse.reasoning_blocks` verbatim (with `id`, `summary`,
   `encrypted_content`).
3. `Message.assistant_from_response` prepends them to the rebuilt
   assistant turn; the OpenAI adapter then *flattens* them into the
   input array as standalone `reasoning` items at the same position
   they were emitted.

```python
while True:
    resp = await ctx.llm.generate(
        messages,
        model="gpt-5.5",
        tools=[get_weather],
        provider_options={"reasoning": {"effort": "medium"}},
    )
    # Helper prepends resp.reasoning_blocks; adapter flattens them
    # into the next request's input array — exactly what Responses
    # expects for context continuity.
    messages.append(Message.assistant_from_response(resp))
    if not resp.tool_calls:
        print(resp.text)
        break
    for tc in resp.tool_calls:
        result = await dispatch_tool_call(tc.name, tc.args)
        messages.append(Message.tool_response(
            tool_call_id=tc.id,           # tc.id is the call_id
            name=tc.name,
            response=result,
        ))
```

If you ever inspect the on-wire input items the adapter produces,
the order for a multi-turn step looks like:

```
[
    {"role": "user", "content": "..."},         # original prompt
    {"type": "reasoning", "id": "rs_...", ...,  # round-tripped
     "encrypted_content": "..."},
    {"type": "function_call", "call_id": "call_xyz", ...},
    {"type": "function_call_output", "call_id": "call_xyz", "output": "..."},
    # next turn picks up here with another reasoning + function_call,
    # or a final assistant message.
]
```

Per the docs: pass back "all reasoning items, function call items,
and function call output items, since the last user message". Our
helper does this on every turn.

**Reasoning effort** is a soft control via
`provider_options={"reasoning": {"effort": "..."}}`:

| Effort | Best for |
| --- | --- |
| `minimal` | Latency-critical, no real reasoning needed. |
| `low` | Tool use, planning, drafting. Modest latency hit. |
| `medium` | Default for `gpt-5.5`. Most workloads. |
| `high` | Hard reasoning, complex debugging, agentic flows. |
| `xhigh` | Long rollouts, deep research. Big latency cost. |

**Reasoning summary** can also be surfaced via
`provider_options={"reasoning": {"summary": "auto"}}`. When set, the
visible summary text is concatenated into `LlmResponse.thought_summary`
for display.

### 1.4 Multimodal input

`Message.content` is `str | list[dict[str, Any]]`. When `content` is
a list, each entry is a *content part*. The SDK standardises a single
neutral part — **image** — because base64-inline encoding works
across every vision-capable provider (Gemini, Anthropic, OpenAI). All
other modalities (audio, video, file references) vary too much to
model neutrally; agents pass provider-specific part dicts through
`content` directly.

The `image_part()` helper builds the neutral shape:

```python
from bp_sdk.llm import Message, image_part

# From a path — mime_type inferred from the extension.
await ctx.llm.generate([
    Message(role="user", content=[
        {"text": "What is in this picture?"},
        image_part("photo.jpg"),
    ]),
])

# From bytes — mime_type required.
buf = my_camera.snapshot()  # bytes
await ctx.llm.generate([
    Message(role="user", content=[
        {"text": "Caption this frame."},
        image_part(buf, mime_type="image/png"),
    ]),
])
```

Neutral schema: `{"image": {"mime_type": "...", "data": "<base64>"}}`.
Provider adapters detect this and rewrite to native shape:

| Provider  | Native shape                                                            |
| --------- | ----------------------------------------------------------------------- |
| Gemini    | `{"inline_data": {"mime_type": ..., "data": ...}}`                      |
| Anthropic | `{"type": "image", "source": {"type": "base64", "media_type": ...}}`    |
| OpenAI    | `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}` |

Other parts pass through verbatim. Agents that need provider-specific
features (Gemini `file_data`, Gemini File API URIs, Anthropic PDFs)
hand the native part dict directly:

```python
# Gemini File API reference — opaque to SDK, agent's responsibility.
Message(role="user", content=[
    {"text": "Summarise this PDF."},
    {"file_data": {"file_uri": "files/abc", "mime_type": "application/pdf"}},
])
```

The image binary travels inline inside the WS frame; for very large
images (multi-MB), prefer the provider's native upload API and pass
the resulting reference part directly instead of `image_part()`.

**`ctx.files.llm_ref()` — pass a NAME, let the router resolve it.**
`image_part()`/`document_part()` inline raw bytes (the agent must
*hold* them, and the +33% base64 rides the agent→router frame). When
the file is in the router-managed stash, reference it by NAME with
`ctx.files.llm_ref(name)` instead:

```python
from bp_sdk import Message

parts = [{"text": payload.prompt}]
parts += [ctx.files.llm_ref(n) for n in await ctx.files.list()]  # names only
resp = await ctx.llm.generate([Message(role="user", content=parts)])
```

The agent→router frame carries only the name (`{"file_ref": {"name":
…}}`); the router derives the `(user_id, session_id)` from the task,
resolves the named blob from storage, and inlines it into the
provider call. This works from a storage-less agent and sidesteps the
`FrameTooLargeError` inline footgun. The router routes by the blob's
mime: `image/*` inlines as an image, `application/pdf` as a document,
text types (markdown / html / csv / json / plain) inline as **text**
(providers reject arbitrary text mimes in a document slot, so they're
fed as text instead), and anything else becomes a short "not a
multimodal-supported type" reference note rather than an unfeedable
blob. An explicit `as_` (`image`/`document`) forces the base64
envelope. Inlining is capped: a resolved file over the router's
`llm_attachment_inline_max_bytes` fails the call with a clear error —
for very large media use the provider's native upload API and pass
that reference part directly.

### 1.5 Multimodal output

The router's LLM service stays a thin call/response. Image generation,
audio synthesis, and other binary-output flows are explicitly **not**
wrapped here — they belong in dedicated agents that own storage and
representation. An `image-generator` agent receives a prompt, calls
its provider, persists the result through `ctx.files.store()`, and
returns its file-store name. Calling agents see a normal
agent-to-agent task (subject to the usual ACL), not a provider-specific
LLM oddity.

### 1.6 Quota and budget — partially shipped

The router-wide **admit-rate quota** (per-`(user_id, level)` token
bucket on `NewTask` admit) **is shipped** — see
`docs/router/state.md` §2.3 and `docs/design/quota-enforcement.md`.
It bounds task-admission throughput per user; an agent whose user
is over budget gets the spawn rejected with
`reason:"quota_exceeded"` (+ `retry_after_s`).

**LLM-specific** per-user budgets (token / cost counters enforced
at the LLM call) are still **not yet wired** — they're the planned
counter-table half of the quota work. Today LLM calls succeed
unless the provider rejects them or the deployment hits a manual
ceiling. The SDK records `LLMResponse.usage` and emits it on
`router_llm_tokens_total`, so the data is in place for those
counters once they land.

### 1.7 Embeddings (`ctx.llm.embed`)

Same alias-driven routing as `generate` and `count_tokens` — the
service maps a model name to the right provider adapter:

```python
vectors = await ctx.llm.embed(
    "Quick brown fox jumps over the lazy dog.",
    model="text-embedding-3-small",
)
# → list[list[float]]; first inner list is the embedding for input[0].
```

**Provider support:**

| Provider | Status | Notes |
| --- | --- | --- |
| Gemini | ✅ | Same concrete model namespace as chat (`gemini-2.5-*`). |
| OpenAI | ✅ via `openai-embeddings` | Separate provider — different endpoint (`/v1/embeddings`), different concrete-model namespace (`text-embedding-3-*`). The chat-side `openai` provider raises `NotImplementedError` on `embed()`. |
| Anthropic | ❌ | Anthropic recommends Voyage AI; we'd add a Voyage provider when needed. |

The `text-embedding-3-small` / `text-embedding-3-large` /
`text-embedding-ada-002` aliases route to the
`openai-embeddings` provider automatically. Default vector
dimensions are model-defined (1536 for `-small`, 3072 for `-large`);
the `dimensions` truncation parameter from the OpenAI API isn't
exposed today but agents can call the OpenAI client directly for
that case.

## 2. File handling (`ctx.files`)

Files travel **out-of-band** through a **router-managed named store**
— addressed by NAME, never inlined in `payload` / `output`, so the
typed contract stays clean. The per-task `FileStash` (`ctx.files`) is
the only surface you need:

```python
class FileStash:
    # Store bytes/Path/stream under a NAME. Returns the ACTUAL saved
    # name (may differ from `filename` after a dedup append — always
    # use the returned value). persistent=False → session stash
    # ("{name}", GC'd on session close); persistent=True → user-wide
    # persistent stash ("persist/{name}"). dedup is "append_count"
    # (default) | "overwrite" | "error".
    async def store(self, src: Path | bytes | AsyncIterable[bytes], *,
                    filename: str | None = None,
                    mime_type: str | None = None,
                    persistent: bool = False,
                    dedup: str = "append_count") -> str: ...

    # Write a text file inline (no upload round-trip). Returns the name.
    async def write(self, filename: str, text: str, *,
                    persistent: bool = False,
                    dedup: str = "append_count") -> str: ...

    # Read a name → local bytes (for an agent that PROCESSES them;
    # NOT the LLM-feed path — use llm_ref for that).
    async def read(self, name: str) -> Path: ...
    async def read_bytes(self, name: str) -> bytes: ...

    # Manage the stash by name.
    async def list(self, *, persistent: bool = False,
                   query: str | None = None, stored_after=None) -> list[str]: ...
    async def delete(self, name: str) -> int: ...        # name or "*"-glob
    async def copy(self, src: str, dst: str, *, move: bool = False) -> str: ...

    # Reference a name for an LLM message — the ROUTER resolves it.
    def llm_ref(self, name: str, *, as_: str | None = None) -> dict: ...
```

### 2.1 The name model

- **Names, not refs.** A file is `{filename}` in the session stash or
  `persist/{filename}` in the user-wide persistent stash. There is no
  opaque per-file token — the authority is the `(user_id, scope,
  filename)` tuple, which the router derives from the task (the caller
  never asserts identity). A peer in the same `(user_id, session_id)`
  reaches a file just by mentioning its name; nothing is threaded
  through `spawn` / `delegate`.
- **Storing**: `name = await ctx.files.store(data, filename="chart.png")`
  (or `await ctx.files.write("notes.txt", text)`). Bytes stream to the
  router over a separate HTTP connection (the content-bound
  upload-with-grant path) — they never ride the WS frame. **Always use
  the returned name**: under the default `append_count` dedup a clash
  saves `chart_1.png`, so the name you asked for may not be the name
  you got. Pass `dedup="overwrite"` to replace, or `dedup="error"` to
  refuse a clash.
- **Returning**: `return AgentOutput(content=..., files=[name])`. The
  names ride inside `output.files` (plain strings); a received
  `AgentOutput` carries them on `result.output.files`. An
  LLM-orchestrating parent threads them automatically via
  `Message.tool_response_from_result(...)` (§1.3); any consumer can
  `await ctx.files.read(name)` for the bytes. To keep a produced file
  out of the model's view, simply omit it from `output.files` — it
  stays in the stash, still reachable by name.
- **Showing a file to the LLM**: `ctx.files.llm_ref(name)` — a name
  reference the router resolves into the provider call (§1.4). The
  bytes never enter the agent.
- **Reading**: `path = await ctx.files.read(name)` pulls the bytes to
  a local path over a short-TTL signed URL — for an agent that needs
  to PROCESS the file itself.
- Embedded and external agents get the identical surface. The
  out-of-band path has no payload-frame size cap; inlining bytes into
  a payload via `image_part()`/`document_part()` does (§1.4 /
  `core.md` §7).

### 2.2 Lifecycle

The per-task `FileStash` cleans up its local inbox (downloaded
`read()` bytes + upload spool files) on task completion (success or
failure) — the router-side stash is unaffected. Session-stash files
are garbage-collected when the session closes; `persist/` files live
until explicitly deleted. Total user storage is bounded by the
`file_storage_quota_bytes` per-level quota, enforced at store time.

### 2.3 LLM file tools (`file_tools`)

To let the **model** drive the stash, hand it a ready-made tool
bundle instead of hand-writing schemas:

```python
from bp_sdk import file_tools, is_file_tool, dispatch_file_tool

tools = build_tools(...) + file_tools(bundle="read_only")
resp = await ctx.llm.generate(messages, tools=tools)

for tc in resp.tool_calls:
    if is_file_tool(tc.name):
        messages.append(await dispatch_file_tool(ctx.files, tc))
    else:
        ...  # peer call (ctx.peers.spawn_from_tool_call)
```

- `file_tools("read_only")` (default) → `list_session_file`,
  `list_persist_file`, `read_file`.
- `file_tools("full")` → adds `write_file`, `delete_file`,
  `copy_file`. Expose it only when the workflow genuinely needs the
  model to mutate the stash — `delete_file` accepts a `*` glob, so it
  is the sharpest edge.

`read_file(name)` returns a name `file_ref` as its tool result; the
ROUTER resolves it into the provider call on the **next** turn (§1.4),
so the bytes never enter the agent. Mutating tools echo the saved name
/ count, and a `FileStoreError` comes back as `{"error": code}` so the
model can recover rather than the turn dying.

## 3. Progress (`ctx.progress`)

```python
class ProgressEmitter:
    async def emit(self, event: str, content: str = "",
                   **metadata: Any) -> None: ...
    def chunk(self, text: str) -> None: ...        # token streaming
    def status(self, status: str) -> None: ...     # human-readable
    def tool_call(self, name: str, args: dict) -> None: ...
    def tool_result(self, name: str, result: Any) -> None: ...
```

`emit` is best-effort; backpressure causes `chunk` events to be
coalesced (multi-token concatenation) and oldest non-chunk events to
be dropped if the per-socket outbox is full
(`protocol.md` §4.5). Returns immediately — no agent code is ever
blocked on a slow consumer.

**Implementation note.** The fire-and-forget convenience methods
(`chunk`, `status`, `tool_call`, `tool_result`) schedule each
emit on the event loop via `asyncio.create_task` and park the
Task in a per-emitter strong-ref set (`_pending_emits`) until it
completes. The strong ref is required because asyncio tracks
Tasks via WEAK references — without it a Progress frame
scheduled on a busy loop can be GC'd before its coroutine runs.
A soft cap (`_PENDING_EMITS_SOFT_CAP = 1000`) bounds the set so
a wedged transport can't accumulate Tasks indefinitely; over the
cap, new emits log `progress_emit_dropped_pending_cap` and
return without scheduling. Normal operation is unaffected.

## 4. Cancellation

Cancellation arrives as a router-issued `Cancel` frame and surfaces
in the SDK as a tripped `cancel_token`:

```python
@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    for chunk in long_iterable():
        ctx.cancel_token.raise_if_cancelled()
        ...
```

The cancel token is also wired into the SDK's internal `await`
helpers (`ctx.llm.generate`, `ctx.peers.spawn`, `ctx.files.fetch`),
so cooperative agent code that does most of its waiting via SDK
helpers gets cancellation for free. Pure CPU loops must check the
token themselves.

`CancellationError` from `core.md` §10 is what the SDK raises and
maps to `Result{status:"cancelled", status_code:499}`.

An **uncooperative** handler that never checks the token is, at the
shutdown grace deadline, hard-cancelled by the dispatcher. The
resulting `asyncio.CancelledError` is intercepted the same way: the
dispatcher still emits a terminal `Result{status:"cancelled",
status_code:499}` for the task before the `CancelledError`
re-raises (asyncio contract). So a stuck handler at shutdown
produces a terminal frame rather than leaving the caller's spawn
future to hang to its correlation timeout.

## 5. Tool builders

Turn the visible-peer catalog into provider-native LLM tool
schemas, so the model can call other agents. One call, every
provider — no provider-specific tool-building in your agent:

```python
from bp_sdk.tools import build_tools

tools = build_tools(
    destinations=ctx.peers.visible(),       # already filtered by ctx.user_level
    provider="anthropic",                   # |"openai"|"gemini"
)
```

`ctx.peers.visible()` returns the cached catalog filtered by
`callable_user_levels` against the active task's `user_level`, so the
default flow already excludes agents the user can't reach. If you're
calling `build_tools` against a different snapshot (e.g. the raw
`transport.welcome.available_destinations`), pass `user_level=...`
explicitly so unauthorised agents stay out of the LLM's tool list:

```python
tools = build_tools(
    destinations=transport.welcome.available_destinations,
    provider="anthropic",
    user_level=ctx.user_level,
)
```

`build_tools()` emits one tool per (agent, **tool-visible** mode) —
`call_<agent>` when exactly one mode is tool-visible,
`call_<agent>_<mode>` when several — which
`ctx.peers.spawn_from_tool_call` resolves back to the right
`(agent, mode)` (see `core.md` §7 for the loop). Agents flagged
`AgentInfo.hidden`, and modes registered `@agent.handler(tool=
False)` (`non_tool_modes`), are suppressed from the generated tool
list. An agent whose modes are *all* `tool=False` (a pure
control-plane agent) is therefore absent from `build_tools` output
entirely — there is no permissive `call_<agent>` fallback for it,
so a tool-using model never sees control-plane surfaces. Such modes
remain callable when ACL allows and the caller names the mode
explicitly — `tool=False` is tool-visibility, not access control
(`../acl.md` §8).

## 6. Embedded vs. external

Same handler code, different runtime. The choice is made in
deployment config:

```toml
# router config — register an embedded agent
[[router.embedded_agents]]
module = "my_agents.echo:agent"
```

```toml
# external agent — its own process
[agent]
embedded = false
router_url = "wss://router.example.com/v1/agent"
```

What changes under the hood:

| Concern              | Embedded                                | External                          |
| -------------------- | --------------------------------------- | --------------------------------- |
| Transport            | `InProcessTransport` (asyncio queues)   | `WebSocketTransport`              |
| Handler dispatch     | Direct async function call              | Frame → recv loop → dispatch      |
| File access          | router-managed named store (`ctx.files`) | same — name-based, no `localfile`  |
| Auth                 | Implicit, in-process trust              | Bearer JWT over WS                |
| Crash blast radius   | Takes router with it                    | Isolated process                  |
| Hot reload           | `importlib.reload()` (dev mode)         | Restart container                 |
| Use case             | Hot-path stateless agents               | Everything else                   |

Embedded agents must use `async`/`await` consistently — the SDK
asserts at registration time that the handler is a coroutine
function, and the linter forbids known-blocking imports
(`requests`, sync `sqlite3`, raw `time.sleep`) in embedded modules.

The LLM bridge specifically is **not** an embedded agent in the
rewrite; it's an SDK service (`ctx.llm`). Embedded agents are
reserved for hot-path transformations the SDK doesn't already
provide.

## 7. Testing

The SDK ships a test harness:

```python
import os, pytest
from bp_protocol.types import AgentInfo, LLMData
from bp_sdk.testing import TestRouter

@pytest.mark.asyncio
async def test_echo():
    async with TestRouter(db_url=os.environ["TEST_DB_URL"]) as router:
        info = AgentInfo(agent_id="echo_uppercaser", description="…",
                         capabilities=["text.transform.uppercase"])
        token = await router.register_agent(info)            # returns the agent JWT

        user = await router.create_user(level="tier0")
        result = await router.call(
            info.agent_id,
            LLMData(prompt="hello"),
            user_id=user.user_id,
        )
        assert result.status.value == "succeeded"
        assert result.output.content == "HELLO"
```

`TestRouter` runs the real router (FastAPI app) on a random port
backed by a Postgres reachable via `TEST_DB_URL` (the schema must
already be applied via `alembic upgrade head`). It re-seeds the
bootstrap ACL rule between tests and reloads the in-memory rule set
so the catalog admits everything by default.

`register_agent(info)` inserts the agent row directly and returns a
freshly-issued agent JWT, bypassing the invitation flow. The actual
agent process can then connect to `router.ws_url` with that token.

`router.call(agent_id, payload, user_id=...)` injects a synthetic
caller and drives the full admit → dispatch → result pipeline,
returning the terminal `ResultFrame`.

## 8. Worked example: Gemini agent

A realistic agent that uses streaming, tool calls, files, and
provider-specific options. Roughly the shape we'd build the Gemini
suite from.

```python
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

agent = Agent(
    info=AgentInfo(
        agent_id="gemini_main",
        description=(
            "Gemini-backed conversational agent with web search, "
            "code execution, and image generation."
        ),
        groups=["rank1", "provider:gemini"],
        capabilities=[
            "llm.generate.text",
            "llm.generate.image",
            "search.web",
            "exec.code.python",
        ],
    ),
)

@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    ctx.log.info("gemini_main.start")
    ctx.progress.status("thinking")

    response = await ctx.llm.generate(
        prompt=payload.prompt,
        model="gemini-2.5",
        stream=True,
        provider_options={
            "system_instruction": payload.agent_instruction,
            "tools": [
                {"google_search": {}},
                {"code_execution": {}},
            ],
            "thinking_budget_tokens": 4096,
        },
    )

    text_parts: list[str] = []
    files: list[str] = []

    async for delta in response:
        if delta.text:
            text_parts.append(delta.text)
        if delta.tool_call and delta.tool_call.name == "image_generation":
            ctx.progress.tool_call("image_generation", delta.tool_call.args)
            image_bytes = await delta.tool_call.await_result()
            name = await ctx.files.store(
                image_bytes,
                filename="generated.png",
                mime_type="image/png",
            )
            files.append(name)   # use the RETURNED name (dedup may rename)
            ctx.progress.tool_result("image_generation", {"name": name})

    return AgentOutput(content="".join(text_parts), files=files)
```

Notice what the agent does **not** do: no socket code, no `/receive`
endpoint, no manual ack, no token refresh, no progress fan-out
plumbing, no cancellation polling (the SDK handles it inside
`ctx.llm.generate`'s iterator), no provider SDK setup, no API key
handling. The handler reads as business logic.

A coding-tier-2 specialist looks structurally identical, with
different `capabilities`, different `tools`, and different
`provider_options`. That repeatability is the goal of the SDK design.

## 9. Versioning

The SDK is a Python package versioned independently of the router.
Compatibility rules:

- `protocol_version` (in every frame) is bumped on backward-
  incompatible wire changes; routers reject mismatched agents.
- The SDK declares a supported `protocol_version` range; pip
  resolution handles the rest.
- New optional features (e.g. new `Progress` event types) ship as
  minor SDK versions and are no-ops on older routers.
- Breaking SDK API changes (handler signature, `TaskContext` fields)
  bump the SDK major version; agents pin a major.
