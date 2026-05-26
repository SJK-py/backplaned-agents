> **Superseded.** This proposal (router-resolved `ProxyFile`
> attachments for LLM requests) was replaced by the router-managed
> named file store — see `router-managed-file-store.md` (§8.1 is the
> name-`file_ref` LLM path). Kept as a design record; the `ProxyFile`
> model it describes no longer exists in the code.

# Router-resolved ProxyFile attachments for LLM requests

Let an agent put a **`ProxyFile` reference** (typically a
`router-proxy` ref it already received on its `NewTaskFrame`) into an
LLM message, and have the **router** resolve it from storage and
inline (or provider-native-upload) it before the provider call —
instead of the agent fetching the bytes and base64-inlining them
itself.

## 1. The gap today

Files reach the LLM exactly one way: `image_part()` /
`document_part()` (`bp_sdk/llm.py:544,619`) read raw bytes,
base64-encode them, and embed `{"image"|"document": {mime_type,
data}}` directly into `Message.content`. That rides inside
`LlmRequestFrame.messages` (`bp_protocol/frames.py:414`) over the
agent→router WebSocket; the router's provider adapters
(`bp_router/llm/providers/*`) rewrite the neutral envelope to the
native shape (`gemini.py:163`, `anthropic.py:179`, `openai.py:155`).

`LlmRequestFrame` has **no** attachment / file-ref channel (full
field set verified: `kind, model, preset, messages, tools,
tool_choice, temperature, max_tokens, stream, provider_options,
text, user_id, task_id`). The router LLM path does **zero** storage
resolution — `grep bp_router/llm/` for `ProxyFile`/`FileStore`
is empty.

Consequences for the common "an agent received an image on its task
and wants to ask the LLM about it" case:

- The file makes a **storage → agent → router → provider**
  round-trip. The agent must `await ctx.files.fetch(pf)` (storage →
  agent), then the bytes go agent → router base64-inlined (+33%),
  then router → provider.
- The agent→router WS frame carries the full base64 payload and is
  subject to the frame cap; the docs (`core.md` §7, §10.2) currently
  just *warn* about `FrameTooLargeError` as a footgun.
- A **storage-less agent** (no local FS, embedded sandbox, a thin
  router/orchestrator) cannot forward an inbound file to an LLM at
  all without first materialising bytes it has no reason to hold.

The router already has a **general, multi-protocol attachment
resolver** — `tasks._resolve_attachments` (`tasks.py:644-894`) —
that ingests `router-proxy` (keyed re-mint, no bytes move),
`localfile` (operator-gated, symlink-resolved containment), and
`http`/https (operator-gated, SSRF-guarded via `security.url_guard`:
public-IP-only, resolve-then-pin, no redirects, size/time caps), and
normalises **every** input to a `router-proxy` ref. It is the right
place — and the right *code* — to do this resolution for LLM
requests too.

## 2. Goals / non-goals

**Goals**

- An agent passes **any `ProxyFile` the existing attachment
  resolver can ingest** (`router-proxy`, and the operator-gated
  `localfile` / `http`) into an LLM message; the router resolves
  bytes and the model sees the image/document.
- Agent→router bandwidth drops to ~the size of the ref (hundreds of
  bytes) regardless of file size; the frame-cap footgun disappears
  for this path.
- Storage-less agents can forward inbound attachments to an LLM with
  no byte handling.
- **Reuse `_resolve_attachments` itself** — same code, same
  per-protocol authz, same SSRF/containment guards, same operator
  gates. No new ingestion, authz, or security surface.
- Backward-compatible at the wire (no new `LlmRequestFrame` field).

**Non-goals**

- Replacing `image_part()`/`document_part()` — those stay the right
  tool for agent-*generated* / local bytes.
- Provider-native large-file upload (Gemini File API, Anthropic
  Files, OpenAI Files) is a **Phase 2 seam**, not P1.
- Adding a protocol allowlist that differs from the task path —
  the LLM path supports *exactly* what `_resolve_attachments`
  supports, because it is the same function. Changing the resolver's
  protocol set / authz / gates is out of scope (a separate change
  that would benefit both paths equally).

## 3. Design space — why an in-content part, not a frame sidecar

The obvious move — add `attachments: list[ProxyFile]` to
`LlmRequestFrame`, mirroring `NewTaskFrame`/`ResultFrame` — is
**wrong here**. A task payload is a single opaque blob with files
"alongside" it; an LLM message is **positional and multi-part**
(`content: list[part]`, interleaved text/image/document). A flat
sidecar list loses *where* in the conversation the file belongs and
*which* of N files maps to which prompt span.

