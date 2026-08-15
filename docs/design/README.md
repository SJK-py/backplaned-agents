# Design docs

Each file here is a design record for one change: the problem, the shape
chosen, what was deliberately not done, and (where it has landed) the
deviations the implementation forced. They are kept after implementation
rather than deleted — the "what not to do" sections are the part that stops
a later change from quietly re-opening a settled question.

**Status is stated at the top of each doc**, and this index is the map. When
a doc's status changes, update it in both places.

## Platform — landed

| doc | what it covers |
| --- | --- |
| [`router-managed-file-store.md`](./router-managed-file-store.md) | Named per-user/per-session file stash; `File*` frames, `ctx.files`, LLM `file_ref` resolution. Replaced `ProxyFile`. |
| [`router-managed-session-store.md`](./router-managed-session-store.md) | Conversation log + session state + hand-over queue + FIFO turn lease. `SessionOp` frames, `ctx.history`. Both sides landed; the suite's `session_history`/`session_info` are gone (§13.2). Three `[shipped]` deviations in §13.3. Two known warts (§5.2, §9.4), one shipped-then-fixed bug (§6.4). |
| [`router-resolved-preset-slots.md`](./router-resolved-preset-slots.md) | The tier gate and the user's model choice resolved as one decision from an opaque slot key. Both halves landed; three `[shipped]` deviations (§5.1, §8.1, §8.2). |
| [`deployment-agent-host.md`](./deployment-agent-host.md) | One process per agent *group*, a roster token instead of twelve invitations, one `init`. 22 compose services → 11. |
| [`agent-tool-history-recall.md`](./agent-tool-history-recall.md) | `recall_tool_history` — an agent re-reading its own earlier tool results on demand. |
| [`oidc-webapp.md`](./oidc-webapp.md) | SSO against an external OpenID Provider; router as relying party, webapp as BFF. |
| [`mcp-bridge-per-server-mode-per-tool.md`](./mcp-bridge-per-server-mode-per-tool.md) | One backplane agent per MCP server, one mode per tool. |
| [`mcp-bridge-custom-llm-agents.md`](./mcp-bridge-custom-llm-agents.md) | Operator-authored LLM agents stood up by the bridge. |
| [`multimodal-vision-sidecar.md`](./multimodal-vision-sidecar.md) | Phase 1 landed — a text-only chat preset reading images/PDFs via a separate vision preset. |
| [`quota-enforcement.md`](./quota-enforcement.md) | Phases 1–2 landed; phase 3 (concurrent-task caps) outstanding. |
| [`kakao-channel.md`](./kakao-channel.md) | KakaoTalk as an egress-only pull channel behind a relay + queue. |

## Platform — proposed

| doc | what it covers |
| --- | --- |
| [`bridge-python-code-agents.md`](./bridge-python-code-agents.md) | A third bridge-provisioned agent kind: an operator-authored Python function run in a uid-dropped subprocess. v1 is pure-function + network egress, no backplane handles. Also records seven issues found in the custom-LLM-agent implementation (§14). |

## Deferred / superseded

| doc | why |
| --- | --- |
| [`s3-multipart-upload.md`](./s3-multipart-upload.md) | Deferred future work; no code proposed until a real size ceiling bites. |
| [`admin-session-cookie-encryption.md`](./admin-session-cookie-encryption.md) | Deferred future work. |
| [`llm-retriable-errors.md`](./llm-retriable-errors.md) | Draft for review. |
| [`llm-proxyfile-attachments.md`](./llm-proxyfile-attachments.md) | **Superseded** by the named file store. |
| [`llm-tool-response-from-result.md`](./llm-tool-response-from-result.md) | **Superseded** by the named file store §8.1. Kept as a record. |

## Reading order for the current line of work

The session store, preset slots, and deployment docs form one arc — moving
per-user and per-session state out of the suite's own Postgres and into the
router, then collapsing what the suite still needs to deploy:

1. `router-managed-session-store.md` §13 — how a suite maps onto the store
   (also the target shape for the suite rebuild).
2. `router-resolved-preset-slots.md` — the part of `user_config` the router
   must *interpret* rather than merely store. §5.1 is the one that decides
   where a user-facing setting can live at all.
3. `deployment-agent-host.md` — what is left to run once the suite's
   database dependency is mostly gone.

## Conventions

  * **Status line first**, in a blockquote, before the prose.
  * **`[shipped]` markers** flag where the implementation knowingly departed
    from the text — cheaper and more honest than rewriting the design to
    match the code after the fact.
  * **A "what not to do" section** in anything substantial. It is the part
    future readers need most and the part a diff cannot express.
  * **Platform changes are also logged** in
    [`../backplaned/changelog.md`](../backplaned/changelog.md), which tracks
    the suite's footprint on the vendored platform packages. A design doc says
    *why*; the changelog says *what changed, when*.
