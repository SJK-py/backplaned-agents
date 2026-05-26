"""bp_admin.pages.audit — Audit log viewer (phase 7).

Mounts under `/admin/audit`. Wraps:
  - GET /v1/admin/audit?since=&until=&event=&actor_id=&limit=

Filter state lives in the URL (`hx-push-url=true` on the form) so any
view is bookmarkable and shareable. Detail pages deep-link in via
`?actor_id=<uuid>`.

The upstream API has no offset/cursor — only `limit` (max 1000). The
"limit" select is exposed in the filter bar; expanding history requires
a separate API extension.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from bp_admin._helpers import access_token, error_response, is_htmx, upstream
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


LIMIT_OPTIONS = [50, 100, 250, 500, 1000]


@router.get("", response_class=HTMLResponse)
async def list_audit(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    event: str | None = None,
    actor_id: str | None = None,
    limit: int = 100,
) -> HTMLResponse:
    if limit < 1 or limit > 1000:
        limit = 100

    params: dict[str, str | int] = {"limit": limit}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if event:
        params["event"] = event.strip()
    if actor_id:
        params["actor_id"] = actor_id.strip()

    try:
        rows = await upstream(request).admin_request(
            "GET",
            "/audit",
            access_token=access_token(request),
            params=params,
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=is_htmx(request), active_section="audit")

    # Pre-render JSON payloads once for the template — keeps the Jinja
    # tidy and ensures consistent indentation.
    for r in rows:
        payload = r.get("payload")
        if payload is None:
            r["payload_json"] = None
        else:
            try:
                r["payload_json"] = json.dumps(payload, indent=2, sort_keys=True, default=str)
            except (TypeError, ValueError):
                r["payload_json"] = str(payload)

    templates = request.app.state.templates
    template = (
        "audit/_table_body.html" if is_htmx(request) else "audit/list.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_section": "audit",
            "rows": rows,
            "filters": {
                "since": since or "",
                "until": until or "",
                "event": event or "",
                "actor_id": actor_id or "",
                "limit": limit,
            },
            "limit_options": LIMIT_OPTIONS,
        },
    )
