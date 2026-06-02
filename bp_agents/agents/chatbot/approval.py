"""chatbot.approval — reconcile admin-approved users into the suite store.

A service-level channel can't see approval results directly; it discovers
its provisioned users by polling `GET /v1/admin/serviced-sessions`
(`credentials.list_serviced_sessions`). For each session carrying a
channel-native `external_id`, the channel writes the suite-side identity:
`suite_platform_mappings` (chat_id → user_id), a `user_config` row (seeding
`default_session_id`), and a `session_info` row. All writes are idempotent,
so re-polling is safe.

Channel-agnostic: `reconcile_serviced_sessions` / `approval_poll_loop` take
`channel` + `platform` (defaulting to Telegram), so the chatbot runs one
loop per active channel — Telegram (`chatbot_telegram`/`telegram`) and,
when enabled, KakaoTalk (`chatbot_kakao`/`kakao`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from bp_agents.agents.chatbot.credentials import ChannelCredentials, ServicedSession
from bp_agents.db import queries

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)

# Default channel/platform for the reconcile (overridable per call — the
# chatbot passes channel="chatbot_kakao"/platform="kakao" for its Kakao loop).
PLATFORM = "telegram"
CHANNEL = "chatbot_telegram"


async def reconcile_serviced_sessions(
    pool: asyncpg.Pool,
    records: list[ServicedSession],
    *,
    settings: SuiteSettings,
    platform: str = PLATFORM,
    channel: str = CHANNEL,
) -> int:
    """Write the suite-side identity for each discovered session. Returns
    the count of newly-mapped chats (an already-mapped chat is a no-op).
    Idempotent — safe to call repeatedly with overlapping records.

    `platform`/`channel` default to Telegram but are parametrized so the
    same reconcile serves the KakaoTalk channel (`kakao`/`chatbot_kakao`).

    A first-time `user_config` row seeds its per-tier LLM presets from
    `settings.default_preset_*` (`SUITE_DEFAULT_PRESET_{PRO,BALANCED,LITE,
    EMBEDDING}`), so an operator's configured tier defaults actually take
    effect at registration."""
    newly_mapped = 0
    for rec in records:
        if not rec.external_id:
            continue
        async with pool.acquire() as conn:
            existing = await queries.resolve_user_id(
                conn, platform=platform, chat_id=rec.external_id
            )
            await queries.upsert_platform_mapping(
                conn, platform=platform, chat_id=rec.external_id,
                user_id=rec.user_id,
            )
            # Seeds default_session_id + per-tier presets on first create;
            # a no-op for an existing config (so a later /new isn't clobbered).
            await queries.create_user_config(
                conn, user_id=rec.user_id, default_session_id=rec.session_id,
                preset_pro=settings.default_preset_pro,
                preset_balanced=settings.default_preset_balanced,
                preset_lite=settings.default_preset_lite,
                preset_embedding=settings.default_preset_embedding,
            )
            await queries.create_session_info(
                conn, session_id=rec.session_id, user_id=rec.user_id,
                channel=channel, chat_id=rec.external_id,
            )
        if existing is None:
            newly_mapped += 1
            logger.info(
                "registration_reconciled",
                extra={
                    "event": "registration_reconciled",
                    "bp.user_id": rec.user_id,
                    "external_id": rec.external_id,
                },
            )
    return newly_mapped


async def approval_poll_loop(
    *,
    credentials: ChannelCredentials,
    pool: asyncpg.Pool,
    settings: SuiteSettings,
    stop: asyncio.Event,
    interval_s: float = 30.0,
    channel: str = CHANNEL,
    platform: str = PLATFORM,
) -> None:
    """Poll `serviced-sessions` for `channel` on an interval and reconcile new
    ones into `platform` mappings. The cursor is in-memory (advances past the
    newest seen `opened_at`); a restart re-lists from scratch, which is safe
    because reconcile is idempotent. Run one loop per channel."""
    cursor: datetime | None = None
    while not stop.is_set():
        try:
            records = await credentials.list_serviced_sessions(
                channel=channel, since=cursor
            )
            if records:
                await reconcile_serviced_sessions(
                    pool, records, settings=settings,
                    platform=platform, channel=channel,
                )
                cursor = max(r.opened_at for r in records)
        except Exception:  # noqa: BLE001
            logger.exception(
                "approval_poll_error",
                extra={"event": "approval_poll_error", "channel": channel},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass
