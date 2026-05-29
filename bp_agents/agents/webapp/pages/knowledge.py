"""bp_agents.agents.webapp.pages.knowledge — the Knowledge base page.

Lists the per-user document set via the knowledge_base agent's `tool:false`
browse / delete modes (JSON in `AgentOutput`). Per-user, so it rides any open
session ([webapp.md] §4). Documents are added through chat / file upload, not
here — this page browses and removes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from bp_agents.agents.webapp.pages._common import call_agent_json
from bp_agents.common.payloads import MAX_PAGE, KbBrowse, KbDelete

logger = logging.getLogger(__name__)
router = APIRouter()

_KB = "knowledge_base"


async def _list_ctx(
    request: Request, *, query: str, collection: str, tag: str, page: int
) -> dict:
    start = page * MAX_PAGE
    data = await call_agent_json(
        request, dest=_KB, mode="browse",
        payload=KbBrowse(
            query=query or None, collection=collection or None, tag=tag or None,
            start=start, end=start + MAX_PAGE,
        ),
    )
    items = data.get("items", []) if data else []
    total = data.get("total", 0) if data else 0
    return {
        "items": items, "total": total, "page": page, "page_size": MAX_PAGE,
        "has_prev": page > 0, "has_next": (page + 1) * MAX_PAGE < total,
        "query": query, "collection": collection, "tag": tag,
        "unavailable": data is None,
    }


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(
    request: Request, query: str = "", collection: str = "", tag: str = "",
    page: int = 0,
) -> HTMLResponse:
    ctx = await _list_ctx(
        request, query=query, collection=collection, tag=tag, page=max(0, page)
    )
    return request.app.state.templates.TemplateResponse(
        request, "knowledge/list.html", {**ctx, "active_section": "knowledge"},
    )


@router.get("/knowledge/list", response_class=HTMLResponse)
async def knowledge_list(
    request: Request, query: str = "", collection: str = "", tag: str = "",
    page: int = 0,
) -> HTMLResponse:
    ctx = await _list_ctx(
        request, query=query, collection=collection, tag=tag, page=max(0, page)
    )
    return request.app.state.templates.TemplateResponse(
        request, "knowledge/_items.html", ctx,
    )


@router.post("/knowledge/delete")
async def knowledge_delete(
    request: Request, title: str = Form(...), collection: str = Form("")
) -> Response:
    data = await call_agent_json(
        request, dest=_KB, mode="delete",
        payload=KbDelete(title=title, collection=collection or None),
    )
    if data is None:
        raise HTTPException(status_code=502)
    return Response(status_code=204, headers={"HX-Trigger": "knowledgeChanged"})
