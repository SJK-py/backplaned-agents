# A separate, configurable multimodal model for reading files

> **Status: proposal.** Review of "let a text-only agent model read
> images/PDFs by configuring a separate multimodal model via env var,"
> plus a recommended design. No code landed yet.

## 1. The gap today

`read_file` shows a stash file to the model by returning a `file_ref`
part (`bp_sdk/file_tools.py:185`, `files.llm_ref`); the **router**
resolves it on the next `generate` into provider-neutral content
(`bp_router/llm/attachments.py:278` `resolve_request_file_refs`). The
modality is decided by mime (`attachments.py:118` `_classify`):

  * `text/*`, JSON, YAML, CSV, … → a **text** part (decoded UTF-8).
  * `image/*` → an **image** part; `application/pdf` → a **document**
    part — both base64 multimodal envelopes.
  * anything else → a text *reference note* (not inlined).

So the multimodal requirement is **narrower than it looks**: text files
already work on any model. The gap is **images and PDFs** — those reach
the model only as image/document envelopes, and if the agent's preset
points at a text-only model the provider 400s. Today that is handled
**reactively**: `_generate_resilient` catches the non-retriable error and
`_strip_unfeedable_file_refs` (`bp_agents/common/loop.py:174`) swaps each
`file_ref` for a note — *"this backend can't read that file type; convert
it to text or Markdown first."* The file is effectively lost to the model
unless it then routes around it (e.g. via the `md_converter` agent).

The operator's only lever is the agent's chat preset
(`default_preset_pro/balanced/lite`, `bp_agents/settings.py:60`). To read
images they must make the **whole reasoning model** multimodal — they
can't pair a strong text reasoner with a cheap vision model.

## 2. What already exists (and why it's not enough)

  * **`md_converter` agent + `md_ocr_*` settings** (`settings.py:386`). A
    file→Markdown converter with optional LLM-vision OCR, configured via a
    *dedicated, separate, env-driven* vision model (`md_ocr_model`,
    `md_ocr_api_key`, `md_ocr_base_url`). This is the precedent for what
    the user wants — but it solves a different problem: extracting **text**
    from documents/scans. It does not give the reasoning model genuine
    **image understanding** ("what's in this chart / screenshot / photo?"),
    and it only ran OCR because MarkItDown is a third-party *sync* library
    that couldn't use the router's frame channel — a constraint that does
    **not** apply to us here.
  * **The reactive strip/recover seam** (`_generate_resilient`). The right
    *place* to hook, but today it only degrades (note + drop), it doesn't
    recover the content.
  * **`file_ref` `as_` override** (`files.py:269`) and the router preset
    system (retries, fallback, tier-gating, secret resolution,
    metrics) — infrastructure we should reuse, not reinvent.

## 3. Recommended design — a vision sidecar on the recovery seam

Add an operator-configured **multimodal preset** and, when an image/PDF
can't be fed to the main model, transcribe it through that preset in a
**self-contained sub-`generate`** and feed the resulting **text** back to
the main model. The main reasoning model never needs to be multimodal.

### 3.1 Configuration (reuse presets, not raw creds)

One new suite setting, mirroring the existing tier defaults:

```python
# bp_agents/settings.py
default_preset_multimodal: str = ""   # env: SUITE_DEFAULT_PRESET_MULTIMODAL
```

  * **Empty (default) → today's behaviour exactly**: file_ref straight to
    the main model; if it can't ingest it, strip-and-note as now.
  * **Set to a preset name** → the sidecar is active. The value is a
    **router preset** (e.g. `gemini_flash_vision`), *not* raw creds:
    the sub-call goes through `ctx.llm.generate(preset=...)`, so it
    inherits the router's retry/fallback/tier-gate/secret/metrics
    machinery for free. (This is strictly better than the `md_ocr_*`
    raw-creds shape, which only exists because of the sync-library
    constraint.)
  * Later, a per-user `preset_multimodal` `user_config` column +
    `selectable_presets_multimodal`, exactly paralleling the existing
    chat-tier preset plumbing. Out of scope for phase 1.

### 3.2 Mechanism — transcribe instead of strip

Replace the "strip → note" recovery with "transcribe → substitute text"
**when the multimodal preset is configured**. A new helper:

```python
async def _transcribe_file_refs(ctx, messages, *, preset: str) -> int:
    # For each {"file_ref": {"name": n}} part that the main model just
    # rejected: run a focused sub-call on the vision preset and replace
    # the part in place with {"text": <transcription>}.
    #   sub = await ctx.llm.generate(
    #       preset=preset,
    #       messages=[Message(role="user", content=[
    #           {"text": _VISION_INSTRUCTION},
    #           {"file_ref": {"name": n}},   # resolves against the VISION model
    #       ])],
    #   )
    #   part -> {"text": f"[Contents of {n}, read by the vision model]\n{sub.text}"}
```

