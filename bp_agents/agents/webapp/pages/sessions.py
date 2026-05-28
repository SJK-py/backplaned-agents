"""bp_agents.agents.webapp.pages.sessions — session list + lifecycle.

The authoritative session list comes from the router (`GET /v1/sessions`,
user token); the channel badge + delegation status are enriched from the
suite's `session_info` ([webapp.md] §4). New opens a router session +
`session_info`; close archives it; remove hard-deletes via the router
purge AND reclaims the suite-side rows the purge doesn't reach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.pages._common import TELEGRAM_CHANNEL, owned_session
from bp_agents.agents.webapp.upstream import UpstreamError
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()

WEBAPP_CHANNEL = "webapp"


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


@router.post("/sessions")
async def new_session(request: Request) -> Response:
    """Open a router session (user token) + its suite `session_info`, then
    land the user in the new chat. Does NOT touch `default_session_id` —
    that's the chatbot's inbound-routing target, not the webapp's."""
    upstream = request.app.state.upstream
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        raise HTTPException(status_code=404)
    access = request.session["access_token"]
    try:
        view = await upstream.create_session(access_token=access)
    except UpstreamError as exc:
        logger.warning(
            "webapp_session_new_failed",
            extra={"event": "webapp_session_new_failed", "status_code": exc.status_code},
        )
        raise HTTPException(status_code=502) from exc
    session_id = view["session_id"]
    async with pool.acquire() as conn:
        await queries.create_session_info(
            conn, session_id=session_id, user_id=user_id, channel=WEBAPP_CHANNEL,
        )
    return Response(status_code=204, headers={"HX-Redirect": f"/chat/{session_id}"})


@router.post("/sessions/{session_id}/close")
async def close_session(session_id: str, request: Request) -> Response:
    """Archive the session (router `DELETE`). History/config are kept."""
    if await owned_session(request, session_id) is None:
        raise HTTPException(status_code=404)
    access = request.session["access_token"]
    try:
        await request.app.state.upstream.delete_session(
            access_token=access, session_id=session_id, purge=False
        )
    except UpstreamError as exc:
        logger.warning(
            "webapp_session_close_failed",
            extra={"event": "webapp_session_close_failed", "status_code": exc.status_code},
        )
        raise HTTPException(status_code=502) from exc
    return Response(status_code=204, headers={"HX-Redirect": "/"})


@router.post("/sessions/{session_id}/remove")
async def remove_session(session_id: str, request: Request) -> Response:
    """Hard-delete: router purge (`DELETE …?purge=true`) THEN reclaim the
    suite rows the purge can't reach ([webapp.md] §4). Irreversible — the UI
    confirms first."""
    if await owned_session(request, session_id) is None:
        raise HTTPException(status_code=404)
    pool = request.app.state.pool
    access = request.session["access_token"]
    try:
        await request.app.state.upstream.delete_session(
            access_token=access, session_id=session_id, purge=True
        )
    except UpstreamError as exc:
        logger.warning(
            "webapp_session_remove_failed",
            extra={"event": "webapp_session_remove_failed", "status_code": exc.status_code},
        )
        raise HTTPException(status_code=502) from exc
    # Suite-side cleanup — atomic, after the router purge ([webapp.md] §9).
    async with pool.acquire() as conn, conn.transaction():
        await queries.purge_session_suite_data(conn, session_id)
    return Response(status_code=204, headers={"HX-Redirect": "/"})
