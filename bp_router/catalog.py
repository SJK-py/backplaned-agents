"""bp_router.catalog — Push catalog snapshots to live agents.

The agent's `Welcome` frame carries an `available_destinations` snapshot
that's frozen for the WS lifespan. To keep that cache aligned with
admin / onboarding changes without forcing a reconnect, the router
pushes a `CatalogUpdate` frame whenever the catalog could change for
a connected agent.

Triggers (all admin-frequency, never hot path):
- Agent onboards.
- Admin suspends an agent (`POST /v1/admin/agents/{id}/suspend`).
- Admin evicts an agent (`POST /v1/admin/agents/{id}/evict`).
- Admin mutates ACL rules (any of the firewall-rule endpoints).

Disconnects do NOT trigger a push — the resume window absorbs flaps,
and admit-time `agent_disconnected` is the existing safety net.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bp_protocol.frames import CatalogUpdateFrame
from bp_router.db import queries
from bp_router.visibility import available_destinations

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


async def push_catalog_update_to_all(state: AppState) -> int:
    """Recompute every connected agent's catalog and push CatalogUpdate.

    Returns the number of frames queued. Agents whose outbox is full
    are skipped with a warning rather than blocking the caller — the
    SDK rebuilds its catalog on the next reconnect anyway.
    """
    pool = state.db_pool  # type: ignore[attr-defined]
    settings = state.settings  # type: ignore[attr-defined]
    registry = state.socket_registry  # type: ignore[attr-defined]

    live_ids = registry.live_agent_ids()
    if not live_ids:
        return 0

    async with pool.acquire() as conn:
        all_agents = await queries.list_agents(conn)
    by_id = {a.agent_id: a for a in all_agents}

    sent = 0
    for agent_id in live_ids:
        entry = registry.get(agent_id)
        if entry is None:
            continue  # raced with a disconnect
        agent_row = by_id.get(agent_id)
        if agent_row is None:
            # Live socket without a matching DB row — eviction in flight,
            # close handler will tear it down.
            continue
        catalog = available_destinations(
            agent_row,
            all_agents,
            state.rules.rules,  # type: ignore[attr-defined]
            max_tier=settings.acl_max_tier,
        )
        frame = CatalogUpdateFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
            available_destinations=catalog,
        )
        try:
            entry.outbox.put_nowait(frame)
            sent += 1
        except asyncio.QueueFull:
            logger.warning(
                "catalog_update_outbox_full",
                extra={
                    "event": "catalog_update_outbox_full",
                    "bp.agent_id": agent_id,
                },
            )

    logger.info(
        "catalog_updates_pushed",
        extra={"event": "catalog_updates_pushed", "count": sent, "live": len(live_ids)},
    )
    return sent
