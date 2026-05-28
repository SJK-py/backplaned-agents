"""deep_reasoning plan_mode — a bespoke, in-process planning sub-loop.

When the delegated reasoning turn elects `plan_mode` ([agents.md]
deep_reasoning), control leaves the normal turn loop and enters this
module: an explicit plan (an ordered list of steps) the model manages and
executes one step at a time. Each `execute_step` spawns
`orchestrator(subagent)` — an l1→l0 call ([acl.md] `l1/* ->
l0/agent.orchestration`) — so a step gets the orchestrator's full
toolset; the result is recorded and the next step's decision opens with a
**fresh, bounded context** (objective + plan + result summaries), not an
ever-growing transcript.

Plan state lives only in memory for this turn ([deferred-work.md]); the
final report is the turn's `AgentOutput` (which terminates the delegated
task and keeps the session delegated to deep_reasoning).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bp_agents.common import (
    LocalToolset,
    emit_loop_progress,
    make_current_time_tool,
    make_send_file_tool,
    run_llm_loop,
    text_output,
)
from bp_agents.db import queries
from bp_protocol.types import AgentOutput, LLMData, TaskStatus
from bp_sdk import Message, ToolSpec
from bp_sdk.peers import PeerCallError

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.settings import SuiteSettings
    from bp_sdk import TaskContext

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT_ID = "orchestrator"

# Per-step executor instruction, prepended to the orchestrator subagent's
# system prompt (the model may append its own `additional_instruction`).
_STEP_INSTRUCTION = (
    "You are executing ONE step of a larger plan on behalf of a reasoning "
    "specialist. Carry out exactly this step using your tools and return a "
    "concise, self-contained result the planner can build on. Do not try to "
    "solve the whole objective."
)

# Plan-control tools — advertised in each decision loop and treated as
# terminal (the loop returns the moment one is called so we can apply it).
_ADD = "add_step"
_MODIFY = "modify_step"
_REMOVE = "remove_step"
_EXECUTE = "execute_step"
_QUIT = "quit_and_report"


def _spec(name: str, description: str, props: dict, required: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": props, "required": required},
    )


_ADD_SPEC = _spec(
    _ADD,
    "Insert a new step into the plan after the given 1-indexed position "
    "(0 = make it the first step).",
    {
        "add_after_num": {"type": "integer", "description": "1-indexed step to insert after (0 = front)."},
        "contents": {"type": "string", "description": "What the new step should accomplish."},
    },
    ["add_after_num", "contents"],
)
_MODIFY_SPEC = _spec(
    _MODIFY,
    "Rewrite an existing step.",
    {
        "target_step_num": {"type": "integer", "description": "1-indexed step to rewrite."},
        "contents": {"type": "string", "description": "New step text."},
    },
    ["target_step_num", "contents"],
)
_REMOVE_SPEC = _spec(
    _REMOVE,
    "Delete a step from the plan.",
    {"target_step_num": {"type": "integer", "description": "1-indexed step to delete."}},
    ["target_step_num"],
)
_EXECUTE_SPEC = _spec(
    _EXECUTE,
    "Execute the CURRENT step: a specialist carries it out and returns a "
    "result, which is recorded before the next step.",
    {
        "relevant_context": {
            "type": "string",
            "description": "Context from earlier results the executor needs for this step.",
        },
        "additional_instruction": {
            "type": "string",
            "description": "Optional extra guidance for how to execute this step.",
        },
    },
    ["relevant_context"],
)
_QUIT_SPEC = _spec(
    _QUIT,
    "Finish the plan and report the final answer to the user.",
    {"result_content": {"type": "string", "description": "The final answer for the user."}},
    ["result_content"],
)

_DECISION_SPECS = [_ADD_SPEC, _MODIFY_SPEC, _REMOVE_SPEC, _EXECUTE_SPEC, _QUIT_SPEC]
_DECISION_NAMES = {s.name for s in _DECISION_SPECS}
# The plan-exhausted final loop: only re-extend (add_step) or finish (write text).
_FINAL_SPECS = [_ADD_SPEC]
_FINAL_NAMES = {_ADD}


def _numbered(steps: list[str]) -> str:
    if not steps:
        return "(no steps yet)"
    return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))


def _results_digest(results: list[dict[str, Any]], *, limit: int = 400) -> str:
    if not results:
        return "(no steps executed yet)"
    lines: list[str] = []
    for r in results:
        body = r["summary"].strip().replace("\n", " ")
        if len(body) > limit:
            body = body[:limit] + "…"
        files = f" [files: {', '.join(r['files'])}]" if r["files"] else ""
        lines.append(f"- Step {r['n']} ({r['status']}): {body}{files}")
    return "\n".join(lines)


def _planner_system(objective: str, steps: list[str], results: list[dict]) -> str:
    return (
        "You are running a structured plan to accomplish the user's objective. "
        "Manage the plan and execute it one step at a time.\n\n"
        f"## Objective\n{objective}\n\n"
        f"## Plan\n{_numbered(steps)}\n\n"
        f"## Results so far\n{_results_digest(results)}\n\n"
        "Tools: add_step / modify_step / remove_step adjust the plan "
        "(steps are 1-indexed); execute_step runs the CURRENT step; read_file "
        "inspects a file a step produced; send_file marks a file to deliver to "
        "the user with your final report; quit_and_report finishes with the "
        "final answer. Adjust the plan only when the results make it necessary. "
        "Call exactly one plan tool."
    )


def _planner_user(steps: list[str], cursor: int) -> str:
    if not steps:
        return (
            "The plan is empty. Add the first step with add_step, or — if no "
            "plan is needed — finish with quit_and_report."
        )
    return (
        f"## Current step ({cursor + 1}/{len(steps)})\n{steps[cursor]}\n\n"
        "Decide: modify the plan, execute this step, or finalize."
    )


def _final_system(objective: str, results: list[dict]) -> str:
    return (
        "Every planned step is complete. Using the results below, write the "
        "final answer to the user's objective. If something essential is still "
        "missing, call add_step to extend the plan; otherwise just write the "
        "answer (no tool call). Use send_file to attach any produced files.\n\n"
        f"## Objective\n{objective}\n\n## Results\n{_results_digest(results, limit=800)}"
    )


async def _execute_step(
    ctx: TaskContext,
    step: str,
    args: dict[str, Any],
    results: list[dict],
    *,
    settings: SuiteSettings,
) -> dict[str, Any]:
    """Run one step via orchestrator(subagent); return a recorded result."""
    add_instr = str(args.get("additional_instruction") or "").strip()
    rel_ctx = str(args.get("relevant_context") or "").strip()
    prior = _results_digest(results)
    context = "\n\n".join(
        p for p in (rel_ctx, f"## Results so far\n{prior}" if results else "") if p
    )
    instruction = f"{_STEP_INSTRUCTION}\n\n{add_instr}" if add_instr else _STEP_INSTRUCTION
    n = len(results) + 1
    try:
        child = await ctx.peers.spawn(
            ORCHESTRATOR_AGENT_ID,
            LLMData(prompt=step, agent_instruction=instruction, context=context or None),
            mode="subagent", wait=True, timeout_s=settings.plan_step_timeout_s,
        )
    except PeerCallError as exc:
        logger.warning("plan_step_failed", extra={"event": "plan_step_failed", "step": n})
        return {"n": n, "step": step, "status": "failed", "summary": f"(step failed: {exc})", "files": []}
    out = child.output
    ok = child.status == TaskStatus.SUCCEEDED
    return {
        "n": n, "step": step, "status": "ok" if ok else "failed",
        "summary": (out.content if out else "") or "", "files": list(out.files) if out else [],
    }


async def run_plan(
    ctx: TaskContext,
    *,
    objective: str,
    initial_steps: list[str],
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    """Drive the plan to a final `AgentOutput`. Bounded by
    `plan_max_steps` / `plan_max_iters` so it always terminates."""
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_pro if cfg else settings.default_preset_pro
    timezone = cfg.timezone if cfg else settings.default_timezone

    steps: list[str] = [s for s in initial_steps if s][: settings.plan_max_steps]
    results: list[dict[str, Any]] = []
    out_files: list[str] = []
    cursor = 0

    def _tools() -> LocalToolset:
        return LocalToolset(
            [make_current_time_tool(timezone), make_send_file_tool(out_files)]
        )

    await emit_loop_progress(ctx, kind="status", detail=f"Planning: {objective[:80]}")

    for _ in range(settings.plan_max_iters):
        # Plan exhausted → compose the final answer (or extend the plan).
        if steps and cursor >= len(steps):
            resp = await run_llm_loop(
                ctx, messages=[
                    Message(role="system", content=_final_system(objective, results)),
                    Message(role="user", content="Write the final answer now."),
                ],
                preset=preset, local_tools=_tools(), use_peer_tools=False,
                extra_tools=_FINAL_SPECS, terminal_tools=_FINAL_NAMES,
                file_tools="read_only", detail_chars=settings.verbose_detail_chars,
            )
            add = next((tc for tc in resp.tool_calls if tc.name == _ADD), None)
            if add is not None and len(steps) < settings.plan_max_steps:
                steps.append(str(add.args.get("contents") or ""))
                continue
            return text_output(resp.text or "(plan complete)", files=out_files)

        resp = await run_llm_loop(
            ctx, messages=[
                Message(role="system", content=_planner_system(objective, steps, results)),
                Message(role="user", content=_planner_user(steps, cursor)),
            ],
            preset=preset, local_tools=_tools(), use_peer_tools=False,
            extra_tools=_DECISION_SPECS, terminal_tools=_DECISION_NAMES,
            file_tools="read_only", detail_chars=settings.verbose_detail_chars,
        )
        call = next((tc for tc in resp.tool_calls if tc.name in _DECISION_NAMES), None)
        if call is None:
            # The model answered without choosing a plan action — take it as final.
            return text_output(resp.text or "(no result)", files=out_files)

        args = call.args or {}
        if call.name == _QUIT:
            return text_output(
                str(args.get("result_content") or resp.text or ""), files=out_files
            )
        if call.name == _ADD:
            if len(steps) < settings.plan_max_steps:
                pos = max(0, min(int(args.get("add_after_num") or 0), len(steps)))
                steps.insert(pos, str(args.get("contents") or ""))
            continue
        if call.name == _MODIFY:
            i = int(args.get("target_step_num") or 0) - 1
            if 0 <= i < len(steps):
                steps[i] = str(args.get("contents") or steps[i])
            continue
        if call.name == _REMOVE:
            i = int(args.get("target_step_num") or 0) - 1
            if 0 <= i < len(steps):
                steps.pop(i)
                if cursor > i:
                    cursor -= 1
            continue
        if call.name == _EXECUTE:
            if not steps:
                continue
            step = steps[cursor]
            await emit_loop_progress(
                ctx, kind="status",
                detail=f"Step {cursor + 1}/{len(steps)}: {step[:60]}",
            )
            results.append(await _execute_step(ctx, step, args, results, settings=settings))
            cursor += 1
            continue

    # Iteration budget exhausted — best-effort report from what we have.
    await emit_loop_progress(ctx, kind="status", detail="Plan budget reached; reporting.")
    summary = _results_digest(results, limit=800)
    return text_output(
        f"I worked through the plan but reached my step budget. Here's what I "
        f"found so far:\n\n{summary}",
        files=out_files,
    )
