"""bp_agents.common.loop — the shared multi-turn tool-calling loop.

Generalises the canonical agent loop (cf. `examples/test_drive/
gemini_agent.py`): LLM generate → round-trip the assistant turn (carries
reasoning blocks / thought-signatures so multi-turn tool use doesn't 400)
→ dispatch each tool call (peer agent via `spawn_from_tool_call`, or a
local tool) → thread the result back → repeat until the model stops
calling tools or `max_rounds` is hit.

Shared by the orchestrator (l0) and the l1 specialists. Per-agent
behaviour (system prompt, which local tools, whether peer tools are
exposed) is passed in; the loop itself is generic.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import TYPE_CHECKING, Any

from bp_agents.common.progress import emit_loop_progress, relay_subagent_progress
from bp_agents.common.tools import LocalToolset, peer_tool_specs
from bp_protocol.types import TaskStatus
from bp_sdk import (
    CancellationError,
    FileStoreError,
    LlmCallError,
    Message,
    UpstreamError,
    dispatch_file_tool,
    is_file_tool,
)
from bp_sdk import file_tools as sdk_file_tools

if TYPE_CHECKING:
    from bp_protocol.frames import ResultFrame
    from bp_sdk import LlmResponse, TaskContext, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


def _failed_tool_text(tool_name: str, child: ResultFrame) -> str:
    """Render a non-succeeded subagent result into a tool-response string the
    MODEL can act on. The error carries a `{code, message}`; both are
    agent/router-authored and safe to relay (raw exception strings are already
    scrubbed to `internal_error` upstream, so nothing host-internal leaks).
    Without this the model gets an empty result and fails silently."""
    err = child.error or {}
    code = str(err.get("code") or child.status.value)
    message = str(err.get("message") or "").strip()
    detail = f"{code}: {message}" if message and message != code else code
    return f"The {tool_name} call did not succeed ({detail})."


# The vision sidecar's system prompt — faithfulness first. A confidently
# hallucinating proxy is worse than a blocked read, so it must transcribe
# verbatim, preserve structure, report ABSENCE of asked-for content, and
# flag uncertainty instead of guessing
# ([../docs/design/multimodal-vision-sidecar.md] §3.2).
_VISION_SYSTEM = (
    "You are a vision model reading a file on behalf of a text-only "
    "assistant that cannot see it. Transcribe and describe the file "
    "FAITHFULLY: copy all text verbatim (numbers, URLs, labels, code "
    "exactly as written), preserve structure (render tables as Markdown), "
    "and describe relevant visual layout. If the information the goal asks "
    "for is NOT present in the file, say so explicitly. Flag anything "
    "illegible or uncertain — never guess or invent content. Your reply is "
    "the assistant's only window onto this file."
)


def _is_visual(mime: str | None, name: str) -> bool:
    """Whether a file needs a multimodal model (image or PDF). Authoritative
    when `mime` is known — the stash `stat`'s `mime_type` — and falls back to
    the name's extension for an older blob stored without a mime, so an
    extension-less but correctly-typed image is still caught."""
    m = mime or mimetypes.guess_type(name)[0] or ""
    return m.startswith("image/") or m == "application/pdf"


def _last_user_text(messages: list[Message], *, limit: int = 1000) -> str:
    """The most recent `user` turn's text, trimmed — ambient context handed
    to the vision model so it knows the task even when `purpose` is thin."""
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        c = msg.content
        if isinstance(c, str):
            return c[-limit:]
        if isinstance(c, list):
            text = "\n".join(
                p["text"] for p in c
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
            return text[-limit:]
    return ""


async def _vision_read_file(
    ctx: TaskContext,
    tool_call: ToolCall,
    *,
    preset: str,
    vision_context: str,
) -> Message | None:
    """Read an image/PDF `read_file` call through the separate vision
    `preset` and return its TEXT as the tool result, so a text-only main
    model never sees raw bytes ([multimodal-vision-sidecar.md] §3.3).

    The image/PDF gate is AUTHORITATIVE: it `stat`s the file for its real
    `mime_type` (extension fallback for an older mime-less blob), so an
    extension-less or mislabelled image is still routed correctly.

    Returns `None` when the file isn't image/PDF, or when `stat` reports it
    unbound — the caller then falls through to the normal `read_file` path
    (a text file resolves to a text part the main model reads directly; a
    missing name surfaces the usual error). The vision call is a
    SELF-CONTAINED one-shot generate (its own messages, no tool history),
    so it never entangles the main loop's reasoning-block / tool_call_id
    round-trip across providers."""
    args = tool_call.args or {}
    name = str(args.get("name") or "").strip()
    if not name:
        return Message.tool_response(
            tool_call_id=tool_call.id, name=tool_call.name,
            response={"error": "read_file requires a 'name'"},
        )
    # Authoritative type from the stash; on a glitch, fall back to extension.
    mime: str | None = None
    try:
        mime = (await ctx.files.stat(name)).mime_type
    except (asyncio.CancelledError, CancellationError):
        raise
    except FileStoreError:
        return None  # not_found / invalid_filename → normal dispatch surfaces it
    except Exception:  # noqa: BLE001 — stat glitch: degrade to the extension gate
        logger.debug("vision stat failed; using extension gate", exc_info=True)
    if not _is_visual(mime, name):
        return None  # not image/PDF — let the normal file dispatch handle it
    purpose = str(args.get("purpose") or "").strip()
    goal = purpose or "Read this file and report all of its content."
    ctx_block = (
        f"\n\nTask context (for relevance):\n{vision_context}"
        if vision_context else ""
    )
    user_parts: list[dict[str, Any]] = [
        {"text": f"Goal: {goal}{ctx_block}"},
        ctx.files.llm_ref(name),
    ]
    try:
        resp = await ctx.llm.generate(
            [
                Message(role="system", content=_VISION_SYSTEM),
                Message(role="user", content=user_parts),
            ],
            preset=preset,
        )
    except (asyncio.CancelledError, CancellationError):
        raise
    except Exception as exc:  # noqa: BLE001 — surface as a result the model can act on
        logger.warning(
            "vision_read_failed",
            extra={"event": "vision_read_failed", "preset": preset,
                   "error": type(exc).__name__},
        )
        return Message.tool_response(
            tool_call_id=tool_call.id, name=tool_call.name,
            response=(
                f"[Could not read '{name}' with the vision model: {exc}. "
                "Tell the user the file couldn't be read, or try again.]"
            ),
        )
    text = (resp.text or "").strip() or "(the vision model returned no text)"
    header = f"[Contents of '{name}', read by the vision model" + (
        f" for: {purpose}]" if purpose else "]"
    )
    return Message.tool_response(
        tool_call_id=tool_call.id, name=tool_call.name,
        response=f"{header}\n{text}",
    )


async def _dispatch_tool_call(
    ctx: TaskContext,
    tool_call: ToolCall,
    local_tools: LocalToolset | None,
    *,
    file_tools_enabled: bool = False,
    forward_subagent_progress: bool = True,
    multimodal_preset: str | None = None,
    vision_context: str = "",
) -> Message:
    """Route one model tool call to a local tool, a file-store tool, or a
    peer agent, and return the tool-response `Message`. A peer-call failure
    is fed back as the result so the model can recover instead of the turn
    dying. A `read_file` call returns a name `file_ref` part that the router
    resolves into multimodal content on the next `generate` ([sessions.md]
    §2).

    When `forward_subagent_progress`, a peer (subagent) call is **streamed**
    and its action progress is re-emitted on this agent's task, so a verbose
    user sees the specialist's steps (e.g. `[Research Agent] [Tool]
    web_search`) bubbling up — not just the umbrella call.

    EVERY dispatch path runs under ONE error boundary: any failure — a peer
    spawn that raised, a file-store glitch that isn't a clean `FileStoreError`,
    an unexpected bug in a local tool — is fed back to the model as the tool
    result so the loop CONTINUES and the model can recover (retry, route
    around it, or tell the user what broke) instead of the exception unwinding
    the whole turn into a no-response failure. Genuine cancellation (task
    abort / shutdown) is re-raised, never swallowed."""
    try:
        if local_tools is not None and local_tools.has(tool_call.name):
            return await local_tools.dispatch(ctx, tool_call)
        if file_tools_enabled and is_file_tool(tool_call.name):
            # Vision sidecar: a text-only model's read_file on an image/PDF is
            # transcribed to text by a separate vision preset instead of being
            # fed as raw bytes ([multimodal-vision-sidecar.md]). Non-visual
            # files (and an unset sidecar) fall through to the normal path.
            if multimodal_preset and tool_call.name == "read_file":
                vision_msg = await _vision_read_file(
                    ctx, tool_call, preset=multimodal_preset,
                    vision_context=vision_context,
                )
                if vision_msg is not None:
                    return vision_msg
            return await dispatch_file_tool(ctx.files, tool_call)
        if tool_call.name.startswith("call_"):
            if forward_subagent_progress:
                async with (
                    await ctx.peers.spawn_from_tool_call(tool_call, stream=True)
                ) as stream:
                    async for child_pf in stream:
                        await relay_subagent_progress(ctx, child_pf)
                    child = await stream.result()
            else:
                child = await ctx.peers.spawn_from_tool_call(tool_call)
            # A FAILED/CANCELLED child has output=None and its reason in
            # `child.error` — which tool_response_from_result drops, so the
            # model would otherwise get an EMPTY tool result for a failed
            # delegation and couldn't tell the user or retry. Surface the error
            # code/message so the model can react (e.g. a file-name typo →
            # not_found → ask/recheck).
            if child.status is not TaskStatus.SUCCEEDED:
                return Message.tool_response(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    response=_failed_tool_text(tool_call.name, child),
                )
            return Message.tool_response_from_result(
                tool_call_id=tool_call.id, name=tool_call.name, result=child
            )
        # Neither a known local tool nor a peer-agent tool name.
        return Message.tool_response(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            response=f"unknown tool: {tool_call.name}",
        )
    except (asyncio.CancelledError, CancellationError):
        # Cooperative cancel / shutdown — end the turn; do NOT feed to the
        # model and re-issue, that would defeat the abort.
        raise
    except Exception as exc:  # noqa: BLE001 — any tool failure becomes a result
        # The detail (type + message) goes to ops via the log; the model sees a
        # bounded line it can act on. Keeps a single bad tool call from killing
        # the turn — the loop runs another round and the model still answers.
        logger.warning(
            "tool_dispatch_error",
            extra={
                "event": "tool_dispatch_error",
                "tool": tool_call.name,
                "error": type(exc).__name__,
            },
        )
        return Message.tool_response(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            response=f"The {tool_call.name} call failed: {exc}",
        )


def _detail_tail(text: str | None, limit: int) -> str | None:
    """The verbose-progress `detail`: the last non-empty paragraph of
    `text`, trimmed to its last `limit` characters (prefixed with `…` when
    truncated). `None` when there's nothing to show or detail is disabled."""
    if not text or limit <= 0:
        return None
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    tail = blocks[-1] if blocks else text.strip()
    if not tail:
        return None
    return f"…{tail[-limit:]}" if len(tail) > limit else tail


# Injected when the loop hits `max_rounds` mid-tool-use, to force a final
# text answer instead of returning an empty tool-call turn.
_FINAL_ANSWER_NUDGE = (
    "You've reached the tool-use limit for this turn. Do not call any more "
    "tools. Give your best final answer now using the information already "
    "gathered; if it's incomplete, say what you found and what's still open."
)


def _message_text(msg: Message) -> str | None:
    """The plain-text body of a tool-response message, or `None` when it's
    multimodal/structured (e.g. a `read_file` `file_ref` part)."""
    return msg.content if isinstance(msg.content, str) else None


# How many times one `generate` call may strip rejected attachments and retry
# before giving up and surfacing the error. Small: one bad attachment type is
# the common case; a loop that keeps failing on stripped input is broken.
_MAX_ATTACHMENT_RECOVERIES = 2


def _strip_unfeedable_file_refs(messages: list[Message]) -> int:
    """Replace every `file_ref` part in `messages` with a text note the model
    can act on, returning the count replaced (0 → nothing to recover).

    The recovery path for a provider that REJECTS an attached file the model
    can't ingest — e.g. `read_file` on a PDF against an Anthropic-compatible
    backend without document support. The router resolves a `file_ref` into a
    base64 `document`/`image` part on the NEXT `generate`; if the backend 400s
    on it, the whole turn died with no reply. Swapping the ref for a
    `[couldn't be shown — convert it first]` note lets the model route around
    it (convert to text/Markdown and read that) instead."""
    replaced = 0
    for msg in messages:
        content = msg.content
        if not isinstance(content, list):
            continue
        for i, part in enumerate(content):
            if isinstance(part, dict) and isinstance(part.get("file_ref"), dict):
                name = part["file_ref"].get("name") or "file"
                content[i] = {
                    "text": (
                        f"[The file {name!r} could not be shown to the model — "
                        "this backend can't read that file type directly. "
                        "Convert it to text or Markdown first (e.g. with a "
                        "document-conversion tool) and read the result.]"
                    )
                }
                replaced += 1
    return replaced


async def _generate_resilient(
    ctx: TaskContext,
    messages: list[Message],
    *,
    preset: str | None,
    tools: list[ToolSpec] | None,
    tool_choice: Any | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LlmResponse:
    """`ctx.llm.generate`, but recover from a provider rejecting an attached
    file it can't ingest. On a NON-retriable `LlmCallError` (a content/4xx
    problem, not a transient blip — the SDK already exhausts its retry budget
    for retriable ones) when the messages carry `file_ref`-derived
    attachments, strip those attachments, leave the model a note, and retry —
    so an unfeedable file becomes a recoverable result the model can work
    around, not a dead turn. Anything else maps to `UpstreamError` as before."""
    attempts = 0
    while True:
        try:
            return await ctx.llm.generate(
                messages,
                preset=preset,
                tools=tools,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except LlmCallError as exc:
            if exc.retriable or attempts >= _MAX_ATTACHMENT_RECOVERIES:
                raise UpstreamError(f"LLM call failed: {exc}") from exc
            if not _strip_unfeedable_file_refs(messages):
                raise UpstreamError(f"LLM call failed: {exc}") from exc
            attempts += 1
            logger.warning(
                "llm_input_rejected_recovered",
                extra={
                    "event": "llm_input_rejected_recovered",
                    "error": exc.code,
                    "attempt": attempts,
                },
            )


async def run_llm_loop(
    ctx: TaskContext,
    *,
    messages: list[Message],
    preset: str | None = None,
    local_tools: LocalToolset | None = None,
    use_peer_tools: bool = True,
    tool_choice: Any | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_rounds: int = 8,
    emit_progress: bool = True,
    forward_subagent_progress: bool = True,
    extra_tools: list[ToolSpec] | None = None,
    terminal_tools: set[str] | None = None,
    file_tools: str | None = None,
    multimodal_preset: str | None = None,
    detail_chars: int = 100,
) -> LlmResponse:
    """Run the tool-calling loop until the model returns no tool calls
    (or `max_rounds` is reached). **Mutates `messages` in place** —
    appends each assistant turn (via `assistant_from_response`, so
    reasoning round-trips) and each tool-response row. Returns the final
    `LlmResponse`; the caller persists `resp.text` as the assistant turn
    and reads `resp.usage` for accounting.

    Tools offered to the model = the ACL-filtered peer catalog (when
    `use_peer_tools`) + `local_tools` + `extra_tools` (advertised-only
    specs the loop never dispatches). A downstream `LlmCallError` is
    mapped to `UpstreamError` (status 502) at the boundary.

    `terminal_tools` names tools that **end** the loop instead of being
    dispatched: when the model calls one, the assistant turn is appended
    and the response is returned immediately so the caller can act on the
    call (delegation hand-off `hand_off`, delegate hand-back
    `end_delegation`). The terminal tool's spec must be supplied via
    `extra_tools` (or `local_tools`) for the model to see it.

    `file_tools` (a `file_tools` bundle name — `"read_only"` or `"full"`)
    exposes the SDK file-store tools so the model can list / read / write
    stash files. `read_file` shows a file to the model multimodally (the
    bytes attach on the next turn, router-resolved). Only file-capable
    agents (those with `ctx.files`) should pass it.

    `multimodal_preset` (when set) turns on the **vision sidecar** for
    `read_file` ([../docs/design/multimodal-vision-sidecar.md]): the agent
    passes its configured vision preset here only when the turn's own
    preset is text-only, so `read_file` advertises an optional `purpose`
    arg and routes image/PDF reads through `multimodal_preset` (returning
    text) instead of feeding raw bytes the text-only model can't ingest.
    """
    peer_specs = peer_tool_specs(ctx) if use_peer_tools else []
    local_specs = local_tools.specs() if local_tools is not None else []
    proxy_on = bool(multimodal_preset) and bool(file_tools)
    file_specs = (
        sdk_file_tools(file_tools, read_file_intent=proxy_on) if file_tools else []
    )
    tools = peer_specs + local_specs + file_specs + (extra_tools or [])
    terminal = terminal_tools or set()
    # Ambient context for the vision sidecar — computed once; the current
    # user turn so the vision model knows the task even with a thin `purpose`.
    vision_context = _last_user_text(messages) if proxy_on else ""

    resp: LlmResponse | None = None
    for round_idx in range(max_rounds):
        if emit_progress:
            await emit_loop_progress(ctx, kind="thinking", round=round_idx + 1)
        resp = await _generate_resilient(
            ctx, messages,
            preset=preset,
            tools=tools or None,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Round-trip the assistant turn verbatim (reasoning blocks +
        # thought-signatures) before dispatching tools.
        messages.append(Message.assistant_from_response(resp))
        # Surface the model's reasoning (when the provider exposes a
        # thought summary) as a detailed `thinking` line.
        if emit_progress and resp.thought_summary:
            await emit_loop_progress(
                ctx, kind="thinking", round=round_idx + 1,
                detail=_detail_tail(resp.thought_summary, detail_chars),
            )
        if not resp.tool_calls:
            return resp

        # A terminal tool ends the loop — the caller inspects
        # `resp.tool_calls` and acts (e.g. delegation hand-off/back).
        # Surface it as progress first: otherwise the hand-off / hand-back
        # transitions are invisible in verbose mode, unlike the ordinary
        # dispatched tool calls below. The loop never dispatches a terminal
        # tool, so there's no matching `tool_result` frame.
        terminal_calls = [tc for tc in resp.tool_calls if tc.name in terminal]
        if terminal_calls:
            if emit_progress:
                accompanying = _detail_tail(resp.text, detail_chars)
                for tc in terminal_calls:
                    await emit_loop_progress(
                        ctx, kind="tool_call", round=round_idx + 1,
                        tool=tc.name, detail=accompanying,
                    )
            return resp

        # The assistant's spoken message accompanying the tool call(s).
        accompanying = _detail_tail(resp.text, detail_chars)
        for tc in resp.tool_calls:
            if emit_progress:
                await emit_loop_progress(
                    ctx, kind="tool_call", round=round_idx + 1, tool=tc.name,
                    detail=accompanying,
                )
            result_msg = await _dispatch_tool_call(
                ctx, tc, local_tools, file_tools_enabled=bool(file_tools),
                forward_subagent_progress=forward_subagent_progress,
                multimodal_preset=multimodal_preset if proxy_on else None,
                vision_context=vision_context,
            )
            messages.append(result_msg)
            if emit_progress:
                await emit_loop_progress(
                    ctx, kind="tool_result", round=round_idx + 1, tool=tc.name,
                    detail=_detail_tail(_message_text(result_msg), detail_chars),
                )

    # Exhausted max_rounds with tool calls still pending. The model has the
    # latest tool results in `messages` but hasn't synthesised them, and the
    # last `resp` is a tool-call turn (often empty text) — returning it gives
    # the user a blank reply. Force ONE final answer with tools disabled so
    # the model must produce text from what it gathered.
    assert resp is not None
    if not resp.tool_calls:
        return resp
    if emit_progress:
        await emit_loop_progress(
            ctx, kind="thinking", round=max_rounds, detail="wrapping up",
        )
    messages.append(Message(role="user", content=_FINAL_ANSWER_NUDGE))
    final = await _generate_resilient(
        ctx, messages, preset=preset, tools=None,
        temperature=temperature, max_tokens=max_tokens,
    )
    messages.append(Message.assistant_from_response(final))
    return final
