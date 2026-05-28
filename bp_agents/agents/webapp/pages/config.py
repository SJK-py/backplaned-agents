"""bp_agents.agents.webapp.pages.config — the structured config form.

A per-user settings form over `user_config` ([webapp.md] §5, Decision 2).
Reads the row directly; writes via `queries.update_user_config` with the
SAME validation the config agent's `set_config` uses
(`bp_agents.config_edit`), so the form and the NL path can't disagree. The
chat pane still handles "change my timezone" conversationally.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.config_edit import EDITABLE_FIELDS, ConfigError, coerce_config_value
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/config", response_class=HTMLResponse)
async def config_view(request: Request, saved: int = 0) -> HTMLResponse:
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        raise HTTPException(status_code=404)
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, user_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "config/form.html",
        {"cfg": cfg, "saved": bool(saved), "error": None, "active_section": "config"},
    )


@router.post("/config", response_class=HTMLResponse)
async def config_save(request: Request) -> HTMLResponse:
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        raise HTTPException(status_code=404)
    form = await request.form()

    updates: dict[str, object] = {}
    errors: list[str] = []
    for field, typ in EDITABLE_FIELDS.items():
        if typ is bool:
            raw = "true" if field in form else "false"  # checkbox semantics
        elif field not in form:
            continue
        else:
            raw = form[field]
        try:
            updates[field] = coerce_config_value(field, raw)
        except ConfigError as exc:
            errors.append(str(exc))

    if errors:
        async with pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, user_id)
        return request.app.state.templates.TemplateResponse(
            request,
            "config/form.html",
            {"cfg": cfg, "saved": False, "error": "; ".join(errors),
             "active_section": "config"},
            status_code=400,
        )

    if updates:
        async with pool.acquire() as conn:
            await queries.update_user_config(conn, user_id, **updates)
    return RedirectResponse(url="/config?saved=1", status_code=303)
