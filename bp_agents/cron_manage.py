"""bp_agents.cron_manage — cron-job management (the conversational
add/list/remove/modify loop) shared by the agent that hosts the `/cron`
command.

Split out of the chatbot so it runs on a *different* agent than the
channel: the channel spawns the management task, and the router forbids an
agent invoking itself (`bp_router.acl` denies `caller == callee`,
`<self_call>`). v1 hosts this on the **config** agent (l2) — a normal
`channel → l2` call. The scheduler (firing) stays in the chatbot; the two
halves only share the `cron_jobs` table.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from croniter import croniter

from bp_agents.common import LocalTool, LocalToolset, run_llm_loop, text_output
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_sdk import Message, ToolSpec

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.db.models import CronJobRow
    from bp_protocol.types import AgentOutput
    from bp_sdk import TaskContext

# Report policy values (also read by the scheduler's _effective_report).
REPORT_ALWAYS, REPORT_NEVER, REPORT_CBC = "always", "never", "case_by_case"


class CronError(ValueError):
    """An invalid cron expression (or other bad cron input)."""


def is_valid_cron(expr: str) -> bool:
    """Standard 5-field cron validity (croniter)."""
    return croniter.is_valid(expr)


# -- shared add / remove (the LLM toolset AND the webapp form call these,
#    so validation + ownership are single-sourced, [webapp.md] §5) ----------


async def add_cron(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    session_id: str,
    cron_expression: str,
    cron_message: str,
    timezone: str = "UTC",
    report: str = REPORT_CBC,
) -> CronJobRow:
    """Validate + create a cron job. Raises `CronError` on a bad expression."""
    if not is_valid_cron(cron_expression):
        raise CronError(f"Invalid cron expression: {cron_expression!r}")
    async with pool.acquire() as conn:
        return await queries.create_cron_job(
            conn, cron_id=uuid.uuid4().hex, user_id=user_id,
            session_id=session_id, cron_expression=cron_expression,
            cron_message=cron_message, timezone=timezone, report=report,
        )


async def remove_cron(pool: asyncpg.Pool, *, user_id: str, cron_id: str) -> bool:
    """Delete a job the user owns. Returns False if it doesn't exist or isn't
    theirs (so callers can 404/report without leaking other users' ids)."""
    async with pool.acquire() as conn:
        job = await queries.get_cron_job(conn, cron_id)
        if job is None or job.user_id != user_id:
            return False
        await queries.remove_cron_job(conn, cron_id)
    return True


def make_cron_tools(pool: asyncpg.Pool) -> list[LocalTool]:
    async def _add(ctx: TaskContext, args: dict[str, Any]) -> str:
        try:
            job = await add_cron(
                pool, user_id=ctx.user_id, session_id=ctx.session_id,
                cron_expression=args["cron_expression"],
                cron_message=args["cron_message"],
                timezone=args.get("timezone", "UTC"),
                report=args.get("report", REPORT_CBC),
            )
        except CronError as exc:
            return str(exc)
        return f"Created job {job.cron_id} ({job.cron_expression})."

    async def _list(ctx: TaskContext, args: dict[str, Any]) -> str:
        async with pool.acquire() as conn:
            jobs = await queries.list_cron_jobs(conn, user_id=ctx.user_id)
        if not jobs:
            return "No scheduled jobs."
        return "\n".join(
            f"- {j.cron_id} [{j.status}] {j.cron_expression} ({j.timezone}): "
            f"{j.cron_message}"
            for j in jobs
        )

    async def _remove(ctx: TaskContext, args: dict[str, Any]) -> str:
        if not await remove_cron(pool, user_id=ctx.user_id, cron_id=args["cron_id"]):
            return "No such job."
        return f"Removed job {args['cron_id']}."

    async def _modify(ctx: TaskContext, args: dict[str, Any]) -> str:
        cron_id = args.pop("cron_id")
        async with pool.acquire() as conn:
            job = await queries.get_cron_job(conn, cron_id)
            if job is None or job.user_id != ctx.user_id:
                return "No such job."
            fields = {
                k: v for k, v in args.items()
                if k in ("cron_expression", "timezone", "report", "cron_message", "status")
            }
            if "cron_expression" in fields and not croniter.is_valid(
                fields["cron_expression"]
            ):
                return f"Invalid cron expression: {fields['cron_expression']!r}"
            await queries.update_cron_job(conn, cron_id, **fields)
        return f"Updated job {cron_id}."

    _obj = {"type": "object", "additionalProperties": True}
    return [
        LocalTool(spec=ToolSpec(name="add_cron", description="Schedule a new recurring job.", parameters={
            "type": "object",
            "properties": {
                "cron_expression": {"type": "string", "description": "Standard 5-field cron."},
                "cron_message": {"type": "string", "description": "The scheduled prompt to run."},
                "timezone": {"type": "string", "description": "IANA tz (default UTC)."},
                "report": {"type": "string", "enum": [REPORT_ALWAYS, REPORT_NEVER, REPORT_CBC]},
            },
            "required": ["cron_expression", "cron_message"],
        }), handler=_add),
        LocalTool(spec=ToolSpec(name="list_cron", description="List the user's scheduled jobs.", parameters=_obj), handler=_list),
        LocalTool(spec=ToolSpec(name="remove_cron", description="Delete a scheduled job by id.", parameters={
            "type": "object", "properties": {"cron_id": {"type": "string"}}, "required": ["cron_id"],
        }), handler=_remove),
        LocalTool(spec=ToolSpec(name="modify_cron", description="Modify a scheduled job by id.", parameters={
            "type": "object",
            "properties": {
                "cron_id": {"type": "string"},
                "cron_expression": {"type": "string"},
                "timezone": {"type": "string"},
                "report": {"type": "string"},
                "cron_message": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "inactive"]},
            },
            "required": ["cron_id"],
        }), handler=_modify),
    ]


_CRON_SYSTEM = """\
You manage the user's scheduled jobs (reminders, recurring tasks). Use the \
tools to add / list / remove / modify jobs. Cron expressions are standard \
5-field (minute hour day month weekday). Always confirm what you did in \
plain language, and when listing jobs, present them clearly.\
"""


async def run_cron_management(
    ctx: TaskContext, payload: MessagePayload, *, pool: asyncpg.Pool, preset: str,
    language: str | None = None,
) -> AgentOutput:
    # /cron dispatches straight here, bypassing the orchestrator that would
    # otherwise carry the user's language — so instruct the model explicitly.
    system = _CRON_SYSTEM
    if language:
        system += (
            f" Write your entire reply in the user's preferred language "
            f"(their `language` setting: {language})."
        )
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=payload.prompt),
    ]
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset,
        local_tools=LocalToolset(make_cron_tools(pool)), use_peer_tools=False,
    )
    if resp.text and resp.text.strip():
        return text_output(resp.text)
    # Empty model turn (e.g. it called a tool and stopped) — fall back to the
    # current job list so the user always gets something concrete.
    async with pool.acquire() as conn:
        jobs = await queries.list_cron_jobs(conn, user_id=ctx.user_id)
    if not jobs:
        return text_output("You have no scheduled jobs.")
    listing = "\n".join(
        f"- {j.cron_id} [{j.status}] {j.cron_expression} ({j.timezone}): {j.cron_message}"
        for j in jobs
    )
    return text_output(f"Your scheduled jobs:\n{listing}")
