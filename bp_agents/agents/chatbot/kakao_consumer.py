"""chatbot.kakao_consumer — the KakaoTalk pull loop (PR1 skeleton).

Mirrors the Telegram `_poll_loop`: a `while not stop.is_set()` loop that
PULLS jobs from the Cloudflare Queue and ACKs them, backing off on a pull
error (2s) rather than tight-looping.

PR1 is plumbing only — turn processing (the `KakaoGateway`, the deadline
state machine, the registry) lands in the next PR. For now the loop
drains and acks, logging each job so the relay → queue → agent path is
verifiable end-to-end without any user-facing behavior. Because the
gateway is not wired yet, pulled turns are acked-and-dropped (not
replayed forever); the consumer only runs at all when explicitly
configured, so this affects nothing until then.
"""

from __future__ import annotations

import asyncio
import logging

from bp_agents.agents.chatbot.kakao_client import KakaoClient
from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Back off `seconds`, returning early if shutdown is signalled."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def kakao_consume_loop(
    client: KakaoClient, settings: SuiteSettings, stop: asyncio.Event
) -> None:
    """Pull KakaoTalk jobs from the Cloudflare Queue and ack them.

    A pull/network error backs off (2s) like `_poll_loop`; on success
    every pulled message is acked so the queue drains.
    """
    logger.info(
        "kakao_consumer_started", extra={"event": "kakao_consumer_started"}
    )
    while not stop.is_set():
        try:
            jobs = await client.pull(
                batch_size=settings.kakao_pull_batch_size,
                visibility_timeout_s=settings.kakao_pull_visibility_timeout_s,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_pull_error", extra={"event": "kakao_pull_error"}
            )
            await _sleep_or_stop(stop, 2.0)
            continue

        if not jobs:
            continue

        for job in jobs:
            logger.info(
                "kakao_job_received",
                extra={"event": "kakao_job_received", "kakao.msg_id": job.msg_id},
            )
        try:
            await client.ack([job.lease_id for job in jobs])
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_ack_error", extra={"event": "kakao_ack_error"}
            )
