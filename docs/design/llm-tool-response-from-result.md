> **Superseded.** The per-file `feed_llm`/`ProxyFile` mechanism
> described here was replaced by the router-managed named file store:
> a child's `AgentOutput.files` are file-store NAMES, threaded as
> `{"file_ref": {"name": …}}` parts by
> `Message.tool_response_from_result`. See
> `router-managed-file-store.md` §8.1. Kept as a design record.

# Result files auto-fed as LLM tool-response inputs

Let a child agent declare per-file whether each `ProxyFile` it
returns should be auto-passed back to an LLM parent as
tool-response multimodal content, or kept out of the LLM's view.
The LLM-orchestrating parent picks the right behaviour
**without per-tool hand-crafting** — discovering a new tool means
the existing tool-call loop covers it.

## 1. The gap today

`AgentOutput.files` is the producer's only outbound file channel.
The SDK lifts it into `ResultFrame.attachments`
(`bp_protocol/types.py`); the router rewrites every ref to
`router-proxy` with a fresh keyed fetch URL
(`bp_protocol/frames.py`). The receiver sees one list —
`result.attachments` — and decides what to do with it.

For an LLM-orchestrating parent that runs a generic tool loop, that
single channel forces a hand-crafted **per-tool** decision: which
files from THIS tool's result should be inlined as multimodal
tool-result content vs. fetched-locally / forwarded-onward /
ignored? Pre-this-change, `examples/test_drive/gemini_agent.py`'s
loop just dropped `child.attachments` entirely, because there is
no general answer when the LLM might invoke any tool in the
catalog.

The blocker is structural, not stylistic:

  * **Auto-discovery is core**: an LLM agent's tool surface is
    `build_tools()` over the visible catalog. New agents that
    appear in the catalog (a registered MCP server, a freshly
    onboarded helper) become callable **without code change**.
  * **A receiver-side switch defeats auto-discovery**: "for tool
    X fetch, for tool Y inline" is per-tool knowledge the LLM
    parent can only have by being updated whenever the tool set
    grows. That's exactly what the auto-tool-build flow is meant
    to avoid.
  * **The right place to declare intent is the producer.** The
    agent that PRODUCED a file knows whether it's "the rendered
    chart I want the LLM to see" or "the gigabyte log dump the
    caller should download." Declaring that as part of each
    file's metadata lets every consumer follow the same rule
    without per-tool branching.

## 2. Goals / non-goals

**Goals**

  * **Producer designates** at emit time which files are inline-
    for-LLM vs. explicit-fetch.
  * **Consumer follows automatically**: an LLM parent's tool-call
    loop calls one SDK helper per tool_call and gets the right
    multimodal tool_response — no per-tool branching, no decision
    to make when a newly-discovered tool starts returning files.
  * Producer intent rides through the existing `router-proxy`
    rewrite path unchanged — **no router-side schema work** and
    **no new wire field on `ResultFrame`**.
  * **Backwards-compat for the existing path**: `output.files`
    keeps its current semantics; `result.attachments` keeps its
    current shape. Existing code paths
    (`ctx.files.fetch_all(result.attachments)`) keep working.

**Non-goals**

  * Per-tool LLM-visibility (`tool=False` per MCP tool) — orthogonal.
  * Changing `peers.spawn`'s return type or surface.

## 3. Design — single channel + per-file `feed_llm` flag

**Per-file producer intent** (`ProxyFile`):

```python
class ProxyFile(BaseModel):
    ...
    feed_llm: bool = True
    """When True (default), an LLM-orchestrating receiver that calls
    `Message.tool_response_from_result(...)` threads this file into
    the LLM as multimodal `file_part(pf)` content. Set False to
    exclude the file from the auto-LLM-feed path — useful for bulk
    output the producer wants the caller to fetch / forward /
    archive without presenting it to the model.

    Non-LLM consumers (`ctx.files.fetch_all(...)`, storage relays)
    ignore the flag and see every entry — single channel,
    producer-decided routing for the LLM-feed path."""
```

**Producer ergonomics** (`ProxyFileManager.put`):

```python
async def put(
    self,
    src: Path | bytes | AsyncIterable[bytes],
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    feed_llm: bool = True,
) -> ProxyFile:
    ...
```

The kwarg threads through to BOTH `ProxyFile` construction sites
inside `put` (the embedded `localfile` shortcut and the
external-agent upload-then-mint path).

**Consumer helper** (`bp_sdk/llm.py::Message`):

```python
@classmethod
def tool_response_from_result(
    cls,
    *,
    tool_call_id: str,
    name: str,
    result: ResultFrame,
) -> Message:
    """Build a tool-response message from a `peers.spawn(...)`
    result. Reads `result.attachments` and filters by
    `pf.feed_llm` — only entries with `feed_llm=True` are threaded
    through as `file_part(pf)` multimodal content. Files with
    `feed_llm=False` stay in `result.attachments` for explicit
    fetch via `ctx.files.fetch_all(...)`.
    """
```

