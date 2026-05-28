"""bp_agents.agents.webapp.pages.sessions — read-only session list.

The authoritative session list comes from the router (`GET /v1/sessions`,
user token); the channel badge + delegation status are enriched from the
suite's `session_info` ([webapp.md] §4). Read-only in Phase 2 — new /
close / remove land in a later phase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.upstream import UpstreamError
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()

# session_info.channel value the chatbot writes for Telegram-origin
# sessions — flagged in the UI so the user knows progress won't mirror
# back to Telegram if they continue it here ([webapp.md] §4).
TELEGRAM_CHANNEL = "chatbot_telegram"


@dataclass
class SessionRow:
    session_id: str
    opened_at: object
    closed: bool
    channel: str | None
    delegated_to: str | None

    @property
    def is_telegram(self) -> bool:
        return self.channel == TELEGRAM_CHANNEL


async def _load_rows(request: Request) -> list[SessionRow]:
    upstream = request.app.state.upstream
    pool = request.app.state.pool
    access = request.session["access_token"]

    sessions = await upstream.list_sessions(access_token=access)

    # Enrich each session with its suite-side channel + delegation status.
    info_by_id: dict[str, object] = {}
    user_id = session_user_id(request)
    if pool is not None and user_id:
        async with pool.acquire() as conn:
            for info in await queries.list_session_info_for_user(conn, user_id):
                info_by_id[info.session_id] = info

    rows: list[SessionRow] = []
    for s in sessions:
        sid = s["session_id"]
        info = info_by_id.get(sid)
        rows.append(
            SessionRow(
                session_id=sid,
                opened_at=s.get("opened_at"),
                closed=bool(s.get("closed_at")),
                channel=getattr(info, "channel", None),
                delegated_to=getattr(info, "delegated_to", None),
            )
        )
    return rows


@router.get("/", response_class=HTMLResponse)
async def session_list(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    try:
        rows = await _load_rows(request)
    except UpstreamError as exc:
        logger.warning(
            "webapp_session_list_failed",
            extra={"event": "webapp_session_list_failed",
                   "status_code": exc.status_code},
        )
        return templates.TemplateResponse(
            request,
            "sessions/list.html",
            {"rows": [], "error": "Couldn't load your sessions. Please retry.",
             "active_section": "sessions"},
            status_code=502,
        )
    return templates.TemplateResponse(
        request,
        "sessions/list.html",
        {"rows": rows, "error": None, "active_section": "sessions"},
    )
