"""Test-drive: caller agent — the peer-orchestration reference.

Demonstrates the `ctx.peers` surface end to end:

  * Discovery — `peers.find(capability)` and `peers.describe(id)`
    against the ACL-filtered catalog (already scoped to the task's
    user level).
  * Streaming spawn — `async with peers.spawn(..., stream=True)`:
    consume the child's `ProgressFrame`s live, then await its
    terminal `ResultFrame`.
  * Typed peer errors — `SpawnRejected` / `ResultTimeout` /
    `PeerCallError` become graceful output instead of an unhandled
    crash.
  * File round-trip — files are shared by NAME in the router-managed
    session stash, so a child reaches a parent-stashed file by
    mentioning its name (no forwarding). A child's produced files
    arrive as NAMES on `result.output.files`.
  * Delegation — `ctx.delegating_agent_id` distinguishes a hand-off
    from a fresh call; the `handoff` mode shows `peers.delegate`,
    which reassigns THIS task to another agent.

Run AFTER `echo_agent.py` (and, for the `handoff` mode,
`gemini_agent.py`) is connected:

    AGENT_INVITATION_TOKEN=<token> \\
    AGENT_ROUTER_URL=ws://127.0.0.1:8000/v1/agent \\
    AGENT_STATE_DIR=/tmp/caller-agent-state \\
        python examples/test_drive/caller_agent.py
"""

from __future__ import annotations

from bp_protocol.types import AgentInfo, AgentOutput, LLMData, TaskStatus
from bp_sdk import (
    Agent,
    PeerCallError,
    ResultTimeout,
    SpawnRejected,
    TaskContext,
)

_TARGET_CAPABILITY = "text.transform.uppercase"


agent = Agent(
    info=AgentInfo(
        agent_id="caller_agent",
        description="Test-drive caller — discovers + streams a peer call.",
        groups=["test_drive"],
        capabilities=[_TARGET_CAPABILITY],
    ),
)


@agent.handler
async def handle_call(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    # `delegating_agent_id` is set when the router carried an
    # existing task_id forward — i.e. another agent delegated TO us.
    # Delegation is not a separate handler; you branch on this if you
    # care.
    if ctx.delegating_agent_id is not None:
        ctx.log.info(
            "caller.delegated_in",
            extra={
                "event": "caller.delegated_in",
                "from": ctx.delegating_agent_id,
            },
        )

    # Discovery against the ACL-filtered catalog. `find` only returns
    # agents callable at THIS task's user level.
    providers = await ctx.peers.find(_TARGET_CAPABILITY)
    target = next(
        (p.agent_id for p in providers if p.agent_id != agent.info.agent_id),
        "echo_agent",
    )
    described = await ctx.peers.describe(target)
    ctx.log.info(
        "caller.target",
        extra={
            "event": "caller.target",
            "target": target,
            "modes": list((described.accepts_schema or {}).keys()),
        },
    )

    chunks: list[str] = []
    try:
        # Streaming spawn: `async with` is the safe form — it always
        # `aclose()`s the subscription even on early break / error
        # (the un-managed form leaks until the correlation timeout).
        async with ctx.peers.spawn(
            target,
            payload,
            stream=True,
            timeout_s=30.0,
        ) as stream:
            async for pf in stream:
                # echo_agent emits status + per-word chunk events.
                if pf.event == "chunk":
                    chunks.append(pf.content)
                ctx.progress.status(f"child:{pf.event}")
            result = await stream.result()
    except SpawnRejected as exc:
        # Admit-time refusal (ACL / schema / quota / depth).
        return AgentOutput(content=f"spawn rejected: {exc}")
    except ResultTimeout:
        return AgentOutput(content=f"{target} did not finish within 30s")
    except PeerCallError as exc:
        return AgentOutput(content=f"peer call failed: {exc}")

    if result.status != TaskStatus.SUCCEEDED:
        return AgentOutput(
            content=f"{target} returned status {result.status.value}",
        )

    # Files the child produced arrive as NAMES on
    # `result.output.files` (router-managed stash). A peer in the same
    # user+session reaches them by name — `await ctx.files.read(name)`
    # for the bytes, or `ctx.files.llm_ref(name)` to show one to an LLM.
    produced_names = list(result.output.files) if result.output else []

    echoed = (result.output.content if result.output else "") or ""
    return AgentOutput(
        content=f"caller_agent saw {target} return: {echoed!r}",
        metadata={
            "streamed_chunks": len(chunks),
            "child_files": len(produced_names),
            "was_delegated_in": ctx.delegating_agent_id is not None,
        },
    )


@agent.handler(mode="handoff")
async def handle_handoff(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    """Hand THIS task off to `gemini_agent` — `delegate` reassigns
    the task_id, so the delegate becomes responsible for terminating
    it. The handler returns immediately afterwards; the SDK
    suppresses the default Result so there is exactly one terminal
    frame."""
    ctx.log.info("caller.handoff", extra={"event": "caller.handoff"})
    await ctx.peers.delegate("gemini_agent", payload, mode="LLMData")
    return AgentOutput()  # suppressed — the delegate terminates the task


if __name__ == "__main__":
    agent.run()
