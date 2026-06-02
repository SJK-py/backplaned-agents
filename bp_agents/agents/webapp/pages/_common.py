"""Shared page helpers for the webapp."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Request

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.upstream import UpstreamError
from bp_agents.db import queries

if TYPE_CHECKING:
    from bp_agents.db.models import SessionInfoRow

logger = logging.getLogger(__name__)

# session_info.channel values the chatbot writes for chat-origin sessions —
# flagged in the UI (list badge + a one-time note on open) so the user knows
# progress won't mirror back to the chat if continued here. A chat-origin
# session is retired only from the chatbot (`/new`, which releases its channel
# to NULL); the web app must NOT close/remove it while the channel is still
# set, or it would yank the cron-fallback `default_session_id` out from under
# the chat.
TELEGRAM_CHANNEL = "chatbot_telegram"
KAKAO_CHANNEL = "chatbot_kakao"
# Channels owned by a chatbot gateway — protected from web-app close/remove.
CHATBOT_CHANNELS = frozenset({TELEGRAM_CHANNEL, KAKAO_CHANNEL})


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


async def carrier_session(request: Request) -> str | None:
    """An OPEN session_id to ride for a per-user management dispatch (Memory /
    Knowledge pages). Root-task admit requires a real, open, owned session;
    the target agent works per-user, so any open session serves. Prefers the
    user's `default_session_id`, else the newest open one; None if none open."""
    upstream = request.app.state.upstream
    pool = request.app.state.pool
    user_id = session_user_id(request)
    access = request.session.get("access_token")
    if upstream is None or not access:
        return None
    try:
        sessions = await upstream.list_sessions(access_token=access)
    except UpstreamError:
        logger.warning("webapp_carrier_list_failed", extra={"event": "webapp_carrier_list_failed"})
        return None
    open_sessions = [s for s in sessions if not s.get("closed_at")]
    if not open_sessions:
        return None
    open_ids = {s["session_id"] for s in open_sessions}
    if pool is not None and user_id:
        async with pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, user_id)
        if cfg and cfg.default_session_id in open_ids:
            return cfg.default_session_id
    open_sessions.sort(key=lambda s: s.get("opened_at") or "", reverse=True)
    return open_sessions[0]["session_id"]


async def call_agent_json(
    request: Request, *, dest: str, mode: str, payload: Any
) -> dict[str, Any] | None:
    """Dispatch a one-shot management task to `dest` and parse its JSON
    `AgentOutput`. Returns None when there's no channel core or no open
    carrier session (the page renders an empty state), or on a failed/empty
    result. Never raises on a malformed agent response."""
    core = request.app.state.core
    user_id = session_user_id(request)
    if core is None or not user_id:
        return None
    session_id = await carrier_session(request)
    if session_id is None:
        return None
    try:
        result = await core.call_agent(
            user_id=user_id, session_id=session_id, dest=dest, mode=mode,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — surface as an empty result to the page
        logger.warning(
            "webapp_call_agent_failed",
            extra={"event": "webapp_call_agent_failed", "dest": dest, "mode": mode},
        )
        return None
    content = (result.output.content if result.output else "") or ""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
