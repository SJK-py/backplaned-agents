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


@router.get("/metrics-cards", response_class=HTMLResponse)
async def metrics_cards(request: Request) -> HTMLResponse:
    """HTMX partial: router metric cards. The dashboard auto-refreshes this
    every 30s, so the initial page load isn't blocked on the metrics fetch."""
    up = upstream(request)
    token = access_token(request)
    summary = await _fetch_safe(up, "GET", "/metrics/summary", access_token=token)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_partials/metrics_cards.html",
        {
            "metric_cards": _metric_cards(summary),
            "metrics_failed": summary is None,
        },
    )


def _human(n: float) -> str:
    """Compact integer formatting: 1.2k / 3.4M."""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _top_breakdown(d: dict[str, float] | None, limit: int = 3) -> str | None:
    """Render the top-N label:count pairs, e.g. 'stop 12, error 1'."""
    if not d:
        return None
    items = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return ", ".join(f"{k or '—'} {int(v)}" for k, v in items) or None


def _metric_cards(summary: dict | None) -> list[dict[str, Any]]:
    """Build dashboard cards from the router metrics summary. `tone='alert'`
    flags non-zero error/failure counts (and a down Redis) for the template."""
    if not summary:
        return []
    llm = summary.get("llm", {}) or {}
    tasks = summary.get("tasks", {}) or {}
    infra = summary.get("infra", {}) or {}

    errors = int(llm.get("errors_total", 0) or 0)
    exhausted = int(llm.get("fallback_chain_exhausted_total", 0) or 0)
    calls = int(llm.get("calls_total", 0) or 0)
    tokens_in = int(llm.get("tokens_in", 0) or 0)
    tokens_out = int(llm.get("tokens_out", 0) or 0)
    cost_usd = (llm.get("cost_microusd", 0) or 0) / 1_000_000
    redis = infra.get("redis_health")

    return [
        {
            "label": "LLM upstream errors",
            "value": errors,
            "sub": _top_breakdown(llm.get("errors_by_code")),
            "tone": "alert" if errors else None,
        },
        {
            "label": "Failed (chain exhausted)",
            "value": exhausted,
            "sub": f"{int(llm.get('fallback_used_total', 0) or 0)} saved by fallback",
            "tone": "alert" if exhausted else None,
        },
        {
            "label": "LLM calls (ok)",
            "value": _human(calls),
            "sub": _top_breakdown(llm.get("calls_by_provider")),
            "tone": None,
        },
        {
            "label": "Tokens",
            "value": _human(tokens_in + tokens_out),
            "sub": f"{_human(tokens_in)} in / {_human(tokens_out)} out",
            "tone": None,
        },
        {
            "label": "LLM cost",
            "value": f"${cost_usd:,.2f}",
            "sub": "since router start",
            "tone": None,
        },
        {
            "label": "Active tasks",
            "value": int(tasks.get("active", 0) or 0),
            "sub": _top_breakdown(tasks.get("active_by_state")),
            "tone": None,
        },
        {
            "label": "Redis",
            "value": ("OK" if redis == 1 else "DOWN" if redis == 0 else "—"),
            "sub": None,
            "tone": "alert" if redis == 0 else None,
        },
    ]


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
