"""config agent — conversational user-config management (l2)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bp_agents.common import LocalTool, LocalToolset, run_llm_loop, text_output
from bp_agents.common.payloads import MessagePayload
from bp_agents.config_edit import (
    ConfigError,
    coerce_config_value,
    displayable_fields,
    editable_fields,
    preset_choices_from_settings,
)
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

# Editable fields + value coercion are shared with the webapp config form
# (bp_agents.config_edit) so the NL path and the structured form agree. The
# per-tier LLM-preset fields are added only when the operator opts in via
# `SuiteSettings.selectable_presets_*` (an allow-list per tier).

_SYSTEM_BASE = (
    "You manage the user's settings. Use `get_config` to read current values "
    "and `set_config` to change one. Editable fields: full_name, timezone "
    "(IANA), language, verbose_default (true/false), custom_note, "
    "max_context_token_limit. The user also has three LLM model tiers — "
    "preset_pro (deep reasoning), preset_balanced (general), preset_lite "
    "(quick helpers); always report their current values on a read. They are "
    "only CHANGEABLE when listed as selectable below; if a tier isn't listed, "
    "tell the user it's set by their administrator and can't be changed here."
)
_SYSTEM_TAIL = (
    " ALWAYS end your reply by stating the relevant settings in plain "
    "language: on a read, list the current values; after a change, restate "
    "the field's new value. Never reply with only an acknowledgement like "
    '"done".'
)


def _system_prompt(
    preset_choices: dict[str, list[str]], language: str | None = None
) -> str:
    """The config system prompt, with a sentence per opted-in preset tier
    listing the names the user may pick (so the model offers real choices).
    When the user has a `language` preference, instruct the model to write its
    reply in it (the `/config` dispatch bypasses the orchestrator, which would
    otherwise carry the language)."""
    lines = [_SYSTEM_BASE]
    for field, choices in preset_choices.items():
        if choices:
            lines.append(
                f" The {field} (LLM model tier) may be set to one of: "
                f"{', '.join(choices)}."
            )
    lines.append(_SYSTEM_TAIL)
    if language:
        lines.append(
            f" Write your entire reply in the user's preferred language "
            f"(their `language` setting: {language}); keep field names and "
            f"setting values verbatim."
        )
    return "".join(lines)


def _format_config(cfg: Any, preset_choices: dict[str, list[str]]) -> str:
    if cfg is None:
        return "No settings found."
    # Show every displayable field, including the model tiers. A tier the
    # operator hasn't opened for editing (empty allow-list) is tagged
    # "(set by your administrator)" so its value is visible but clearly not
    # user-changeable. Without this a bare read hid the model entirely.
    editable = editable_fields(preset_choices)
    lines = []
    for f in displayable_fields():
        suffix = "" if f in editable else "  (set by your administrator)"
        lines.append(f"{f}: {getattr(cfg, f)}{suffix}")
    return "\n".join(lines)


async def _build_tools(
    pool: asyncpg.Pool, preset_choices: dict[str, list[str]]
) -> LocalToolset:
    fields = editable_fields(preset_choices)

    async def _get(ctx: TaskContext, args: dict[str, Any]) -> str:
        async with pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, ctx.user_id)
        return _format_config(cfg, preset_choices)

    async def _set(ctx: TaskContext, args: dict[str, Any]) -> str:
        field = args.get("field")
        try:
            value = coerce_config_value(
                field, args.get("value"), preset_choices=preset_choices
            )
        except ConfigError as exc:
            return str(exc)
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
                        "field": {"type": "string", "enum": sorted(fields)},
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
    preset_choices = preset_choices_from_settings(settings)
    tools = await _build_tools(pool, preset_choices)
    messages = [
        Message(
            role="system",
            content=_system_prompt(
                preset_choices, language=cfg.language if cfg else None
            ),
        ),
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
    return text_output(_format_config(cfg, preset_choices))


@agent.handler(
    mode="message",
    description="Read or change the user's settings — name, timezone, "
    "language, verbose mode, context-token limit, custom note.",
)
async def message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    assert _pool is not None
    return await run_config(ctx, payload, pool=_pool, settings=_settings)


@agent.handler(
    mode="cron",
    description="Manage the user's scheduled jobs / reminders — add, list, "
    "remove, or modify recurring tasks (e.g. \"remind me at 8am\").",
)
async def cron(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    """Cron-job management (add/list/remove/modify). Tool-visible: the
    orchestrator's LLM calls `call_config_cron` to set reminders, and the
    channel's `/cron` command spawns it directly. Hosted here (not the
    chatbot) because the router forbids an agent invoking itself
    ([acl.py] `<self_call>`); config is reachable only by the orchestrator
    and the channel."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_lite if cfg else _settings.default_preset_lite
    return await run_cron_management(
        ctx, payload, pool=_pool, preset=preset,
        language=cfg.language if cfg else None,
    )


if __name__ == "__main__":
    agent.run()
