# Router-managed named file store

Replace the `ProxyFile`-based out-of-band file model with a
first-class, router-managed named file store: agents reference
files by **name** (`{filename}` or `persist/{filename}`) in a
per-user, per-session shared stash, store/fetch/manage them via
dedicated frames, and expose file operations to LLMs as tools.
Drop `ProxyFile` and the `attachments` channels entirely.

## 1. The gap today

`ProxyFile` is an **opaque reference** that must be threaded
explicitly through every hop: an agent puts bytes
(`ctx.files.put → ProxyFile`), passes the ref via
`NewTaskFrame.attachments` / `ResultFrame.attachments`, and the
router rewrites it to a fresh `router-proxy` ref scoped to each
recipient (`bp_router/attachments.py::resolve_proxyfiles`).

That works, but it forces a **pass-the-reference** discipline:

  * There is no shared namespace. Agent A cannot say "use
    `report.pdf`" — it must hold (and forward) the exact
    `ProxyFile` object. An agent that wasn't handed the ref can't
    reach the file at all.
  * Files are write-once, reference-scoped. There's no "list what's
    in my workspace", no "overwrite the draft", no "copy the
    template" — the model an agentic workflow actually wants.
  * The LLM can't manage files. It can be *handed* a file (via the
    just-merged `feed_llm` path) but can't list / read-on-demand /
    write / delete. A coding agent that wants to read `main.py`,
    edit it, and write it back has no surface for that.
  * The router makes **outbound fetches** for `http`-protocol refs
    (`resolve_proxyfiles` http branch) — an SSRF surface guarded by
    `security.url_guard`, but a surface nonetheless.

The motivating principle: **an agent should be able to refer to a
file by name in a shared stash and use it on demand**, and any
peer in the same user+session should understand that name without
being handed a ref.

## 2. Goals / non-goals