So the reference must live **as a content part, at the position the
agent wants it**, symmetric with the existing neutral parts:

```jsonc
// New neutral content part:
{"file_ref": {
    "proxy": { /* ANY ProxyFile: protocol + path (+ key for
                  router-proxy) + mime_type + … — the router's
                  resolver normalises it, exactly as for a task
                  attachment */ },
    "as": "image"            // | "document"  (which neutral envelope
                             //   to materialise into)
}}
```

`as` is an explicit discriminator, not MIME-sniffed — consistent
with the deliberate `image` vs `document` split (Anthropic gates
them on different block types; the agent owns the semantic signal).
When omitted the resolver infers `image` for `image/*` else
`document`.

Rejected alternatives:

- **`LlmRequestFrame.attachments` sidecar** — loses positionality
  (above).
- **Resolve in the SDK** — that *is* today's status quo we're
  removing.
- **Resolve per-provider** — the resolver instead produces the
  *existing* neutral `{"image"|"document": {...}}` envelope, so all
  three provider adapters' part converters stay **unchanged**. One
  resolution path, not three.
- **Restrict to `router-proxy` only** (this draft's first cut) —
  rejected on review: `_resolve_attachments` *already* ingests
  `localfile` and `http` behind their operator gates + SSRF guard,
  so an artificial protocol restriction is both unnecessary and a
  drift risk. Reuse the resolver whole; whatever it accepts, the
  LLM path accepts identically.

## 4. Wire shape & compatibility

No `LlmRequestFrame` field change — `file_ref` is just another dict
in the existing `messages[i].content` list (`messages:
list[dict[str, Any]]`). Pydantic is not involved in part shape; the
adapters key-dispatch on dict keys.

**Roll order (router before agents).** An old router has no
resolver, so a `file_ref` part would reach a provider adapter that
doesn't recognise the key and would drop/garble it. Therefore:
upgrade the router before any agent emits `file_part()`. This is the
same roll rule already documented for additive protocol features.
The SDK gates emission behind the helper, so an un-upgraded fleet
simply keeps using `image_part()`.

## 5. SDK surface

```python
# bp_sdk/llm.py
def file_part(
    pf: ProxyFile,
    *,
    as_: Literal["image", "document"] | None = None,  # infer from mime
) -> dict[str, Any]: ...
```

- Returns `{"file_ref": {"proxy": pf.model_dump(), "as": <resolved>}}`.
- **Thin packager — no protocol allowlist in the SDK.** It accepts
  any `ProxyFile` and lets the router's reused resolver enforce
  per-protocol rules (key for `router-proxy`; operator gate +
  containment for `localfile`; operator gate + SSRF guard for
  `http`). Putting the allowlist only in the router is deliberate:
  one source of truth, no SDK↔router drift on what's permitted, and
  operators flip a server setting without re-releasing agents.
- `image_part`/`document_part` unchanged (inline-bytes path).

The headline ergonomic — forward an inbound attachment to the LLM
with **zero** byte handling, works for storage-less agents:

```python
@agent.handler
async def describe(ctx, payload: LLMData) -> AgentOutput:
    parts = [{"text": payload.prompt}]
    parts += [file_part(pf) for pf in ctx.files.inbound()]   # refs only
    resp = await ctx.llm.generate([Message(role="user", content=parts)])
    return AgentOutput(content=resp.text)
```

Inbound `ctx.files.inbound()` refs are already `router-proxy` with a
fresh task-user-scoped key, so they are directly usable; so is an
agent's own `ctx.files.put()` result. An agent constructing a
`ProxyFile(protocol="http", path=...)` or `localfile` ref works too
**iff the operator has enabled that ingestion** — the same setting
that already governs task attachments.

## 6. Router resolution

**Lift `_resolve_attachments` into a shared, caller-agnostic
`resolve_proxyfiles(state, *, user_id, caller_agent_id,
refs) -> list[ProxyFile]`** (move `tasks.py:644-894` as-is; the
task admit path becomes a thin caller). It already does, per
protocol, exactly what the LLM path needs and normalises every
input to a `router-proxy` ref carrying `sha256` + a fresh
task-user-scoped key:

- `router-proxy` → keyed-token verify + `grant.file_id == file_id`
  (no user filter — the documented capability/forwarding model) +
  `get_file_by_id` + re-mint.
- `localfile` → `file_ingest_localfile_allowed_agents` gate +
  symlink-resolved containment under
  `file_ingest_localfile_allowed_root` + ingest.
- `http` → `file_ingest_http_enabled` gate + `url_guard`
  (public-IP-only, resolve-then-pin, no redirects, size/time caps;
  refusal is opaque) + ingest.
- anything else → rejected (`unsupported_attachment`).

The only refactor `_resolve_attachments` needs: raise a
**caller-agnostic** error instead of `AdmitError` (e.g.
`AttachmentResolutionError(code, msg)`), which the task path maps to
`AdmitError` and the LLM path maps to `LlmCallError`.

The LLM resolver (`bp_router/llm/attachments.py`), invoked from
`LlmService.generate` and the streaming setup (`service.py:499` /
`_generate_stream_with_setup_retry`) **after** preset `_resolve`,
**before** `adapter.generate(messages, ...)`:

1. Collect every `file_ref` part across all messages; enforce the
   per-request count cap (§7).
2. `resolved = await resolve_proxyfiles(state, user_id=...,
   caller_agent_id=..., refs=[p["proxy"] for p in file_refs])` —
   one call; all protocols, gates, SSRF handled inside.
3. For each resolved `router-proxy` ref: size policy (§7). If
   inline-eligible, stream bytes via `FileStore.open(ref.sha256)` →
   base64 → **replace the `file_ref` part in place** with the
   existing neutral envelope `{ "<as>": {"mime_type": ref.mime_type,
   "data": <b64>, "display_name"?: ref.original_filename} }`. `<as>`
   = the part's explicit value, else inferred from `ref.mime_type`.
4. Any resolver failure (gate denied, bad/expired key, SSRF-blocked,
   not found, too large) → fail the LLM request as `LlmCallError`.
   Never silently drop — a missing image changes the model's answer.

`user_id`/`caller_agent_id` come from `LlmRequestFrame`
(`user_id`/`task_id`) + the request's authenticated agent — the same
inputs `_resolve_attachments` already takes, so the gates
(localfile-allowed-agents, http-enabled) evaluate identically to the
task path.

After the resolver runs, every part is plain text, a
provider-native passthrough, or the **already-existing** neutral
`image`/`document` envelope — so `gemini._convert_part` /
`anthropic._convert_part` / `openai._convert_part` are untouched.

## 7. Size policy & the Phase-2 native-upload seam

Inline base64 still inflates the **router→provider** request and
every provider caps inline request size (Gemini ≈20 MB total
request; Anthropic/OpenAI similar order). So:

- `settings.llm_attachment_inline_max_bytes` (default conservative,
  e.g. 5 MiB) — at or under, inline as in §6.
- `settings.llm_request_max_file_refs` (default e.g. 16) — bound the
  count so one request can't make the router stream/base64 unbounded
  data (DoS bound; the keyed model authorises *access*, not
  *volume*).
- Over the inline cap → **Phase 2**: a per-adapter
  `upload_file(sha256, mime, stream) -> provider_ref` hook (Gemini
  File API / Anthropic Files / OpenAI Files), result cached by
  `sha256` so repeated turns reuse the handle; the `file_ref` part
  is then replaced with the provider-native reference part instead
  of an inline envelope. P1 ships inline-only and returns a clear
  "file too large for inline; provider-native upload not yet
  supported — use the provider File API and pass the native part"
  error above the cap (today's documented escape hatch still works).

## 8. Security

- Because the LLM path **calls the same `resolve_proxyfiles`**, it
  inherits every existing control with zero new surface to review:
  the `router-proxy` keyed-capability model (`verify_file_fetch_token`
  + `grant.file_id == file_id`, no user filter — the documented
  forwarding model, `security.md` §10); the `localfile`
  allowed-agents gate + symlink-resolved containment; the `http`
  enable-gate + `url_guard` SSRF defence (public-IP-only,
  resolve-then-pin, no redirects, opaque refusal). This is strictly
  *safer* than the original router-proxy-only draft — there is no
  second, lesser-reviewed ingestion path.
- The **same operator settings govern both** task-attachment and
  LLM-request ingestion (`file_ingest_http_enabled`,
  `file_ingest_localfile_allowed_*`). An operator who has not
  enabled http ingestion for tasks has not enabled it for LLM
  requests either — consistent posture, one knob.
- `LlmRequestFrame` already carries `user_id` / `task_id` for
  audit/metric context. Parity decision: task attachments are *not*
  separately audited (the keyed fetch is the control); LLM-side
  resolution follows the same parity — emit a metric
  (`llm_attachment_resolved_total{outcome}` + bytes) but no new
  audit event, to avoid asymmetric noise.
- Resource caps (`§7`) are the abuse bound: access is keyed, volume
  is capped per request and per file.

## 9. Implementation sequence

1. `file_part()` + the `file_ref` neutral part contract
   (`bp_sdk/llm.py`); `ProxyFile`-validity guard.
2. Extract `_resolve_attachments` (`tasks.py:644-894`) into a shared
   `resolve_proxyfiles(...)` raising a caller-agnostic
   `AttachmentResolutionError`; `tasks.admit_task` becomes a thin
   caller mapping it to `AdmitError`. **Pure refactor — no behaviour
   change; existing task-attachment tests are the regression net.**
3. `bp_router/llm/attachments.py`: collect `file_ref` parts → one
   `resolve_proxyfiles` call → `FileStore.open` → base64 → in-place
   replacement; size/count caps; map failure to `LlmCallError`.
4. Hook into `LlmService.generate` + stream setup before
   `adapter.generate`.
5. Settings: `llm_attachment_inline_max_bytes`,
   `llm_request_max_file_refs`.
6. Docs: `services.md` §1.4 (the new ref path vs `image_part`,
   when to use which), `core.md` §7/§10 (drop the "must fetch +
   inline yourself" framing for inbound files; the footgun warning
   becomes "or pass the ref and let the router resolve it").
7. Tests: the shared-resolver extraction keeps all existing
   `_resolve_attachments` tests green (router-proxy/localfile/http +
   SSRF/gate cases) — now also exercised via the LLM caller;
   LLM-specific: `file_part` shape + `as` inference, in-place
   replacement preserves part order/position, multi-ref, count/size
   caps, `AttachmentResolutionError → LlmCallError` mapping,
   end-to-end `file_ref → neutral → provider-native`,
   roll-order/back-compat pin.
8. **Phase 2** (separate): per-adapter `upload_file` + sha256 handle
   cache + lifecycle.

## 10. What not to do

- Don't add `LlmRequestFrame.attachments` (loses positionality).
- Don't resolve per-provider (resolve once to the existing neutral
  envelope; adapters stay untouched).
- Don't fork or re-restrict the resolver. The LLM path supports
  exactly the protocols the task path does *because it is the same
  function*. No SDK-side protocol allowlist (drifts from the
  router's gates), no user-scoped authz for this path (would break
  delegation/forwarding parity and fork the security model). If a
  protocol/authz/gate should change, change `resolve_proxyfiles`
  once — both paths move together.
- Don't silently drop an unresolvable `file_ref` — fail loud
  (`LlmCallError`); a missing image silently changes model output.
- Don't inline beyond the cap "to be helpful" — that just moves the
  frame-size trap from agent→router to router→provider.

## 11. Open questions

- Exact inline cap default — provider-min-aware vs a single
  conservative constant. Lean: single constant in P1, per-provider
  refinement with Phase 2.
- ~~`localfile` for embedded agents?~~ **Resolved (review):** all
  protocols `_resolve_attachments` supports — `router-proxy`,
  operator-gated `localfile`, operator-gated/SSRF-guarded `http` —
  work in P1, because the LLM path reuses that resolver wholesale.
  No protocol restriction.
- Streaming requests: resolution happens once at setup (before the
  first delta), so it composes with the existing pre-first-delta
  retry boundary unchanged — confirm no interaction with fallback
  chain (resolver runs before adapter selection per fallback hop, or
  once before the chain? Lean: once before the chain — the resolved
  bytes are provider-agnostic).

## 12. Review log

- **v2 (pre-merge review):** the v1 draft restricted the path to
  `router-proxy` refs and proposed lifting only the keyed-verify
  block. Corrected: `_resolve_attachments` is *already* a general
  multi-protocol resolver (`router-proxy` + operator-gated
  `localfile` + operator-gated/SSRF-guarded `http`) that normalises
  everything to a `router-proxy` ref. The design now reuses that
  whole function (extracted into a shared `resolve_proxyfiles`),
  so the LLM path supports the full protocol set with no new
  ingestion/authz/SSRF surface — strictly safer than a second
  bespoke path, and storage-less / URL-only agents are supported
  out of the box.
