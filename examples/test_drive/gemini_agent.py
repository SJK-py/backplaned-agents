"""Test-drive: gemini agent — the full LLM-surface reference.

A realistic LLM agent (cf. `docs/sdk/services.md` §8), made
concrete:

  * Multi-turn tool-calling loop where the MODEL calls peer agents:
    `build_tools(ctx.peers.visible())` → `ToolSpec` → `generate(
    tools=)` → `Message.assistant_from_response` → `peers.
    spawn_from_tool_call` → `Message.tool_response` → repeat. The
    `assistant_from_response` helper round-trips Gemini 3
    thought-signatures automatically (drop them and the next call
    400s).
  * Streaming (`stream=True`) consumed via `StreamAccumulator` —
    the iterator yields raw `LlmDelta`s; the accumulator rebuilds a
    proper `LlmResponse` so the turn can still be round-tripped.
  * `RetryPolicy` — `generate` retries by default; passed
    explicitly here to make the contract visible.
  * Multimodal input — image files in the session stash become
    `ctx.files.llm_ref(name)` parts the ROUTER resolves into the
    provider call (out-of-band file → LLM vision, no local fetch).
  * File tools — `file_tools(bundle="read_only")` lets the MODEL
    browse + read session files; `dispatch_file_tool` runs the call
    against `ctx.files` (`read_file` returns a name file_ref the
    router resolves on the next turn).
  * Typed-error mapping — a downstream `LlmCallError` is re-raised
    as `UpstreamError` (status_code 502) at the boundary.
  * Token / cost accounting surfaced in `metadata` (incl. the
    thinking-budget split).

Pre-reqs:
  - `GEMINI_API_KEY` in the router env (the `default` preset's
    `api_key_ref` resolves `env://GEMINI_API_KEY`).
  - `google-genai>=1.14` in the router venv (`llm-gemini` extra).

Run AFTER the router is up (and ideally with `echo_agent.py`
connected so the model has a peer tool to call):

    AGENT_INVITATION_TOKEN=<token> \\
    AGENT_ROUTER_URL=ws://127.0.0.1:8000/v1/agent \\
    AGENT_STATE_DIR=/tmp/gemini-agent-state \\
        python examples/test_drive/gemini_agent.py
"""

from __future__ import annotations

from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import (
    Agent,
    LlmCallError,
    Message,
    RetryPolicy,
    StreamAccumulator,
    TaskContext,
    ToolSpec,
    UpstreamError,
    dispatch_file_tool,
    file_tools,
    is_file_tool,
)
from bp_sdk.tools import build_tools

_MAX_TOOL_ROUNDS = 4
_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}


agent = Agent(
    info=AgentInfo(
        agent_id="gemini_agent",
        description="Test-drive — Gemini relay with peer-tool calling.",
        groups=["test_drive"],
        capabilities=["text.generation"],
    ),
)


def _peer_tools(ctx: TaskContext) -> list[ToolSpec]:
    """Project the ACL-filtered catalog into neutral `ToolSpec`s.

    Reusing `build_tools` (rather than hand-rolling names) keeps the
    tool names in lockstep with `peers.spawn_from_tool_call` /
    `resolve_tool_name`, so a model-emitted call always round-trips
    to the right (agent, mode)."""
    fns = build_tools(ctx.peers.visible(), provider="openai")
    return [
        ToolSpec(
            name=f["function"]["name"],
            description=f["function"]["description"],
            parameters=f["function"]["parameters"],
        )
        for f in fns
    ]


async def _user_message(ctx: TaskContext, payload: LLMData) -> Message:
    """Build the user turn — multi-part (text + images) when image
    files are in the session stash, plain text otherwise.

    `ctx.files.llm_ref(name)` is a NAME reference the ROUTER resolves
    into the provider call (bytes never cross the agent→router frame),
    so there's no local fetch here — and a file over the WS cap is
    fed without tripping it. The router infers the modality from the
    named blob's mime type."""
    image_names = [
        n for n in await ctx.files.list()
        if n.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS
    ]
    if not image_names:
        return Message(role="user", content=payload.prompt)

    parts: list[dict] = [{"text": payload.prompt}]
    parts.extend(ctx.files.llm_ref(n) for n in image_names)
    ctx.progress.status(f"vision:{len(image_names)}-image(s)")
    return Message(role="user", content=parts)


