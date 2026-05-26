"""bp_admin.pages.dashboard — landing page after login.

Real counts wired up. Best-effort concurrent fetches of users,
agents, ACL rules, invitations, and a slice of the audit log; one
failed call falls back to "—" for that card without breaking the page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from bp_admin._helpers import access_token, upstream
from bp_admin.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


# How many recent audit events to show in the dashboard feed.
AUDIT_FEED_LIMIT = 10


async def _fetch_safe(
    upstream: UpstreamClient,
    method: str,
    path: str,
    *,
    access_token: str,
    params: dict | None = None,
) -> Any | None:
    """Wrap a single admin fetch so one failure doesn't sink the page."""
    try:
        return await upstream.admin_request(
            method, path, access_token=access_token, params=params
        )
    except UpstreamError as exc:
        logger.warning(
            "dashboard_fetch_failed",
            extra={
                "event": "dashboard_fetch_failed",
                "path": path,
                "status_code": exc.status_code,
            },
        )
        return None


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    up = upstream(request)
    token = access_token(request)

    users, agents, rules, valid_invitations, audit = await asyncio.gather(
        _fetch_safe(up, "GET", "/users", access_token=token, params={"limit": 1000}),
        _fetch_safe(up, "GET", "/agents", access_token=token),
        _fetch_safe(up, "GET", "/acl/rules", access_token=token),
        _fetch_safe(
            up, "GET", "/invitations",
            access_token=token, params={"status": "valid", "limit": 1000},
        ),
        _fetch_safe(
            up, "GET", "/audit",
            access_token=token, params={"limit": AUDIT_FEED_LIMIT},
        ),
    )

    cards = [
        {
            "label": "Users",
            "value": _len_or_dash(users),
            "href": "/admin/users",
            "sub": _users_subtext(users),
        },
        {
            "label": "Active agents",
            "value": _count_or_dash(agents, lambda a: a.get("status") == "active"),
            "href": "/admin/agents?status=active",
            "sub": _agents_subtext(agents),
        },
        {
            "label": "ACL rules",
            "value": _len_or_dash(rules),
            "href": "/admin/acl/rules",
            "sub": None,
        },
        {
            "label": "Pending invitations",
            "value": _len_or_dash(valid_invitations),
            "href": "/admin/invitations?status=valid",
            "sub": None,
        },
    ]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_section": "dashboard",
            "cards": cards,
            "audit": audit or [],
            "audit_failed": audit is None,
        },
    )


def _len_or_dash(rows: list | None) -> str:
    return "—" if rows is None else str(len(rows))


def _count_or_dash(rows: list | None, pred) -> str:  # type: ignore[no-untyped-def]
    if rows is None:
        return "—"
    return str(sum(1 for r in rows if pred(r)))


def _users_subtext(rows: list | None) -> str | None:
    if rows is None:
        return None
    suspended = sum(1 for r in rows if r.get("suspended_at"))
    if suspended == 0:
        return None
    return f"{suspended} suspended"


def _agents_subtext(rows: list | None) -> str | None:
    if rows is None:
        return None
    suspended = sum(1 for r in rows if r.get("status") == "suspended")
    removed = sum(1 for r in rows if r.get("status") == "removed")
    bits: list[str] = []
    if suspended:
        bits.append(f"{suspended} suspended")
    if removed:
        bits.append(f"{removed} removed")
    return ", ".join(bits) or None
