"""Shared page helpers for the webapp."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.upstream import UpstreamError
from bp_agents.db import queries

logger = logging.getLogger(__name__)

# `metadata.kind` values the chatbot sets for chat-origin sessions — flagged
# in the UI (list badge + a one-time note on open) so the user knows progress
# won't mirror back to the chat if continued here. A chat-origin session is
# retired only from the chatbot (`/new`, which deletes the key); the web app
# must NOT close/remove it while it is still set, or it would yank the
# cron-fallback `default_session_id` out from under the chat.
#
# These live in the ROUTER's session metadata — the same field its
# serviced-session discovery reads — rather than in a suite table shadowing
# the session row.
TELEGRAM_CHANNEL = "chatbot_telegram"
KAKAO_CHANNEL = "chatbot_kakao"
WEBAPP_CHANNEL = "webapp"
# Channels owned by a chatbot gateway — protected from web-app close/remove.
CHATBOT_CHANNELS = frozenset({TELEGRAM_CHANNEL, KAKAO_CHANNEL})


@dataclass
class SessionView:
    """One session as the webapp sees it: the router's row, with the suite's
    reading of its metadata."""

    session_id: str
    opened_at: str | None = None
    closed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SessionView:
        return cls(
            session_id=row["session_id"],
            opened_at=row.get("opened_at"),
            closed_at=row.get("closed_at"),
            metadata=row.get("metadata") or {},
        )

    @property
    def channel(self) -> str | None:
        return self.metadata.get("kind")

    @property
    def chat_id(self) -> str | None:
        return self.metadata.get("external_id")

    @property
    def title(self) -> str | None:
        return self.metadata.get("title")

    @property
    def closed(self) -> bool:
        return bool(self.closed_at)


async def ensure_user_config(request: Request) -> None:
    """Idempotently create the logged-in user's suite-side `user_config` row
    — mirrors the chatbot approval reconcile (`create_user_config`).

    Chat users get this row when the approval poller reconciles their serviced
    session; web-first and OIDC accounts never go through that path, so without
    this their `user_config` is missing — `get_user_config` returns None (the
    config agent reads nothing) and `update_user_config` silently updates zero
    rows (the webapp reports 'saved' but the value is unchanged). Idempotent
    (`INSERT ... ON CONFLICT DO NOTHING`), so it's safe to call on every login
    and on the config pages."""
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        return
    async with pool.acquire() as conn:
        await queries.create_user_config(conn, user_id=user_id)


async def owned_session(request: Request, session_id: str) -> SessionView | None:
    """The user's session row for `session_id`, or None (→ 404).

    Ownership is the ROUTER's answer now, not a suite table's: the endpoint
    404s a session that isn't the caller's, which is both the check and the
    non-enumerable posture we want. The router's `admit_task` / file-scope
    check remains the ultimate gate; this is the UX guard."""
    upstream = request.app.state.upstream
    access = request.session.get("access_token")
    if upstream is None or not access:
        return None
    try:
        row = await upstream.get_session(
            access_token=access, session_id=session_id
        )
    except UpstreamError:
        return None
    return SessionView.from_row(row) if row else None


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
