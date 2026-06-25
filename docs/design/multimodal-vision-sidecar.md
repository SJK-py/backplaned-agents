# A separate, configurable multimodal model for reading files

> **Status: Phase 1 implemented.** `SUITE_DEFAULT_PRESET_MULTIMODAL` +
> `SUITE_TEXT_ONLY_PRESETS`, the intent-carrying `read_file`, and the
> vision sub-call routing live in `bp_agents/common/loop.py`
> (`_vision_read_file`), gated by `common.multimodal_preset_for` and wired
> into the orchestrator + l1 turns. Phases 2–3 (proactive safety net for
> context-less images; caching + per-user preset) are not built yet.
>
> **Mime gate (authoritative):** `_vision_read_file` `stat`s the file for
> its real `mime_type` (via `FileStash.stat`) and gates on that — image/*
> or PDF route to the vision preset, everything else falls through to the
> normal `file_ref` path. It falls back to the name's extension only when
> the blob has no stored mime, so an extension-less or mislabelled image is
> still caught. If `stat` reports the name unbound it falls through, so the
> normal `read_file` surfaces the usual `not_found`. (Earlier this gated on
> the extension alone; the `stat` command — `router-managed-file-store.md`
> §4.3 — made the authoritative check possible.)

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

## 3. Recommended design — a config-gated vision sidecar at `read_file`

Two operator settings declare (a) a **multimodal preset** and (b) which
presets are **text-only**. When a turn's preset is text-only and an
image/PDF is read, transcribe it through the multimodal preset in a
**self-contained sub-`generate`** and feed the resulting **text** back to
the main model — with the **intent authored by the main model** (§3.2).
The main reasoning model never needs to be multimodal.

### 3.1 Configuration — two settings, config-driven gating

Two new suite settings:

```python
# bp_agents/settings.py
default_preset_multimodal: str = ""       # env: SUITE_DEFAULT_PRESET_MULTIMODAL
text_only_presets: list[str] = []         # env: SUITE_TEXT_ONLY_PRESETS
```

  * **`default_preset_multimodal`** — the vision preset the sidecar uses.
    A **router preset name** (e.g. `gemini_flash_vision`), *not* raw creds:
    the sub-call goes through `ctx.llm.generate(preset=...)`, so it
    inherits the router's retry/fallback/tier-gate/secret/metrics machinery
    for free. (Strictly better than the `md_ocr_*` raw-creds shape, which
    only exists because of MarkItDown's sync-library constraint.)
  * **`text_only_presets`** — the presets the operator declares
    **not multimodal-capable**. The gate keys on the **resolved preset
    name** of the turn, so heterogeneous tiers (a text-only orchestrator
    preset, a multimodal `pro` preset, user-selected overrides via
    `selectable_presets_*`) each get the right `read_file` independently —
    no single "current preset" assumption.

**The gate.** For a given turn the vision proxy engages iff
`default_preset_multimodal` is set **AND** the turn's resolved preset ∈
`text_only_presets` **AND** the file is image/PDF. All three matter:

  * Either var unset / list empty → **today's behaviour exactly** (plain
    `read_file` → `file_ref`; a multimodal model ingests it natively).
  * The mime clause keeps a text-only model's `read_file` on a `.txt` /
    `.json` from burning a vision call — the router already resolves those
    to plain text parts (`attachments.py:118`). *(Dependency: a suite-side
    mime/stat lookup on the stash file; verify `ctx.files` exposes one.)*

This is **deterministic** — capability is declared, not discovered by a
provider 400. That's the key win over a reactive trigger, and it lets the
tool surface itself honestly (§3.3): the model only sees the `purpose` arg
when the proxy is actually active.

  * Later, a per-user `preset_multimodal` `user_config` column +
    `selectable_presets_multimodal`, paralleling the existing chat-tier
    preset plumbing. Out of scope for phase 1.

### 3.2 The core risk — perception decoupled from intent

This is the hard part, and it dictates the rest of the design. When the
main model sees the raw image, it sees it **with full conversation
context** — it knows what it's looking for. Substitute text from a
separate model and you've split **perception** (the vision model, which
has the pixels) from **intent** (the main model, which has the context).
The failure mode is a generic *"describe everything"* prompt: it either
floods the main model with irrelevant detail or summarizes away the exact
number / cell / label it needed. No fixed prompt fixes this — the missing
ingredient is *what the main model wants*.

The principle that resolves it: **intent must be authored by the party
that holds the context — the main model — and handed to the vision
model.** Three consequences:

  1. **Make intent explicit — `read_file(name, purpose=…)`.** The main
     model writes the question when it calls the tool
     (`read_file("invoice.pdf", purpose="get the total due and the due
     date")`). The vision model receives **image + purpose** and extracts
     against it. This is strictly better than reconstructing intent from
     context, because the main model knows its own intent. It makes the
     intent-carrying `read_file` the **primary** path (§3.3), not an
     afterthought.
  2. **Layer the context** given to the vision model:
       * *primary* — the `purpose` the main model wrote;
       * *ambient* — a **plain-text transcript** of the recent turn (the
         user's request + the agent's stated reason), flattened to text so
         there's no cross-provider structured-content issue, for when
         `purpose` is thin;
       * *faithfulness system prompt* — transcribe text **verbatim**
         (preserve numbers / URLs / tables as markdown), describe layout,
         and **explicitly state when something the purpose asks for is
         absent**, flagging illegibility / uncertainty instead of
         guessing. A confidently hallucinating proxy is worse than a
         blocked read.
  3. **Make it interactive, not one-shot.** Because the interface is now
     text, the main model can **re-query** — call `read_file` again with a
     sharper `purpose` if the first answer is thin. It interrogates the
     image through the vision model as its "eyes." This is what recovers
     the fidelity any single projection loses, and it works for
     user-attached images too (they land in the stash with a name the
     model can `read_file` against).

### 3.3 Config-driven tool building + two seams

The gate (§3.1) is known at **tool-build time**: the agent computes one
flag per turn —

```python
proxy = bool(settings.default_preset_multimodal) and preset in settings.text_only_presets
```

— and passes it into `run_llm_loop`. That flag drives **separate tool
building** and the dispatch route:

  * **Intentful `read_file` (PRIMARY, high quality).** When `proxy` is
    true the loop advertises the **intent-carrying** `read_file` spec (the
    optional `purpose` arg appears *only* here — no prompt clutter when the
    main model is multimodal) and intercepts its dispatch in
    `_dispatch_tool_call` (`bp_agents/common/loop.py:84`) — *not* in
    `bp_sdk`'s `dispatch_file_tool`, which has no `ctx.llm` / preset
    access. For an image/PDF stash file it runs the vision sub-call (image
    + purpose + ambient context + faithfulness prompt) and returns its
    text; a text/other file falls through to the normal `file_ref`. When
    `proxy` is false the plain `read_file` is built and nothing changes.
    `bp_sdk` stays the generic fallback; the suite owns the routing.
  * **Proactive safety net (degraded, for context-less images).**
    Subagent-returned and user-attached images reach the main model with
    **no authored intent**. Because `proxy` already tells us the preset is
    text-only, the loop can transcribe any `file_ref` in the outgoing
    messages **before** the call (in `_generate_resilient`, `loop.py:205`)
    rather than waiting for a 400 — non-blind, but with ambient context
    only, so it's weaker. Frame it as "don't be blind," not "be great";
    the model escalates via an intentful `read_file(purpose=…)` on the same
    stash file when it needs specifics. (Phase 2 — §5.)

```python
async def _transcribe_file_ref(ctx, name, *, preset, purpose, context) -> str:
    sub = await ctx.llm.generate(
        preset=preset,
        messages=[
            Message(role="system", content=_VISION_FAITHFULNESS_PROMPT),
            Message(role="user", content=[
                {"text": _intent_block(purpose, context)},
                {"file_ref": {"name": name}},   # resolves against the VISION model
            ]),
        ],
    )
    return f"[Contents of {name}, read by the vision model for: {purpose}]\n{sub.text}"
```

### 3.4 The honest ceiling

A text proxy never fully equals native multimodal grounding: the main
model can't re-attend to a region the vision model didn't mention without
another round-trip. So the sidecar buys **flexibility and cost** (a strong
text reasoner paired with a cheap/separate vision model), **not parity**.
When fidelity is critical, the right lever remains making the main preset
multimodal. State this plainly rather than overselling the sidecar.

### 3.5 Why a separate sub-call, not "switch the turn's preset"

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

## 4. Cost & correctness notes

  * **Latency.** Both paths are *proactive* (the gate is config-driven, not
    400-discovered): one vision sub-call substitutes for the file_ref, no
    rejected round-trip. A multimodal main model (preset ∉
    `text_only_presets`) never enters the sidecar — zero overhead, full
    backwards-compat.
  * **`task_id` is present** on the sub-call (`ctx.llm.generate` threads
    `ctx.task_id`), which the router requires to resolve a `file_ref`
    (`attachments.py` scope derivation). The vision preset's file scope is
    the same task, so the sub-call can read the same stash file.
  * **Text files & reference types are untouched** — the mime clause routes
    only image/PDF through the vision preset; text resolves to plain text
    parts as today. No wasted vision calls on `.txt`/`.json`.
  * **Misconfiguration.** `text_only_presets` non-empty but
    `default_preset_multimodal` unset → the proxy can't engage; log a
    startup warning rather than silently feeding images to a text-only
    model.
  * The vision preset's own `min_user_level` is enforced by the router, so
    a low-tier user can't reach a premium vision model through the back
    door.

## 5. Phasing

Reordered from the first draft: because intent is what makes the proxy
usable (§3.2), the **intentful `read_file` path leads**, and the
context-less reactive seam follows as a safety net.

1. **Phase 1 — intentful `read_file` (the primary path).** The two
   settings (`default_preset_multimodal`, `text_only_presets`) + the
   `proxy` flag threaded into `run_llm_loop` + the intent-carrying
   `read_file` spec (advertised only when `proxy`) + the vision sub-call
   (`_transcribe_file_ref`, faithfulness prompt, ambient context)
   intercepted in `_dispatch_tool_call` for image/PDF stash files.
   Requires a suite-side mime lookup on the stash file (verify `ctx.files`
   exposes one; if not, add a lightweight stat). Unset → byte-for-byte
   current behaviour.
2. **Phase 2 — proactive safety net.** Reuse the helper in
   `_generate_resilient`: when `proxy` is set, transcribe any `file_ref`
   in the outgoing messages (subagent-returned / user-attached images that
   carry no authored intent) before the call, with ambient context only.
   Degraded but non-blind; the model escalates via an intentful
   `read_file(purpose=…)`.
3. **Phase 3 — caching + per-user.** Cache a file's transcription in the
   stash keyed by `(name, purpose)` so repeat reads are free; add the
   `preset_multimodal` user_config column + `selectable_presets_multimodal`,
   paralleling the existing chat-tier preset plumbing.

## 6. Alternatives considered

  * **Router-side transparent vision pre-pass** (resolve image/document
    parts through a configured vision preset whenever the target preset is
    flagged non-multimodal). Most general — covers every agent, not just
    the suite — but: needs a new per-`Preset` capability flag (none exists,
    `presets.py:49`), makes the router issue a nested LLM call inside an
    LLM call, and the config lands in *router* settings, not the
    `SUITE_` env var the user asked for. Heavier; defer unless a
    non-suite consumer needs it.
  * **Per-call preset switch** — rejected in §3.5 (cross-provider
    tool-call-id + reasoning-block hazards).
  * **`md_converter`-only** (tell the model to convert files to Markdown).
    Already possible, but it's document text-extraction/OCR, not image
    understanding, and it leans on the model choosing to route around a
    failure. The sidecar gives true vision and is automatic.

## 7. Surface summary (phase 1)

  * `bp_agents/settings.py` — `default_preset_multimodal: str = ""` and
    `text_only_presets: list[str] = []`.
  * `bp_sdk/file_tools.py` — `file_tools(..., read_file_intent: bool=False)`
    toggles an optional `purpose` arg on the `read_file` spec (the plain
    `dispatch_file_tool` ignores it; the suite loop reads it).
  * The agents (orchestrator / `l1_common`) compute
    `proxy = bool(default_preset_multimodal) and preset in text_only_presets`
    and pass it + the vision preset + recent-turn context into
    `run_llm_loop`.
  * `bp_agents/common/loop.py` — when `proxy`: build the intent-carrying
    `read_file` spec, and in `_dispatch_tool_call` (`loop.py:84`) intercept
    `read_file` for image/PDF stash files → `_transcribe_file_ref(...)`
    (vision sub-call + faithfulness prompt + ambient-context block); else
    fall through to the normal `file_ref`.
  * `_VISION_FAITHFULNESS_PROMPT` — verbatim transcription, preserve
    structure, report absence, flag uncertainty, no guessing.
  * Tests — a fake `ctx.llm` asserting: preset ∈ `text_only_presets` +
    image → `read_file` returns the sub-call's text and the sub-call
    received the `purpose`; text file → no sub-call (plain `file_ref`);
    preset ∉ list, or vision preset unset → today's behaviour (intent arg
    absent).
  * Docs — note in `docs/agent-suite/` that a text-only chat preset can be
    paired with `SUITE_DEFAULT_PRESET_MULTIMODAL` +
    `SUITE_TEXT_ONLY_PRESETS` for image/PDF reading, and that quality
    depends on the model's `purpose`.
