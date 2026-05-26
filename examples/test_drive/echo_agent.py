"""Test-drive: echo agent — the handler-fundamentals reference.

The smallest agent that still exercises everything a *leaf* handler
typically touches, so it doubles as a feature tour:

  * Typed I/O — `LLMData` in, `AgentOutput` out. `accepts_schema` /
    `produces_schema` (and `non_tool_modes`) are auto-derived from
    the handler signatures on registration; no manual schema
    plumbing.
  * Two modes in one unified registry — a data-plane `LLMData`
    handler (LLM-callable) and a `tool=False` control-plane `ping`
    handler (validated + dispatched, but hidden from `build_tools`).
  * Lifecycle hooks — `@agent.on_startup` / `@agent.on_shutdown`.
  * Typed errors — `InputValidationError` → status_code 400 at the
    boundary (the SDK maps the exception; the handler just raises).
  * Observability — `ctx.log` (pre-bound trace/task/agent),
    `ctx.metric(...)`, `ctx.child_span(...)`.
  * Progress — `ctx.progress.status()` / `.chunk()` so a streaming
    caller (see `caller_agent.py`) sees interim output.
  * Cooperative cancellation — `ctx.cancel_token.raise_if_cancelled()`
    inside the work loop; the SDK turns it into a CANCELLED result.
  * Session files — `ctx.files.list()` (router-managed stash,
    addressed by name; bytes ride out-of-band, NOT in `payload`).

Run with:

    AGENT_INVITATION_TOKEN=<token-from-POST-/v1/admin/invitations> \\
    AGENT_ROUTER_URL=ws://127.0.0.1:8000/v1/agent \\
    AGENT_STATE_DIR=/tmp/echo-agent-state \\
        python examples/test_drive/echo_agent.py
"""

from __future__ import annotations

from pydantic import BaseModel

from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, InputValidationError, TaskContext

agent = Agent(
    info=AgentInfo(
        agent_id="echo_agent",
        description="Test-drive echo agent — uppercases the prompt.",
        groups=["test_drive"],
        capabilities=["text.transform.uppercase"],
    ),
)


class Ping(BaseModel):
    """Control-plane payload — a liveness probe."""

    nonce: str = ""


# ---------------------------------------------------------------------------
# Lifecycle hooks — run once per process, around the run loop.
# ---------------------------------------------------------------------------


@agent.on_startup
async def _warm_up() -> None:
    # Real agents open DB pools / load models here. Hooks are awaited
    # before the first task is dispatched.
    print("echo_agent: startup hook ran")


@agent.on_shutdown
async def _drain() -> None:
    print("echo_agent: shutdown hook ran")


# ---------------------------------------------------------------------------
# Data-plane handler (mode "LLMData", LLM-callable).
# ---------------------------------------------------------------------------


@agent.handler
async def handle_echo(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    ctx.log.info(
        "echo.start",
        extra={"event": "echo.start", "prompt_len": len(payload.prompt)},
    )

    # Boundary validation as a typed error. The SDK turns this into
    # `Result(status=failed, status_code=400)` — no try/except, no
    # manual frame building.
    if not payload.prompt.strip():
        raise InputValidationError("prompt must be non-empty")

    # Files travel out-of-band in the router-managed stash, addressed
    # by NAME (NOT in `payload`) so the typed contract stays clean.
    # List this session's stash; a real agent would
    # `await ctx.files.read(name)` for the bytes, or
    # `ctx.files.llm_ref(name)` to show one to an LLM.
    stash_names = await ctx.files.list()
    if stash_names:
        ctx.log.info(
            "echo.stash_files",
            extra={"event": "echo.stash_files", "count": len(stash_names)},
        )

    ctx.progress.status("transforming")

    # `child_span` nests an OTel span under the task span — every
    # `ctx.log` / `ctx.metric` inside is correlated to it.
    words = payload.prompt.split()
    out: list[str] = []
    with ctx.child_span("uppercase"):
        for i, word in enumerate(words):
            # Cooperative cancellation: a long handler must yield the
            # cancel check itself (the SDK can't preempt user code).
            # Raising here → the SDK emits a CANCELLED result (499).
            ctx.cancel_token.raise_if_cancelled()
            out.append(word.upper())
            # Streamed interim output — a `spawn(stream=True)` caller
            # consumes these as ProgressFrames.
            ctx.progress.chunk(f"[{i + 1}/{len(words)}] {word.upper()}")

    result = " ".join(out)

    # Prometheus-style counter/observation, surfaced via the SDK's
    # metrics bridge.
    ctx.metric("echo_agent_chars_total", float(len(result)))

    return AgentOutput(
        content=result,
        metadata={
            "word_count": len(words),
            "stash_file_count": len(stash_names),
        },
    )


# ---------------------------------------------------------------------------
# Control-plane handler (mode "Ping", tool=False → in non_tool_modes,
# so `build_tools` never advertises it to a tool-using model).
# ---------------------------------------------------------------------------


@agent.handler(tool=False)
async def handle_ping(ctx: TaskContext, payload: Ping) -> AgentOutput:
    ctx.log.info("echo.ping", extra={"event": "echo.ping"})
    return AgentOutput(content="pong", metadata={"nonce": payload.nonce})


if __name__ == "__main__":
    agent.run()
