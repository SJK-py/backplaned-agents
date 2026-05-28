"""chatbot agent — the Telegram gateway process (Phase 1).

A gateway, not a handler-agent: `on_startup` opens the suite DB pool and
launches the Telegram long-poll loop. Each inbound message is handed to
`ChatbotGateway.handle_update`, which injects it as a root task on behalf
of the user and relays the result. No inbound router modes on the normal
path (proactive-push / cron modes land later).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bp_agents.agents.chatbot.approval import approval_poll_loop
from bp_agents.agents.chatbot.credentials import HttpChannelCredentials
from bp_agents.agents.chatbot.cron import CronScheduler, run_cron_management
from bp_agents.agents.chatbot.gateway import BOT_COMMANDS, ChatbotGateway
from bp_agents.agents.chatbot.telegram import (
    FileOffsetStore,
    HttpTelegramClient,
)
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

CHATBOT_AGENT_ID = "chatbot"


agent = Agent(
    info=AgentInfo(
        agent_id=CHATBOT_AGENT_ID,
        description="Telegram channel + session manager.",
        groups=["channel", "inbound"],
        capabilities=[
            "channel.telegram",
            "user.auth",
            "user.registration",
            "user.cron",
            "file.full",
            "session.history",
            "session.management",
        ],
        hidden=True,
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None
_telegram: HttpTelegramClient | None = None
_credentials: HttpChannelCredentials | None = None
_poll_task: asyncio.Task | None = None
_approval_task: asyncio.Task | None = None
_cron_task: asyncio.Task | None = None
_inflight: set[asyncio.Task] = set()
_stop = asyncio.Event()


def _http_url() -> str:
    """Derive the router's HTTP base from its WS url (for the channel's
    control-plane client)."""
    url = agent.config.router_url
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :].split("/v1/")[0]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :].split("/v1/")[0]
    return url


@agent.on_startup
async def _startup() -> None:
    global _pool, _telegram, _credentials, _poll_task, _approval_task  # noqa: PLW0603
    _pool = await open_pool(_settings)

    # Control-plane client (registration submit, serviced-session
    # discovery, per-user session/cancel). Available whenever the
    # service principal was provisioned at onboarding.
    if agent.config.service_refresh_token:
        _credentials = HttpChannelCredentials(
            http_url=_http_url(), config=agent.config
        )
        _approval_task = asyncio.create_task(
            approval_poll_loop(credentials=_credentials, pool=_pool, stop=_stop)
        )

    if not _settings.telegram_bot_token:
        logger.warning(
            "telegram_token_unset",
            extra={"event": "telegram_token_unset"},
        )
        return

    _telegram = HttpTelegramClient(
        _settings.telegram_bot_token, base_url=_settings.telegram_base_url
    )
    # Advertise the command list to Telegram's "/" menu. Best-effort — a
    # failure here must not stop the bot from polling.
    try:
        await _telegram.set_my_commands(BOT_COMMANDS)
    except Exception:  # noqa: BLE001
        logger.warning("set_my_commands_failed", extra={"event": "set_my_commands_failed"})
    gateway = ChatbotGateway(
        dispatcher=agent,
        pool=_pool,
        telegram=_telegram,
        credentials=_credentials,
        result_timeout_s=_settings.dispatch_result_timeout_s,
        fire_memory=True,
    )
    offset_store = FileOffsetStore(Path(agent.config.state_dir) / "telegram_offset")
    _poll_task = asyncio.create_task(_poll_loop(gateway, offset_store))

    # Cron scheduler (v1 lives in the chatbot). Shares the gateway's
    # per-session lock so the apply step serializes with user turns.
    scheduler = CronScheduler(
        dispatcher=agent, pool=_pool, settings=_settings, telegram=_telegram,
        session_lock=gateway._session_lock, credentials=_credentials,
    )
    global _cron_task  # noqa: PLW0603
    _cron_task = asyncio.create_task(scheduler.run_loop(_stop))


@agent.on_shutdown
async def _shutdown() -> None:
    _stop.set()
    for t in (_poll_task, _approval_task, _cron_task):
        if t is not None:
            t.cancel()
    for t in list(_inflight):
        t.cancel()
    if _telegram is not None:
        await _telegram.aclose()
    if _credentials is not None:
        await _credentials.aclose()
    if _pool is not None:
        await _pool.close()


@agent.handler(mode="cron", tool=False)
async def cron(ctx: TaskContext, payload: MessagePayload) -> AgentOutput:
    """Cron job management (add/list/remove/modify) — reached via the
    channel's `/cron` command."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    preset = cfg.preset_lite if cfg else _settings.default_preset_lite
    return await run_cron_management(ctx, payload, pool=_pool, preset=preset)


async def _poll_loop(
    gateway: ChatbotGateway, offset_store: FileOffsetStore
) -> None:
    """Long-poll Telegram and fan each update out to a per-message task.

    A network error backs off (2s) rather than tight-looping; the offset
    is persisted after each update so a restart resumes past it.
    """
    assert _telegram is not None
    offset = offset_store.read()
    while not _stop.is_set():
        try:
            updates = await _telegram.get_updates(
                offset=offset, timeout_s=_settings.telegram_poll_timeout_s
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "telegram_poll_error", extra={"event": "telegram_poll_error"}
            )
            try:
                await asyncio.wait_for(_stop.wait(), timeout=2.0)
            except TimeoutError:
                pass
            continue
        for update in updates:
            offset = update.update_id + 1
            offset_store.write(offset)
            task = asyncio.create_task(
                gateway.handle_update(
                    update.chat_id, update.text, update.attachments
                )
            )
            _inflight.add(task)
            task.add_done_callback(_inflight.discard)


if __name__ == "__main__":
    agent.run()
