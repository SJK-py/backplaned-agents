"""Shared page helpers for the webapp."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.db import queries

if TYPE_CHECKING:
    from bp_agents.db.models import SessionInfoRow

# session_info.channel value the chatbot writes for Telegram-origin
# sessions — flagged in the UI (list badge + a one-time note on open) so the
# user knows progress won't mirror back to Telegram if continued here.
TELEGRAM_CHANNEL = "chatbot_telegram"


async def owned_session(request: Request, session_id: str) -> SessionInfoRow | None:
    """The user's `session_info` row for `session_id`, or None (→ 404). The
    router's `admit_task` / file-scope check is the ultimate ownership gate;
    this is the local UX guard + the source of the active thread."""
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        return None
    async with pool.acquire() as conn:
        info = await queries.get_session_info(conn, session_id)
    if info is None or info.user_id != user_id:
        return None
    return info
