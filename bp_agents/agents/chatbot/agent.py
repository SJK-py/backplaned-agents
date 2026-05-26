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

from bp_agents.agents.chatbot.gateway import ChatbotGateway
from bp_agents.agents.chatbot.telegram import (
    FileOffsetStore,
    HttpTelegramClient,
)
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo
from bp_sdk import Agent

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
_poll_task: asyncio.Task | None = None
_inflight: set[asyncio.Task] = set()
_stop = asyncio.Event()


@agent.on_startup
async def _startup() -> None:
    global _pool, _telegram, _poll_task  # noqa: PLW0603 — startup-wired handles
    _pool = await open_pool(_settings)

    if not _settings.telegram_bot_token:
        logger.warning(
            "telegram_token_unset",
            extra={"event": "telegram_token_unset"},
        )
        return

    _telegram = HttpTelegramClient(
        _settings.telegram_bot_token, base_url=_settings.telegram_base_url
    )
    gateway = ChatbotGateway(
        dispatcher=agent,
        pool=_pool,
        telegram=_telegram,
        result_timeout_s=_settings.dispatch_result_timeout_s,
    )
    offset_store = FileOffsetStore(Path(agent.config.state_dir) / "telegram_offset")
    _poll_task = asyncio.create_task(_poll_loop(gateway, offset_store))


@agent.on_shutdown
async def _shutdown() -> None:
    _stop.set()
    if _poll_task is not None:
        _poll_task.cancel()
    for t in list(_inflight):
        t.cancel()
    if _telegram is not None:
        await _telegram.aclose()
    if _pool is not None:
        await _pool.close()


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
                gateway.handle_update(update.chat_id, update.text)
            )
            _inflight.add(task)
            task.add_done_callback(_inflight.discard)


if __name__ == "__main__":
    agent.run()
