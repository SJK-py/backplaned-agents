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

from typing import TYPE_CHECKING, Any

from bp_agents.common.progress import emit_loop_progress
from bp_agents.common.tools import LocalToolset, peer_tool_specs
from bp_sdk import (
    LlmCallError,
    Message,
    UpstreamError,
    dispatch_file_tool,
    is_file_tool,
)
from bp_sdk import file_tools as sdk_file_tools

if TYPE_CHECKING:
    from bp_sdk import LlmResponse, TaskContext, ToolCall, ToolSpec


async def _dispatch_tool_call(
    ctx: TaskContext,
    tool_call: ToolCall,
    local_tools: LocalToolset | None,
    *,
    file_tools_enabled: bool = False,
) -> Message:
    """Route one model tool call to a local tool, a file-store tool, or a
    peer agent, and return the tool-response `Message`. A peer-call failure
    is fed back as the result so the model can recover instead of the turn
    dying. A `read_file` call returns a name `file_ref` part that the router
    resolves into multimodal content on the next `generate` ([sessions.md]
    §2)."""
    if local_tools is not None and local_tools.has(tool_call.name):
        return await local_tools.dispatch(ctx, tool_call)
    if file_tools_enabled and is_file_tool(tool_call.name):
        return await dispatch_file_tool(ctx.files, tool_call)
    if tool_call.name.startswith("call_"):
        try:
            child = await ctx.peers.spawn_from_tool_call(tool_call)
        except Exception as exc:  # noqa: BLE001
            return Message.tool_response(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                response=f"tool error: {exc}",
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


def _message_text(msg: Message) -> str | None:
    """The plain-text body of a tool-response message, or `None` when it's
    multimodal/structured (e.g. a `read_file` `file_ref` part)."""
    return msg.content if isinstance(msg.content, str) else None


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
    extra_tools: list[ToolSpec] | None = None,
    terminal_tools: set[str] | None = None,
    file_tools: str | None = None,
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
    """
    peer_specs = peer_tool_specs(ctx) if use_peer_tools else []
    local_specs = local_tools.specs() if local_tools is not None else []
    file_specs = sdk_file_tools(file_tools) if file_tools else []
    tools = peer_specs + local_specs + file_specs + (extra_tools or [])
    terminal = terminal_tools or set()

    resp: LlmResponse | None = None
    for round_idx in range(max_rounds):
        if emit_progress:
            await emit_loop_progress(ctx, kind="thinking", round=round_idx + 1)
        try:
            resp = await ctx.llm.generate(
                messages,
                preset=preset,
                tools=tools or None,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except LlmCallError as exc:
            raise UpstreamError(f"LLM call failed: {exc}") from exc

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
        if any(tc.name in terminal for tc in resp.tool_calls):
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
                ctx, tc, local_tools, file_tools_enabled=bool(file_tools)
            )
            messages.append(result_msg)
            if emit_progress:
                await emit_loop_progress(
                    ctx, kind="tool_result", round=round_idx + 1, tool=tc.name,
                    detail=_detail_tail(_message_text(result_msg), detail_chars),
                )

    # Exhausted max_rounds with tool calls still pending — return the
    # last response so the caller surfaces partial progress rather than
    # hanging. `resp` is non-None (max_rounds >= 1).
    assert resp is not None
    return resp
