"""chatbot agent — the Telegram + KakaoTalk gateway process (Phase 1).

A gateway, not a handler-agent: `on_startup` opens the suite DB pool and
launches the Telegram long-poll loop. Each inbound message is handed to
`ChatbotGateway.handle_update`, which injects it as a root task on behalf
of the user and relays the result. No inbound router modes on the normal
path (proactive-push / cron modes land later).

A second, optional channel — KakaoTalk — runs alongside when its
Cloudflare Queue credentials are set: an egress-only pull consumer behind
a Worker relay ([../../../docs/design/kakao-channel.md]). It is launched
independently of `telegram_bot_token`, so either channel can run alone.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bp_agents.agents.chatbot.approval import approval_poll_loop
from bp_agents.agents.chatbot.credentials import HttpChannelCredentials
from bp_agents.agents.chatbot.cron import CronScheduler
from bp_agents.agents.chatbot.gateway import BOT_COMMANDS, ChatbotGateway
from bp_agents.agents.chatbot.kakao_client import HttpKakaoClient
from bp_agents.agents.chatbot.kakao_consumer import kakao_consume_loop
from bp_agents.agents.chatbot.kakao_files import R2FileEgress
from bp_agents.agents.chatbot.kakao_gateway import KakaoGateway
from bp_agents.agents.chatbot.kakao_registry import KakaoTaskRegistry
from bp_agents.agents.chatbot.telegram import (
    FileOffsetStore,
    HttpTelegramClient,
)
from bp_agents.db.connection import open_pool, open_redis
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
        description="Telegram + KakaoTalk channel + session manager.",
        groups=["channel", "inbound"],
        capabilities=[
            "channel.telegram",
            "channel.kakao",
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
_redis: object | None = None  # suite Redis (distributed session lock); None in dev
_telegram: HttpTelegramClient | None = None
_credentials: HttpChannelCredentials | None = None
_poll_task: asyncio.Task | None = None
_approval_task: asyncio.Task | None = None
_cron_task: asyncio.Task | None = None
_kakao: HttpKakaoClient | None = None
_kakao_gateway: KakaoGateway | None = None
_kakao_task: asyncio.Task | None = None
_kakao_approval_task: asyncio.Task | None = None
_inflight: set[asyncio.Task] = set()


def _on_update_done(task: asyncio.Task) -> None:
    """Done-callback for a per-message `handle_update` task: drop it from the
    in-flight set and surface any exception as a structured log instead of an
    unretrieved-task warning (the handler is fire-and-forget, so nothing else
    awaits it)."""
    _inflight.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error(
            "handle_update_failed",
            extra={"event": "handle_update_failed"},
            exc_info=exc,
        )
_stop = asyncio.Event()


def _kakao_configured(settings: SuiteSettings) -> bool:
    """True when all three KakaoTalk queue credentials are present — the
    activation gate for the pull consumer (the Kakao analogue of
    `telegram_bot_token` being set)."""
    return all(
        (
            settings.kakao_cf_account_id,
            settings.kakao_cf_queue_id,
            settings.kakao_cf_api_token,
        )
    )


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
    global _pool, _redis, _telegram, _credentials, _poll_task, _approval_task  # noqa: PLW0603
    _pool = await open_pool(_settings)
    # Suite Redis (optional): when set, the per-session lock is distributed,
    # so a second channel instance (webapp) can serialize turns with this one.
    _redis = await open_redis(_settings)

    # Control-plane client (registration submit, serviced-session
    # discovery, per-user session/cancel). Available whenever the
    # service principal was provisioned at onboarding.
    if agent.config.service_refresh_token:
        _credentials = HttpChannelCredentials(
            http_url=_http_url(), config=agent.config
        )
        _approval_task = asyncio.create_task(
            approval_poll_loop(
                credentials=_credentials, pool=_pool, settings=_settings, stop=_stop
            )
        )

    # KakaoTalk channel (optional, independent of Telegram). Egress-only
    # pull consumer behind the relay + queue (see kakao_consumer). Launched
    # before the Telegram early-return so it can run with Telegram unset.
    # Redis is required: the deadline/next-touch registry (next PR) is
    # Redis-backed, so the gate is declared now — enabling Kakao without
    # Redis fails loudly rather than half-working.
    global _kakao, _kakao_gateway, _kakao_task, _kakao_approval_task  # noqa: PLW0603
    if _kakao_configured(_settings) and _redis is not None:
        _kakao = HttpKakaoClient(_settings)
        # Outbound images go to R2 (presigned urls) when configured; inbound
        # images reuse the router named store regardless.
        _egress = R2FileEgress(_settings) if R2FileEgress.configured(_settings) else None
        _kakao_gateway = KakaoGateway(
            dispatcher=agent,
            pool=_pool,
            client=_kakao,
            registry=KakaoTaskRegistry(_redis, ttl_s=_settings.kakao_carry_ttl_s),
            settings=_settings,
            credentials=_credentials,
            egress=_egress,
            redis=_redis,
        )
        _kakao_task = asyncio.create_task(
            kakao_consume_loop(_kakao_gateway, _kakao, _settings, _stop)
        )
        # Reconcile admin-approved KakaoTalk registrations into
        # suite_platform_mappings(platform='kakao') — a second approval loop
        # for the kakao channel (the telegram one above is separate).
        if _credentials is not None:
            _kakao_approval_task = asyncio.create_task(
                approval_poll_loop(
                    credentials=_credentials, pool=_pool, settings=_settings,
                    stop=_stop, channel="chatbot_kakao", platform="kakao",
                )
            )
    elif _kakao_configured(_settings):
        logger.warning(
            "kakao_redis_required", extra={"event": "kakao_redis_required"}
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
        redis=_redis,
        delegatable_agents=frozenset(_settings.delegatable_agents),
    )
    offset_store = FileOffsetStore(Path(agent.config.state_dir) / "telegram_offset")
    _poll_task = asyncio.create_task(_poll_loop(gateway, offset_store))

    # Cron scheduler (v1 lives in the chatbot). Shares the gateway's
    # per-session lock so the apply step serializes with user turns.
    scheduler = CronScheduler(
        dispatcher=agent, pool=_pool, settings=_settings, telegram=_telegram,
        session_lock=gateway.session_lock, credentials=_credentials,
    )
    global _cron_task  # noqa: PLW0603
    _cron_task = asyncio.create_task(scheduler.run_loop(_stop))


@agent.on_shutdown
async def _shutdown() -> None:
    _stop.set()
    for t in (
        _poll_task, _approval_task, _cron_task, _kakao_task, _kakao_approval_task
    ):
        if t is not None:
            t.cancel()
    for t in list(_inflight):
        t.cancel()
    if _kakao_gateway is not None:
        await _kakao_gateway.aclose()
    if _telegram is not None:
        await _telegram.aclose()
    if _kakao is not None:
        await _kakao.aclose()
    if _credentials is not None:
        await _credentials.aclose()
    if _redis is not None:
        await _redis.aclose()
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
                gateway.handle_update(
                    update.chat_id, update.text, update.attachments
                )
            )
            _inflight.add(task)
            task.add_done_callback(_on_update_done)


if __name__ == "__main__":
    agent.run()
