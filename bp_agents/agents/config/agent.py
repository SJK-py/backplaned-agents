"""config agent — conversational user-settings management (l2).

The settings themselves live in the router's **user-scoped state**
(`bp_agents.user_prefs`), so `get_config` / `set_config` ride
`ctx.history.user_scope` — this agent's own authenticated socket — not a
suite database. The pool it still holds is for cron management, whose jobs
are a suite table.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bp_agents import slots
from bp_agents.common import LocalTool, LocalToolset, run_llm_loop, text_output
from bp_agents.common.payloads import MessagePayload
from bp_agents.cron_manage import run_cron_management
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_agents.user_prefs import (
    ConfigError,
    as_display,
    coerce_config_value,
    editable_fields,
    load_prefs,
    save_pref,
)
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext, ToolSpec

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

CONFIG_AGENT_ID = "config"

# Editable fields + value coercion are shared with the webapp config form
# (bp_agents.user_prefs) so the NL path and the structured form agree.
#
# Model selection is NOT here: it is a router-side preference the user sets
# under their own authority on the web Settings page
# ([../../../docs/design/router-resolved-preset-slots.md] §4). No agent has a
# write path to a value the router enforces policy on, so this agent's job on
# the subject is to say where the switch lives.

_SYSTEM_BASE = (
    "You manage the user's settings. Use `get_config` to read current values "
    "and `set_config` to change one. Editable fields: full_name, timezone "
    "(IANA), language, verbose_default (true/false), custom_note, "
    "max_context_token_limit. If the user asks to change which AI MODEL they "
    "use, explain that model choice lives on the web Settings page — it is "
    "checked against their plan there — and that you can't change it from "
    "chat. Never guess which model they are on."
)
_SYSTEM_TAIL = (
    " ALWAYS end your reply by stating the relevant settings in plain "
    "language: on a read, list the current values; after a change, restate "
    "the field's new value. State ONLY values that appear in a tool result — "
    "never guess or recall a value you have not just read. Never reply with "
    'only an acknowledgement like "done".'
)


def _system_prompt(language: str | None = None) -> str:
    """The config system prompt. When the user has a `language` preference,
    instruct the model to write its reply in it (the `/config` dispatch
    bypasses the orchestrator, which would otherwise carry the language)."""
    lines = [_SYSTEM_BASE, _SYSTEM_TAIL]
    if language:
        lines.append(
            f" Write your entire reply in the user's preferred language "
            f"(their `language` setting: {language}); keep field names and "
            f"setting values verbatim."
        )
    return "".join(lines)


def _build_tools() -> LocalToolset:
    fields = editable_fields()

    async def _get(ctx: TaskContext, args: dict[str, Any]) -> str:
        return as_display(await load_prefs(ctx, _settings))

    async def _set(ctx: TaskContext, args: dict[str, Any]) -> str:
        field = args.get("field")
        try:
            value = coerce_config_value(field, args.get("value"))
        except ConfigError as exc:
            return str(exc)
        await save_pref(ctx, field, value)
        # Read back and return the FULL current settings. Without this the
        # model only sees the one changed field and, when asked to summarize,
        # fabricates the values of the others.
        return (
            f"Set {field} = {value}.\n\nCurrent settings:\n"
            f"{as_display(await load_prefs(ctx, _settings))}"
        )

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


async def run_config(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    prefs = await load_prefs(ctx, _settings)
    messages = [
        Message(role="system", content=_system_prompt(language=prefs.language)),
        Message(role="user", content=payload.prompt),
    ]
    resp = await run_llm_loop(
        ctx, messages=messages, slot=slots.LITE, local_tools=_build_tools(),
        use_peer_tools=False,
    )
    if resp.text and resp.text.strip():
        return text_output(resp.text)
    # The model produced no prose (e.g. it called a tool and stopped) — show
    # the current settings rather than a bare "Done." that hides the result.
    return text_output(as_display(await load_prefs(ctx, _settings)))


@agent.handler(
    mode="message",
    description="Read or change the user's settings — name, timezone, "
    "language, verbose mode, context-token limit, custom note.",
)
async def message(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    return await run_config(ctx, payload)


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
    prefs = await load_prefs(ctx, _settings)
    return await run_cron_management(
        ctx, payload, pool=_pool, language=prefs.language,
    )


if __name__ == "__main__":
    agent.run()
