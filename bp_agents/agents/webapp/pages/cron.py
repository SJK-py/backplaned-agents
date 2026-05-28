"""bp_agents.agents.webapp.pages.cron — the structured cron pane.

List / add / remove scheduled jobs over `cron_jobs` ([webapp.md] §5),
reusing the SAME validated helpers as the config agent's cron toolset
(`bp_agents.cron_manage`). Per-session because a new job binds to a
session_id.

Delivery caveat ([webapp.md] §6, Decision 3): the webapp can *manage*
jobs, but firing a job back to a webapp session needs the deferred
channel-agnostic cron routing. The template notes this; Telegram-session
jobs fire normally.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.pages._common import owned_session
from bp_agents.cron_manage import (
    REPORT_ALWAYS,
    REPORT_CBC,
    REPORT_NEVER,
    CronError,
    add_cron,
    remove_cron,
)
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()

_REPORTS = [REPORT_CBC, REPORT_ALWAYS, REPORT_NEVER]


async def _render(request: Request, session_id: str, *, error: str | None = None,
                  status_code: int = 200) -> HTMLResponse:
    pool = request.app.state.pool
    user_id = session_user_id(request)
    async with pool.acquire() as conn:
        jobs = await queries.list_cron_jobs(conn, user_id=user_id)
        cfg = await queries.get_user_config(conn, user_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "cron/list.html",
        {
            "session_id": session_id,
            "jobs": jobs,
            "default_tz": cfg.timezone if cfg else "UTC",
            "reports": _REPORTS,
            "error": error,
            "active_section": "sessions",
        },
        status_code=status_code,
    )


@router.get("/cron/{session_id}", response_class=HTMLResponse)
async def cron_view(session_id: str, request: Request) -> HTMLResponse:
    if await owned_session(request, session_id) is None:
        raise HTTPException(status_code=404)
    return await _render(request, session_id)


@router.post("/cron/{session_id}")
async def cron_add(
    session_id: str,
    request: Request,
    cron_expression: str = Form(...),
    cron_message: str = Form(...),
    timezone: str = Form("UTC"),
    report: str = Form(REPORT_CBC),
) -> Response:
    if await owned_session(request, session_id) is None:
        raise HTTPException(status_code=404)
    try:
        await add_cron(
            request.app.state.pool,
            user_id=session_user_id(request),
            session_id=session_id,
            cron_expression=cron_expression.strip(),
            cron_message=cron_message.strip(),
            timezone=timezone.strip() or "UTC",
            report=report if report in _REPORTS else REPORT_CBC,
        )
    except CronError as exc:
        return await _render(request, session_id, error=str(exc), status_code=400)
    return RedirectResponse(url=f"/cron/{session_id}", status_code=303)


@router.post("/cron/{session_id}/remove")
async def cron_remove(
    session_id: str, request: Request, cron_id: str = Form(...)
) -> RedirectResponse:
    if await owned_session(request, session_id) is None:
        raise HTTPException(status_code=404)
    await remove_cron(
        request.app.state.pool, user_id=session_user_id(request), cron_id=cron_id
    )
    return RedirectResponse(url=f"/cron/{session_id}", status_code=303)
