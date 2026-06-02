"""chatbot.kakao_consumer — the KakaoTalk pull loop.

Mirrors the Telegram `_poll_loop`: a `while not stop.is_set()` loop that
PULLS jobs from the Cloudflare Queue and fans each out to its own task
(like `_poll_loop` does per update). Each job is acked from its own task
the moment it processes cleanly — a slow/overrunning turn never blocks
other chats' messages or delays their ack (head-of-line free). A pull/
network error backs off (2s) rather than tight-looping.

Ack policy ([kakao-channel.md] §5/§13): ack a job only when `handle_job`
returns without raising. `handle_job` swallows turn-level failures (it
delivers an apology) and is idempotent via a dedupe mark, so it raises
only on a pre-dedupe infra error (e.g. Redis) — exactly the case where
leaving the message unacked for redelivery is the right move.

Back-pressure: at most `max_inflight` turns run concurrently (each can
hold a slot up to `dispatch_result_timeout_s` while parked), so a burst
can't spawn unbounded tasks; surplus jobs simply wait in the queue until
a slot frees.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bp_agents.agents.chatbot.kakao_client import KakaoClient
from bp_agents.agents.chatbot.kakao_gateway import KakaoGateway
from bp_agents.settings import SuiteSettings

if TYPE_CHECKING:
    from bp_agents.agents.chatbot.kakao_client import KakaoJob

logger = logging.getLogger(__name__)

# CF Queues `pull` returns immediately when the queue is empty (no long-poll),
# so back off briefly on an empty pull — otherwise the loop would spin the
# pull API at 100% CPU when idle.
_IDLE_BACKOFF_S = 1.0


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Back off `seconds`, returning early if shutdown is signalled."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def _handle_and_ack(
    gateway: KakaoGateway, client: KakaoClient, job: KakaoJob
) -> None:
    """Process one job and ack it on clean completion. A raise (pre-dedupe
    infra error) leaves it unacked → the queue redelivers it."""
    try:
        await gateway.handle_job(job)
    except Exception:  # noqa: BLE001
        logger.exception(
            "kakao_job_error",
            extra={"event": "kakao_job_error", "kakao.msg_id": job.msg_id},
        )
        return
    try:
        await client.ack([job.lease_id])
    except Exception:  # noqa: BLE001
        logger.exception("kakao_ack_error", extra={"event": "kakao_ack_error"})


async def kakao_consume_loop(
    gateway: KakaoGateway,
    client: KakaoClient,
    settings: SuiteSettings,
    stop: asyncio.Event,
) -> None:
    """Pull KakaoTalk jobs and fan each out to a per-job task that acks on
    success. A pull error backs off (2s) like `_poll_loop`."""
    logger.info(
        "kakao_consumer_started", extra={"event": "kakao_consumer_started"}
    )
    inflight: set[asyncio.Task] = set()
    max_inflight = max(settings.kakao_pull_batch_size * 4, 8)
    try:
        while not stop.is_set():
            if len(inflight) >= max_inflight:
                await _sleep_or_stop(stop, 0.2)  # back-pressure: wait for a slot
                continue
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
                await _sleep_or_stop(stop, _IDLE_BACKOFF_S)
                continue

            for job in jobs:
                task = asyncio.create_task(_handle_and_ack(gateway, client, job))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
    finally:
        for t in list(inflight):
            t.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