**Router rewrite** (`bp_router/attachments.py::resolve_proxyfiles`):
the existing `_mint_ref` / `_ingest` helpers gain a keyword-only
`feed_llm: bool` parameter; all three rewrite paths
(`router-proxy`, `localfile`, `http`) pass `pf.feed_llm` so the
re-keyed `router-proxy` ref delivered to the caller carries the
producer's intent through unchanged.

That's the whole surface change.

## 4. Why per-file, not per-channel

Two alternative shapes were considered and rejected on the way to
this design:

  * **Receiver-side helper choice** (`tool_response_from_result`
    reads ALL `result.attachments` and the receiver picks fetch
    vs. feed per-tool). Rejected because it breaks auto-discovery:
    the LLM parent would need a per-tool-id lookup table to know
    whether a newly-discovered agent's files should be fetched
    or inlined.
  * **Separate `output.llm_files` field + `ResultFrame.llm_attachments`
    wire field** (dual channels carrying different intent).
    Considered concretely; the bytes-on-the-wire cost was real
    (every result with files paid the dual-resolve overhead) and
    the wire-shape change rippled into wire-compat upgrade ordering
    for caller agents. A per-file flag on the existing channel is
    strictly smaller — one wire-level field on `ProxyFile` instead
    of a duplicated list — and produces the same producer-decided
    routing.

The per-file flag is the smallest shape that closes the gap:

```python
# LLM orchestrator — same loop covers every tool, present or
# future, that follows the contract:
for tc in resp.tool_calls:
    child = await ctx.peers.spawn_from_tool_call(tc)
    messages.append(Message.tool_response_from_result(
        tool_call_id=tc.id, name=tc.name, result=child,
    ))
```

A producer that wants to split a single result into "for the
model" and "for the caller" buckets uses one list, two flag
values:

```python
chart_png = await ctx.files.put(png_bytes, filename="chart.png")
chart_csv = await ctx.files.put(csv_bytes, filename="chart.csv",
                                 feed_llm=False)
return AgentOutput(
    content="Rendered the requested chart from the dataset.",
    files=[chart_png, chart_csv],
)
```

A producer with no intent distinction calls `put(...)` without
the kwarg — `feed_llm=True` is the default, so the file flows to
the LLM via the helper.

## 5. Mode-level contracts (when consumer behaviour must vary)

If the LLM parent needs the same kind of file routed differently
per call (sometimes inline, sometimes fetch), the producer
exposes separate modes per the unified-mode model (#235):

  * `mode="render_inline"` — `put(...)` (default `feed_llm=True`)
    → the chart rides into the LLM context.
  * `mode="render_for_download"` — `put(..., feed_llm=False)` →
    the caller fetches the bytes for forwarding.

The mode IS the per-call contract negotiation; the LLM picks the
mode via tool selection, and the producer's per-mode result
honours the contract. The receiver's tool-call loop stays generic
— both modes flow through the same `tool_response_from_result`
helper.

## 6. Implementation impact

Landed in PR #253 (final v3 shape; the v2 attempt at separate
`llm_files` / `llm_attachments` fields was reverted in the same
branch before merge):

  * `bp_protocol/types.py` — `ProxyFile.feed_llm: bool = True`
    field added.
  * `bp_sdk/files.py` — `ProxyFileManager.put(..., feed_llm: bool
    = True)` plumbed through both ProxyFile construction sites.
  * `bp_router/attachments.py` — `_mint_ref` / `_ingest` accept
    keyword-only `feed_llm: bool`; all three rewrite paths
    propagate `pf.feed_llm`.
  * `bp_sdk/llm.py` — `Message.tool_response_from_result(...)`
    filters `result.attachments` by `pf.feed_llm`.
  * `examples/test_drive/gemini_agent.py` — spawn-result loop
    uses the helper.
  * `docs/sdk/services.md` — §2.1.2 + §1.3.1.1 cover the
    producer-side and helper-side surfaces.
  * `docs/router/protocol.md` — Result section documents the
    per-file flag semantics.

No new wire field on `ResultFrame`. No router rewrite duplication.
No changes to `bp_mcp_bridge` or storage paths.

## 7. Open questions (deferred)

  * **Mime-type filter at the helper.** Should the helper drop
    entries whose `mime_type` isn't accepted by the active
    provider? The router's provider adapter is the right
    rejection point and already does the work for inline
    `image_part` / `document_part`. Lean: punt.
  * **MCP bridge bridging.** MCP `tool_result` content can be
    text + image + resource. The bridge today surfaces everything
    via `output.content` plus the `[MCP tool error]` marker. A
    follow-up could route image-content into produced files with
    `feed_llm=True` so the bridged tool's images flow as inline
    LLM input automatically. Out of scope here.
