"""config agent — conversational user-config management (l2)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bp_agents.common import LocalTool, LocalToolset, run_llm_loop, text_output
from bp_agents.common.payloads import MessagePayload
from bp_agents.cron_manage import run_cron_management
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext, ToolSpec

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

CONFIG_AGENT_ID = "config"

# Fields the user may read/set conversationally (a subset of user_config).
_EDITABLE = {
    "full_name": str,
    "timezone": str,
    "language": str,
    "verbose_default": bool,
    "custom_note": str,
    "max_context_token_limit": int,
}

_SYSTEM = """\
You manage the user's settings. Use `get_config` to read current values and \
`set_config` to change one. Editable fields: full_name, timezone (IANA), \
language, verbose_default (true/false), custom_note, max_context_token_limit. \
ALWAYS end your reply by stating the relevant settings in plain language: on \
a read, list the current values; after a change, restate the field's new \
value. Never reply with only an acknowledgement like "done".\
"""


def _format_config(cfg: Any) -> str:
    if cfg is None:
        return "No settings found."
    return "\n".join(f"{f}: {getattr(cfg, f)}" for f in _EDITABLE)


async def _build_tools(pool: asyncpg.Pool) -> LocalToolset:
    async def _get(ctx: TaskContext, args: dict[str, Any]) -> str:
        async with pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, ctx.user_id)
        return _format_config(cfg)

    async def _set(ctx: TaskContext, args: dict[str, Any]) -> str:
        field = args.get("field")
        if field not in _EDITABLE:
            return f"Unknown field {field!r}. Editable: {sorted(_EDITABLE)}"
        raw = args.get("value")
        typ = _EDITABLE[field]
        try:
            if typ is bool:
                value: Any = str(raw).lower() in ("1", "true", "yes", "on")
            elif typ is int:
                value = int(raw)
            else:
                value = str(raw)
        except (TypeError, ValueError):
            return f"Invalid value for {field}: {raw!r}"
        async with pool.acquire() as conn:
            await queries.update_user_config(conn, ctx.user_id, **{field: value})
        return f"Set {field} = {value}."

    return LocalToolset([
        LocalTool(
            spec=ToolSpec(
                name="get_config", description="Show the user's current settings.",
                parameters={"type": "object", "properties": {}},
            ),
            handler=_get,
        ),
        LocalTool(
            spec=ToolSpec(
                name="set_config", description="Set one settings field.",
                parameters={
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": sorted(_EDITABLE)},
                        "value": {"type": "string"},
                    },
                    "required": ["field", "value"],
                },
            ),
            handler=_set,
        ),
    ])


agent = Agent(
    info=AgentInfo(
        agent_id=CONFIG_AGENT_ID,
        description="User self-service: account settings and scheduled jobs.",
        groups=["l2"],
        capabilities=["user.config", "user.cron"],
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None


@agent.on_startup
async def _startup() -> None:
    global _pool  # noqa: PLW0603 — startup-wired handle
    _pool = await open_pool(_settings)


@agent.on_shutdown
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


async def run_config(
    ctx: TaskContext,
    payload: MessagePayload,
    *,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
) -> AgentOutput:
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_lite if cfg else settings.default_preset_lite
    tools = await _build_tools(pool)
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content=payload.prompt),
    ]
    resp = await run_llm_loop(
        ctx, messages=messages, preset=preset, local_tools=tools,
        use_peer_tools=False,
    )
    if resp.text and resp.text.strip():
        return text_output(resp.text)
    # The model produced no prose (e.g. it called a tool and stopped) — show
    # the current settings rather than a bare "Done." that hides the result.
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    return text_output(_format_config(cfg))


@agent.handler(
    mode="message",
    description="Read or change the user's settings — name, timezone, "
    "language, verbose mode, context-token limit, custom note.",
)
async def message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_config(ctx, payload, pool=_pool, settings=_settings)


@agent.handler(
    mode="cron", tool=False,
    description="Manage the user's scheduled jobs (add/list/remove/modify); "
    "reached via the /cron command.",
)
async def cron(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    """Cron-job management (add/list/remove/modify) — reached via the
    channel's `/cron` command. Hosted here (not the chatbot) because the
    router forbids an agent invoking itself ([acl.py] `<self_call>`)."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_lite if cfg else _settings.default_preset_lite
    return await run_cron_management(ctx, payload, pool=_pool, preset=preset)


if __name__ == "__main__":
    agent.run()