**Goals**

  * Per-user, S3-backed file store with a **named** address space.
  * Two scopes via reserved prefixes: ephemeral `{session_id}/`
    (auto-GC'd on session close, the hidden baseline) and
    persistent `persist/` (survives session close, user-wide).
  * Bare `{filename}` resolves to `{session_id}/{filename}`;
    `persist/{filename}` addresses the persistent scope.
  * Dedicated frames — `FileStoreFrame`, `FileFetchFrame`,
    `FileManageFrame` — fully replacing `ProxyFile` + `attachments`.
  * **No router-side outbound fetching.** Stores are
    upload-with-grant (the agent streams bytes under a content-bound
    credential — reuse `POST /v1/files/upload`); fetches are
    ephemeral, signed download URLs the agent pulls itself.
  * Per-user storage quota, enforced by the user's level.
  * SDK surface + a curated set of LLM file tools (read-only and
    full bundles).
  * `AgentOutput.files` becomes a `list[str]` of names auto-fed to
    an LLM parent as tool-result content.

**Non-goals**

  * Backwards compatibility for `ProxyFile` / `attachments` /
    `feed_llm` / `tool_response_from_result`. Hard cutover
    (pre-release; §10).
  * Cross-user file sharing. The bucket is per-user; a delegate
    runs in the same user scope so it shares the stash, but there
    is no cross-user reference.
  * Versioning / history. Overwrite replaces; there is no
    version log (a `persist/` copy is the manual snapshot
    mechanism).
  * Router-side URL ingestion (dropped, §1 / §9).

## 3. Storage model

**Address space.** A file is addressed by `(user_id, scope,
filename)`:

  * `scope = session:{session_id}` — the ephemeral baseline.
    Addressed by a bare `{filename}`.
  * `scope = persist` — the persistent, user-wide store. Addressed
    by `persist/{filename}`.

`persist/` is a **reserved prefix**: a bare filename beginning with
`persist/` is always the persistent scope, never a session file
literally named that. Storing a session file whose name starts with
`persist/` (or contains `/` at all — names are flat, no nested
paths in v1) is rejected `invalid_filename`.

**Name → blob directory.** A `files` table row maps
`(user_id, scope, filename) → (sha256, byte_size, mime_type,
stored_at)`. The S3 backend stays **content-addressed by sha256**
(the existing `FileStore.put(sha256, …)` interface, unchanged): the
directory row is the inode, the sha256 blob is the content. Two
different names with identical content point at the same blob
(transparent blob-level dedup — an optimization, invisible to name
semantics). Blob GC is refcount-by-directory-rows: when the last
name pointing at a sha256 is deleted, the blob is collectable (a
sweep, not inline).

**No global content index for naming.** Per §2-dedup-decision:
storing a *new* name just writes; only a *collision on an existing
name* triggers a content comparison (§5). Identical content under
*different* names is allowed and stored as two directory rows
(sharing one blob).

## 4. Frames

Three new frames, all carrying `user_id` + `session_id` (already
required on task frames — `bp_protocol/frames.py:126`) so the
router scopes every op authoritatively from the authenticated
context, never from an agent-asserted field.

### 4.1 `FileStoreFrame` — save bytes into the stash

The agent has already streamed bytes via the content-bound
upload-with-grant path (`POST /v1/files/upload` — reused as-is;
the grant fixes `sha256` + `byte_size` + `user_id`). The frame
binds the uploaded blob to a name:

```jsonc
{
  "type": "FileStore",
  "user_id": "...", "session_id": "...",
  "trace_id": "...", "span_id": "...",
  "sha256": "...",            // the blob just uploaded under grant
  "byte_size": 12345,
  "filename": "chart.png",    // optional; defaults to the upload's filename
  "persistent": false,        // false → session scope; true → persist/
  "dedup": "append_count",    // append_count | overwrite | error
  "mime_type": "image/png"
}
```

Router → agent reply: `Ack` carrying the **actual saved name**
(`chart.png`, or `chart_1.png` after a dedup append, or
`persist/chart.png` when `persistent=true`). The reply name is
authoritative — see the §5 invariant.

There is **no URL-fetch mode.** A blob the agent doesn't already
hold (a public URL) is the agent's job to fetch and upload. This
removes the router's outbound-fetch SSRF surface entirely.

### 4.2 `FileFetchFrame` — get an ephemeral download URL

```jsonc
{
  "type": "FileFetch",
  "user_id": "...", "session_id": "...",
  "filename": "chart.png"      // or "persist/chart.png"
}
```

Reply: `Ack` with a short-TTL signed URL (the existing
`issue_file_fetch_token` / `GET /v1/files/{id}` machinery, keyed
by the resolved sha256 + user scope). The agent pulls bytes over
plain HTTP itself. URL is one-shot / short-lived for security.

### 4.3 `FileManageFrame` — typed management commands

Carries a discriminated `command` object:

  * **`ListFileRequest`** `{ persistent: bool=false, query: str|null,
    stored_after: datetime|null, detail: bool=false }` → reply lists
    names in the chosen scope (filtered). `persist` listing returns
    `persist/{filename}` forms. With `detail=true` the reply carries
    `entries` (each `{ name, byte_size, mime_type, created_at }`,
    directory row JOINed to its blob's mime) instead of bare `names`,
    so a caller/model sees type + size without a per-file `stat`.
  * **`StatFileRequest`** `{ name }` — metadata for ONE name (`{filename}`
    or `persist/{filename}`): reply `stat` = `{ name, byte_size,
    mime_type, created_at }`, or `error="not_found"`. Read-only; the
    same single-row lookup `FileFetch` uses, JOINed to the blob for
    `mime_type`.
  * **`DeleteFileRequest`** `{ filename }` — `{filename}` or
    `persist/{filename}`; `*` glob allowed (e.g. `draft_*`).
    Deletes directory rows (blob GC is deferred to the sweep).
  * **`CopyFileRequest`** `{ src, dst, delete_original: bool=false }`
    — `cp` (or `mv` when `delete_original`). Cross-scope allowed
    (`report.pdf` → `persist/report.pdf` is the "promote to
    persistent" idiom). `dst` honours the same `dedup` rule.
  * **`WriteFileRequest`** `{ filename, text, persistent: bool=false,
    dedup }` — write a text file inline (no upload round-trip; the
    router hashes + stores the UTF-8 bytes). For text only; binary
    output uses upload-with-grant + `FileStore`.

All mutating commands append an audit hash-chain event
(`file.store` / `file.delete` / `file.copy` / `file.write`) the
same way task events do — file mutations are first-class auditable
actions.

## 5. Dedup + the load-bearing invariant

**Dedup is filename-scoped** (§2 decision):

  * Storing a name that doesn't exist → write it verbatim.
  * Storing a name that DOES exist → compare the incoming sha256
    to the stored one:
    * equal → no-op (idempotent; return the existing name).
    * differ → resolve per `dedup`:
      * `append_count` (default) → `name_1.ext`, `name_2.ext`, …
        (never lose data; the safe default).
      * `overwrite` → replace the directory row's blob pointer.
      * `error` → reject `filename_exists` (for the "create exactly
        this name or tell me" case — notably the LLM `write_file`
        tool).

**The invariant: every store/write/copy returns the ACTUAL saved
name, and callers reference the returned name — never assume the
requested one.** This is what makes the no-agent-prefix shared
namespace safe: a collision can only surprise a caller that
ignores the return value. The SDK and the LLM tool layer enforce
it by *only* surfacing the returned name (and, for the LLM tools,
appending an explicit "saved as `{actual}` (renamed from
`{requested}`)" line to the tool result when they differ).

**Name allocation is atomic.** The collision-check → append-counter
→ insert sequence runs under a row lock / unique constraint on
`(user_id, scope, filename)` so two concurrent stores of the same
name can't both grab `name_1.ext`. On unique-violation the router
retries the counter bump.

## 6. Session binding & lifecycle

  * **Frame ops (task handlers).** The `FileStore` / `FileFetch` /
    `FileManage` frames carry `task_id`; the router derives the
    authoritative `(user_id, session_id)` from the task row and
    authorises **iff the sender is the task's active executor**
    (`attachments.derive_task_file_scope`). This is the path for the
    agent currently running a task.
  * **HTTP ops (gateway agents).** A gateway agent — a channel / webapp
    that *spawns* tasks but is never their active executor, so it cannot
    use the frames — reaches the same stash over session-authed HTTP:
      - `POST /v1/files/names` — bind an already-uploaded blob (by
        `sha256`) to a name (`dedup` ∈ `append_count|overwrite|error`);
        returns the actual saved name.
      - `GET /v1/files/names` — list names in a scope (literal substring
        `query`).
      - `GET /v1/files/names/resolve` — resolve a name → `file_id`
        (+ a self-authorising fetch key for `GET /v1/files/{file_id}`).
    Auth is the caller's **session JWT**; a session-scoped name has its
    `session_id` ownership-checked exactly like `POST /v1/files`. Both
    paths share one implementation (`bp_router.file_store` — scope keys,
    dedup policy, quota gate), so they behave identically. *(This
    supersedes the earlier "HTTP rejected" note: frames stay the primary
    path for task handlers; HTTP is required for spawners, which have no
    task context to derive scope from.)*
  * **GC on session close.** `close_session`
    (`bp_router/api/sessions.py:75`) gains a step: delete all
    directory rows under `scope = session:{session_id}` and
    enqueue the now-unreferenced blobs for the sweep.
    `persist/` rows are untouched.
  * **The `persist/` scope is user-wide and survives every session.**
    (Clarifying the design note's "session-wide" — it is
    cross-session / user-wide, NOT bound to one session.)

## 7. Quota

Net-new (today's `bp_router/quota/` is only rate-limit key
constants). Per-user storage quota, ceiling set by the user's
level:

  * Tracked as `SUM(byte_size)` over the user's directory rows
    (session + persist), maintained incrementally on store / delete
    / write / copy.
  * Enforced at every byte-adding op (`FileStore`, `WriteFile`,
    `CopyFile` without `delete_original`). Over-ceiling →
    `Ack{accepted=false, reason="quota_exceeded"}`; the upload grant
    is refused before bytes are spooled.
  * `persist/` and session bytes count against one ceiling in v1
    (separate ceilings deferred — §13).

## 8. SDK surface

`ctx.files` is reshaped from ProxyFile-returning to name-based:

```python
class FileStash:
    # Store
    async def store(self, src: Path | bytes | AsyncIterable[bytes], *,
                    filename: str | None = None, persistent: bool = False,
                    dedup: Literal["append_count","overwrite","error"]
                        = "append_count") -> str: ...   # returns SAVED name
    async def write(self, filename: str, text: str, *,
                    persistent: bool = False, dedup=...) -> str: ...

    # Read bytes INTO THE AGENT's process (HTTP pull via FileFetch —
    # for an agent that needs to process the bytes itself). This is
    # NOT the LLM-feed path (§8.1): feeding a file to an LLM never
    # pulls bytes into the agent.
    async def read(self, name: str) -> Path: ...        # fetch → local path
    async def read_bytes(self, name: str) -> bytes: ...

    # Manage
    async def list(self, *, persistent: bool = False, query: str | None = None,
                   stored_after: datetime | None = None) -> list[str]: ...
    async def delete(self, name: str) -> int: ...        # supports '*'
    async def copy(self, src: str, dst: str, *, move: bool = False) -> str: ...

    # Reference a stash file in an LLM message by NAME (§8.1) — the
    # router resolves it into the provider call; no bytes cross the
    # agent→router frame.
    def llm_ref(self, name: str, *, as_: Literal["image","document"]
                | None = None) -> dict: ...   # → {"file_ref": {"name", "as"}}
```

`store` / `write` / `copy` **return the saved name** (the §5
invariant in the type signature). `read` / `read_bytes` are the
*agent-wants-bytes-locally* path and pull over HTTP (§4.2) — not
the LLM-feed path.

### 8.1 LLM feeding is router-resolved — never inline bytes at the agent

**Load-bearing correctness property.** Feeding a stash file to an
LLM MUST pass only the **file name as a reference** in the
`LlmRequestFrame`; the **router** resolves the name → bytes and
inlines them into the provider call, *after* the agent→router
frame. File bytes never ride that frame, so a file larger than the
WS `max_payload_bytes` cap (1 MiB default) is fed without tripping
it. This is exactly the path the current `ProxyFile` design already
follows: `file_part(pf)` puts a `{"file_ref": {…}}` in the message,
and `resolve_request_file_refs` (`bp_router/dispatch.py`) resolves
it *before* the provider adapter sees the messages
(`docs/design/llm-proxyfile-attachments.md`). The named store keeps
this path; only the ref shape changes:

```jsonc
// before (ProxyFile):  {"file_ref": {"proxy": {…ProxyFile…}, "as": "image"}}
// after  (named store): {"file_ref": {"name": "chart.png",    "as": "image"}}
```

> **Exception — the `read_file` text window.** The above holds for the
> multimodal feed path (images / PDFs ride a `file_ref`, router-resolved).
> But the `read_file` *tool* now reads a TEXT file's bytes locally
> (`read_bytes`, the §4.2 path) to return a bounded **character window**
> (`max_chars` / `offset`, default first 20000 chars) as a plain text tool
> result — so a large file is page-able and can't flood context. This is a
> deliberate agent-side read for slicing, not an LLM-feed inline; images /
> PDFs / non-UTF-8 files still return a `file_ref` and stay on the
> router-resolved path. See `bp_sdk/file_tools.py` (`_read_file`).

**Name resolution is scoped by an AUTHORITATIVE `(user_id,
session_id)` the router DERIVES from the task row — never from
agent-asserted frame fields.** The router reads
`LlmRequestFrame.task_id`, loads the task row, takes its `user_id`
+ `session_id`, and verifies the connecting agent is that task's
active executor — the exact pattern `FileUploadRequestFrame`
already uses (`bp_protocol/frames.py`: "the router derives the
owning `user_id` from the task row … Any `user_id` an agent might
try to assert is ignored"). A session-scoped bare name resolves
under the derived `session_id`; a `persist/` name under the
derived `user_id`'s persistent scope. The existing per-file inline
cap + over-cap error (`resolve_request_file_refs`) carry over
unchanged.

**Why derive, not carry a `session_id` field.** It is tempting to
add `session_id` to `LlmRequestFrame` (it already carries
`user_id` + `task_id`). Don't — for the same reason the router
must not trust the asserted `user_id` here. A named file's
authority IS the `(user_id, scope, filename)` tuple; there is no
per-file signed `key` the way `ProxyFile` carried one. The current
LLM path *can* trust `frame.user_id` (dispatch.py) only because a
ProxyFile `file_ref` also carries a signed fetch token bound to a
specific `file_id` + user — a lied `user_id` can't forge that key.
Strip the signed key (named store) and the tuple becomes the sole
authority, so an agent-asserted `user_id`/`session_id` would let a
malicious agent read another user's files by name. Deriving both
from the task row (and verifying active-executor) closes that, and
keeps one consistent identity-derivation pattern across all file
ops. **`task_id` is therefore REQUIRED on an `LlmRequest` that
carries name `file_ref`s**; a request with name refs but no
task_id is rejected (`file_ref_requires_task`) — the agent has no
authoritative scope without it.

Consequences for the two LLM-feed surfaces:

  * **`read_file(name)` tool** — its tool RESULT is a name
    `file_ref` (router-resolved), NOT inlined bytes. The model
    asks to "see" `chart.png`; the SDK emits a tool_response
    carrying `{"file_ref": {"name": "chart.png", …}}`; the bytes
    materialise at the router on the *next* `generate` call (scope
    derived from that call's `task_id`). The agent never fetches
    the bytes.
  * **`AgentOutput.files: list[str]`** — the producing agent
    returns names; the LLM parent threads each as a name `file_ref`
    into its `LlmRequest`. The router resolves them under the
    parent's task-derived scope. No `FileFetch`, no agent-side
    inlining.

`read` / `read_bytes` (§8 above) are the *opposite* path — they
exist for an agent that wants the bytes in its own process to
post-process. Those go over HTTP (`FileFetch`) and are unrelated
to LLM feeding.

**LLM tool bundles.** The SDK ships ready-made `ToolSpec` sets so
an LLM agent exposes file ops without hand-writing schemas:

  * `file_tools(bundle="read_only")` → `list_session_file`,
    `list_persist_file`, `read_file`.
  * `file_tools(bundle="full")` → adds `write_file`, `delete_file`,
    `copy_file`.
  * Individual tools importable for à-la-carte surfaces.

`read_file(name)` returns a name `file_ref` (§8.1) so the model
sees the file on the next turn without the bytes ever entering the
agent or the request frame. Mutating tools echo the saved name +
any rename. Docs steer authors to `read_only` unless the workflow
genuinely needs the model to mutate the stash, and gate
`delete_file('*')` behind the `full` bundle with a prominent
warning.

**`AgentOutput.files` → `list[str]`.** No longer ProxyFiles — a
list of names (`{filename}` / `persist/{filename}`) the producing
agent wants auto-fed to an LLM parent as tool-result content. The
parent's tool loop threads each as a name `file_ref` into its next
`LlmRequest` (§8.1) — the **router** resolves and inlines; the
parent never fetches bytes. This replaces the `feed_llm` /
`tool_response_from_result` mechanism with name references.

## 9. Security

  * **No outbound router fetches.** Dropping URL ingestion deletes
    the `url_guard` SSRF surface for files. Stores are
    content-bound upload-with-grant (a leaked grant can't be
    repurposed — it's pinned to one sha256 + size + user); fetches
    are short-TTL signed URLs.
  * **Per-user isolation via derived identity.** The bucket /
    directory rows are keyed by the authoritative `(user_id,
    session_id)` the router DERIVES from the task row (the
    `FileUploadRequestFrame` / `complete_task` pattern), never an
    agent-asserted field. This matters MORE than it did for
    `ProxyFile`: a named file has no per-file signed `key`, so the
    `(user_id, scope, filename)` tuple is the *sole* authority —
    if the router trusted an asserted `user_id`/`session_id`, an
    agent could read another user's files by name. Every file op
    (store / fetch / manage AND name-`file_ref` resolution in an
    `LlmRequest`) derives identity from `task_id` + verifies the
    agent is the task's active executor. Cross-user reference is
    impossible by construction.
  * **Shared-session reach is intentional.** Any agent acting in
    user U's session S reaches S's stash by name — that's the
    feature. A delegate (same user+session) shares it with no
    re-keying.
  * **Filename hygiene.** Reject control chars / quotes (existing
    `_FILENAME_REJECT`), reject `/` in bare names (flat namespace),
    reject the reserved `persist/` collision (§3).
  * **LLM-tool blast radius.** Mutating file tools are opt-in
    (`full` bundle); `delete_file` globs are the sharpest edge —
    documented, and a candidate for a confirm-gate follow-up.

## 10. Migration — hard cutover (pre-release)

One PR sequence, no compat shim:

  1. Land the store (table, frames, S3 directory layer, quota,
     session-close GC).
  2. Reshape `ctx.files` to the name-based `FileStash`; delete
     `ProxyFileManager`.
  3. Delete `ProxyFile`, `NewTaskFrame.attachments`,
     `ResultFrame.attachments`, `bp_router/attachments.py`
     (`resolve_proxyfiles` + the http/localfile ingestion),
     `feed_llm`, `tool_response_from_result`. NOTE:
     `resolve_request_file_refs` is **repurposed** (§11 phase 5) to
     resolve name `file_ref`s, NOT deleted — only its
     ProxyFile/http/localfile resolution goes; `file_part(pf)`
     becomes `ctx.files.llm_ref(name)`.
  4. Update every example + the LLM file-feed path to names.
  5. Update docs (`services.md`, `protocol.md`, `storage.md`,
     `security.md`).

No `ProxyFile` alias, no dual-read. The wire `attachments` field
is removed, not deprecated.

## 11. Implementation sequence

1. **Schema + storage**: `files` directory table
   `(user_id, scope, filename) UNIQUE`, blob refcount sweep,
   quota counter. Migration.
2. **Frames**: `FileStoreFrame` / `FileFetchFrame` /
   `FileManageFrame` + the typed `command` union; router handlers
   (dedup + atomic name allocation + quota + audit).
3. **Session GC**: `close_session` deletes session-scope rows.
4. **SDK**: `FileStash` (`store`/`write`/`read`/`list`/`delete`/
   `copy`/`llm_ref`); delete `ProxyFileManager`.
5. **Router-side name `file_ref` resolution** (§8.1): adapt
   `resolve_request_file_refs` to resolve `{"file_ref": {"name":
   …}}` against the named store, scoped to the `(user_id,
   session_id)` DERIVED from `LlmRequestFrame.task_id` (the
   `FileUploadRequestFrame` pattern — ignore asserted identity,
   verify active-executor), reusing the existing per-file inline
   cap + over-cap error. Reject name refs with no `task_id`
   (`file_ref_requires_task`). This is the path that keeps file
   bytes off the agent→router frame — land it BEFORE the LLM-feed
   surfaces.
6. **LLM tools**: `file_tools(bundle=…)`; `read_file` emits a
   name `file_ref` tool result (not inline bytes).
7. **`AgentOutput.files: list[str]`** + parent-side name→`file_ref`
   threading into the next `LlmRequest` (router resolves).
8. **Cutover deletion** (§10 step 3) + examples + docs.

Each of 1–7 is independently testable; 8 is the breaking flip.
Phase 5 is a prerequisite for 6 and 7 (both depend on the router
resolving name refs).

## 12. What not to do

  * **Don't keep `ProxyFile` as a transitional type.** Pre-release;
    a hard cutover avoids a dual file model.
  * **Don't reintroduce router-side URL fetching** "for
    convenience" — it's the SSRF surface we're deleting. Agents
    fetch-then-upload.
  * **Don't allow nested paths in v1.** Flat names per scope keeps
    the reserved-prefix rule and the dedup-counter simple. Nested
    namespaces are a later RFC.
  * **Don't auto-grant mutating LLM tools.** `read_only` is the
    default bundle; mutations are opt-in.
  * **Don't assume the requested filename equals the saved one.**
    The §5 invariant — always thread the returned name.

## 13. Open questions

  * **Out-of-task identity derivation.** In-task ops derive
    `(user_id, session_id)` from the task row (§8.1 / §9). Out-of-task
    ops have no task to derive from — the only authoritative
    identity is the socket's authenticated agent principal. Sketch:
    derive `user_id` from the agent's owning user; `persist/` ops
    work directly; session-scoped ops take an explicit `session_id`
    the router VALIDATES is owned by that user (a softer guarantee
    than in-task derive-everything). Pin the exact rule — and
    whether out-of-task session ops are even allowed in v1 — at
    implementation. (Transport stays WS frames for SDK uniformity.)
  * **LLM path trusts `frame.user_id` for tier / quota / audit.**
    Adjacent finding surfaced during this analysis: the current
    `_run_llm_call` uses `frame.user_id` directly (dispatch.py),
    safe today because ProxyFile resolution carries a signed key.
    Once name-`file_ref` resolution derives identity from the task
    (§8.1), the tier/quota/audit uses of `frame.user_id` are
    inconsistent with it (one derives, the others trust). Worth a
    separate hardening pass to derive the LLM call's `user_id` from
    the task too; out of scope for this design but flagged so it
    isn't lost.
  * **Separate session vs. persist quota ceilings** — one combined
    ceiling in v1; split if persist abuse becomes a concern.
  * **`read_file` size guard** — a large file inlined as tool
    content blows the context / payload cap. Reuse the existing
    `max_payload_bytes` guard and reject (or truncate-with-notice)
    oversize reads; pin the exact behaviour at implementation.
  * **Blob GC cadence** — inline refcount-delete vs. periodic
    sweep. Lean sweep (consistent with the audit/registration
    sweeps already in the codebase).
  * **MCP-bridged tool results** that carry images — route them
    into the producing agent's stash with auto-feed names? Natural
    once this lands; out of scope here.