@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    ctx.log.info("gemini.start", extra={"event": "gemini.start"})
    ctx.progress.status("thinking")

    messages: list[Message] = []
    if payload.agent_instruction:
        messages.append(Message(role="system", content=payload.agent_instruction))
    messages.append(await _user_message(ctx, payload))

    # Peer-agent tools + the read-only file-store bundle (the model can
    # browse + read the session stash; expose `bundle="full"` only if it
    # should mutate it).
    tools = _peer_tools(ctx) + file_tools(bundle="read_only")
    rounds = 0
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            rounds += 1
            # `max_tokens` left unset on purpose: on Gemini 2.5+ it's
            # the TOTAL budget shared with hidden thinking, so a small
            # cap truncates the visible answer. RetryPolicy() is the
            # default; passed explicitly so the contract is visible.
            resp = await ctx.llm.generate(
                messages,
                preset="default",
                tools=tools or None,
                retry=RetryPolicy(),
            )
            # Round-trip the assistant turn verbatim (carries the
            # Gemini-3 thought-signature the next call requires).
            messages.append(Message.assistant_from_response(resp))
            if not resp.tool_calls:
                break
            for tc in resp.tool_calls:
                ctx.progress.tool_call(tc.name, tc.args)
                if is_file_tool(tc.name):
                    # Local file-store op (list/read) executed against
                    # ctx.files — NOT a peer call. `read_file` returns a
                    # name file_ref the ROUTER resolves on the next turn.
                    messages.append(await dispatch_file_tool(ctx.files, tc))
                    continue
                try:
                    child = await ctx.peers.spawn_from_tool_call(tc)
                except Exception as exc:  # noqa: BLE001
                    # Feed the failure back as the tool result so the
                    # model can recover instead of the turn dying.
                    ctx.progress.tool_result(tc.name, f"tool error: {exc}")
                    messages.append(
                        Message.tool_response(
                            tool_call_id=tc.id,
                            name=tc.name,
                            response=f"tool error: {exc}",
                        )
                    )
                    continue
                ctx.progress.tool_result(
                    tc.name,
                    (child.output.content if child.output else "") or "",
                )
                # The helper threads the child's `output.files`
                # (file-store NAMES the child surfaced) as
                # `{"file_ref": {"name": …}}` parts the ROUTER
                # resolves into the provider call. A child that
                # returned only text gets the same wire output as the
                # bare-text path, so this is always-safe to use.
                messages.append(
                    Message.tool_response_from_result(
                        tool_call_id=tc.id,
                        name=tc.name,
                        result=child,
                    )
                )
    except LlmCallError as exc:
        # Map a downstream provider failure to a 502 at the boundary.
        raise UpstreamError(f"LLM call failed: {exc}") from exc

    ctx.metric("gemini_tool_rounds", float(rounds))
    return AgentOutput(
        content=resp.text,
        metadata={
            "finish_reason": resp.finish_reason,
            "tool_rounds": rounds,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "thoughts_tokens": resp.usage.thoughts_tokens,
            "cost_microusd": resp.usage.cost_microusd,
        },
    )


@agent.handler(mode="stream")
async def handle_stream(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    """Streaming variant — `StreamAccumulator` turns the raw delta
    iterator back into an `LlmResponse` (the streaming path yields
    deltas, not a response, so a naive `assistant_from_response`
    wouldn't apply). Streaming retry is bounded to pre-first-delta
    by design, so no `tools=` round-trip here."""
    ctx.progress.status("streaming")
    acc = StreamAccumulator()
    stream = await ctx.llm.generate(
        payload.prompt, preset="default", stream=True, retry=RetryPolicy()
    )
    async for delta in stream:
        acc.add(delta)
        if delta.text:
            ctx.progress.chunk(delta.text)
    resp = acc.build()
    return AgentOutput(
        content=resp.text,
        metadata={
            "finish_reason": resp.finish_reason,
            "output_tokens": resp.usage.output_tokens,
        },
    )


if __name__ == "__main__":
    agent.run()
