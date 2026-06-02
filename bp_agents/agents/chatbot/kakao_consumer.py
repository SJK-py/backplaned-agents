"""chatbot.kakao_consumer — the KakaoTalk pull loop.

Mirrors the Telegram `_poll_loop`: a `while not stop.is_set()` loop that
PULLS jobs from the Cloudflare Queue, hands each to `KakaoGateway`, and
ACKs the ones that processed cleanly. A pull/network error backs off (2s)
rather than tight-looping.

Ack policy ([kakao-channel.md] §5/§13): ack a job only when `handle_job`
returns without raising. `handle_job` swallows turn-level failures (it
delivers an apology) and is idempotent via a dedupe mark, so it raises
only on a pre-dedupe infra error (e.g. Redis) — exactly the case where
leaving the message unacked for redelivery is the right move.
"""

from __future__ import annotations

import asyncio
import logging

from bp_agents.agents.chatbot.kakao_client import KakaoClient
from bp_agents.agents.chatbot.kakao_gateway import KakaoGateway
from bp_agents.settings import SuiteSettings

logger = logging.getLogger(__name__)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Back off `seconds`, returning early if shutdown is signalled."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def kakao_consume_loop(
    gateway: KakaoGateway,
    client: KakaoClient,
    settings: SuiteSettings,
    stop: asyncio.Event,
) -> None:
    """Pull KakaoTalk jobs, dispatch each through the gateway, ack the
    successes. A pull error backs off (2s) like `_poll_loop`."""
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

        acks: list[str] = []
        for job in jobs:
            try:
                await gateway.handle_job(job)
                acks.append(job.lease_id)
            except Exception:  # noqa: BLE001
                # Pre-dedupe infra error → leave unacked for redelivery.
                logger.exception(
                    "kakao_job_error",
                    extra={"event": "kakao_job_error", "kakao.msg_id": job.msg_id},
                )
        if acks:
            try:
                await client.ack(acks)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_ack_error", extra={"event": "kakao_ack_error"}
                )
