"""bp_agents.agents.webapp.pages.memory — the Memory page.

Browses the per-user fact graph by dispatching to the memory agent's
`tool:false` list / delete / manual_add modes; the agent returns JSON in its
`AgentOutput`, which we render. Memory is per-user, so the dispatch rides any
open session ([webapp.md] §4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from bp_agents.agents.webapp.pages._common import call_agent_json
from bp_agents.common.payloads import MAX_PAGE, MemDelete, MemList, MemManualAdd

logger = logging.getLogger(__name__)
router = APIRouter()

_MEMORY = "memory"
_KINDS = ["preference", "personal_info", "event", "project"]


async def _list_ctx(
    request: Request, *, query: str, kind: str, page: int
) -> dict:
    start = page * MAX_PAGE
    data = await call_agent_json(
        request, dest=_MEMORY, mode="list",
        payload=MemList(
            query=query or None, kind=kind or None,
            start=start, end=start + MAX_PAGE,
        ),
    )
    items = data.get("items", []) if data else []
    total = data.get("total", 0) if data else 0
    return {
        "items": items, "total": total, "page": page, "page_size": MAX_PAGE,
        "has_prev": page > 0, "has_next": (page + 1) * MAX_PAGE < total,
        "query": query, "kind": kind, "kinds": _KINDS,
        "queried": bool(query.strip()),
        "unavailable": data is None,  # no open session / dispatch failed
    }


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(
    request: Request, query: str = "", kind: str = "", page: int = 0
) -> HTMLResponse:
    ctx = await _list_ctx(request, query=query, kind=kind, page=max(0, page))
    return request.app.state.templates.TemplateResponse(
        request, "memory/list.html", {**ctx, "active_section": "memory"},
    )


@router.get("/memory/list", response_class=HTMLResponse)
async def memory_list(
    request: Request, query: str = "", kind: str = "", page: int = 0
) -> HTMLResponse:
    ctx = await _list_ctx(request, query=query, kind=kind, page=max(0, page))
    return request.app.state.templates.TemplateResponse(
        request, "memory/_items.html", ctx,
    )


@router.post("/memory/delete")
async def memory_delete(request: Request, uid: str = Form(...)) -> Response:
    data = await call_agent_json(
        request, dest=_MEMORY, mode="delete", payload=MemDelete(uid=uid),
    )
    if data is None:
        raise HTTPException(status_code=502)
    return Response(status_code=204, headers={"HX-Trigger": "memoryChanged"})


@router.post("/memory/add")
async def memory_add(
    request: Request, fact: str = Form(...), kind: str = Form("personal_info")
) -> Response:
    if not fact.strip():
        raise HTTPException(status_code=400, detail="empty fact")
    k = kind if kind in _KINDS else "personal_info"
    data = await call_agent_json(
        request, dest=_MEMORY, mode="manual_add",
        payload=MemManualAdd(fact=fact.strip(), kind=k),
    )
    if data is None:
        raise HTTPException(status_code=502)
    return Response(status_code=204, headers={"HX-Trigger": "memoryChanged"})