Wire it into `_generate_resilient` (`loop.py:205`): on the non-retriable
content error, if `default_preset_multimodal` is set, call
`_transcribe_file_refs`; otherwise fall back to the existing
`_strip_unfeedable_file_refs`. The retry then re-runs the main model with
text in place of the image/PDF.

### 3.3 Why a separate sub-call, not "switch the turn's preset"

The obvious-but-wrong alternative is to detect multimodal content and
route *that whole generate* to the vision preset. It breaks on this
codebase's own invariants:

  * `LlmService` **refuses cross-provider fallback when the messages
    carry tool_call_ids** (`service.py:790`, `_messages_have_tool_call_ids`):
    the IDs and reasoning blocks are provider-shaped, and replaying them
    through a different provider 400s. A mid-loop preset switch hits
    exactly this.
  * The main loop round-trips reasoning blocks / thought-signatures
    (`assistant_from_response`); swapping models mid-conversation corrupts
    that.

A self-contained sub-`generate` (its own one-shot message list, no tool
history) sidesteps all of it. The main loop stays 100% on the main
preset and only ever sees text. This is the decisive reason to prefer a
sidecar over a router-side or per-call model switch.

### 3.4 Coverage

Hooking the **recovery seam** (not `read_file` specifically) makes this
**universal**: every `file_ref` that reaches the main model is covered —
`read_file`, a subagent returning files via `tool_response_from_result`,
and user-attached images alike. `read_file` is not the only producer, so
a `read_file`-only hook would miss the others.

## 4. Cost & correctness notes

  * **One extra 400 per file-bearing turn** on a text-only main model
    (the reactive trigger). Acceptable: it only happens when the main
    model genuinely can't read the file, and never for a multimodal main
    model (no 400, no sidecar, zero overhead — full backwards-compat).
    Phase 2 can go *proactive* (below) to drop the 400.
  * **`task_id` is present** on the sub-call (`ctx.llm.generate` threads
    `ctx.task_id`), which the router requires to resolve a `file_ref`
    (`attachments.py` scope derivation). The vision preset's file scope is
    the same task, so the sub-call can read the same stash file.
  * **Text files & reference types are untouched** — they never 400, so
    they never enter the sidecar. No wasted vision calls on `.txt`/`.json`.
  * The vision preset's own `min_user_level` is enforced by the router, so
    a low-tier user can't reach a premium vision model through the back
    door.

## 5. Phasing

1. **Phase 1 (this proposal).** `default_preset_multimodal` setting +
   `_transcribe_file_refs` helper wired into `_generate_resilient`.
   Reactive, universal, ~80 lines + tests. Unset → byte-for-byte current
   behaviour.
2. **Phase 2 — proactive + intent.** At `read_file`, mime-gate on
   image/PDF and transcribe **before** the main call (drops the 400
   round-trip); add an optional `purpose`/`question` arg to `read_file`
   so the model tells the vision model what to look for (recovers the
   fidelity lost by going through text). Requires a suite-side mime lookup
   on the stash file (verify `ctx.files` exposes it).
3. **Phase 3 — caching + per-user.** Cache a file's transcription in the
   stash (e.g. `"<name>.vision.md"`) so re-reads are free; add the
   `preset_multimodal` user_config column + `selectable_presets_multimodal`.

## 6. Alternatives considered

  * **Router-side transparent vision pre-pass** (resolve image/document
    parts through a configured vision preset whenever the target preset is
    flagged non-multimodal). Most general — covers every agent, not just
    the suite — but: needs a new per-`Preset` capability flag (none exists,
    `presets.py:49`), makes the router issue a nested LLM call inside an
    LLM call, and the config lands in *router* settings, not the
    `SUITE_` env var the user asked for. Heavier; defer unless a
    non-suite consumer needs it.
  * **Per-call preset switch** — rejected in §3.3 (cross-provider
    tool-call-id + reasoning-block hazards).
  * **`md_converter`-only** (tell the model to convert files to Markdown).
    Already possible, but it's document text-extraction/OCR, not image
    understanding, and it leans on the model choosing to route around a
    failure. The sidecar gives true vision and is automatic.

## 7. Surface summary (phase 1)

  * `bp_agents/settings.py` — `default_preset_multimodal: str = ""`.
  * `bp_agents/common/loop.py` — `_transcribe_file_refs(...)`; branch in
    `_generate_resilient` to prefer it over `_strip_unfeedable_file_refs`
    when the preset is set. Thread the preset in via `run_llm_loop` (it
    already has `ctx` and `preset`).
  * Tests — a fake `ctx.llm` asserting: configured → file_ref replaced by
    the sub-call's text and the main retry succeeds; unset → existing
    strip-note path; text files never trigger a sub-call.
  * Docs — note in `docs/agent-suite/` that a text-only chat preset can be
    paired with `SUITE_DEFAULT_PRESET_MULTIMODAL` for image/PDF reading.
