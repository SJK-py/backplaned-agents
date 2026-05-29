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
from bp_agents.config_edit import (
    PRESET_FIELDS,
    ConfigError,
    coerce_config_value,
    editable_fields,
    preset_choices_from_settings,
)
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()

# Friendly labels for the opt-in LLM-tier <select>s.
_PRESET_LABELS = {
    "preset_pro": "Model — deep reasoning (pro)",
    "preset_balanced": "Model — assistant & research (balanced)",
    "preset_lite": "Model — quick helpers (lite)",
}


def _preset_choices(request: Request) -> dict[str, list[str]]:
    return preset_choices_from_settings(request.app.state.suite_settings)


def _preset_fields_for_template(
    cfg: object, preset_choices: dict[str, list[str]]
) -> list[dict[str, object]]:
    """The opted-in preset tiers to render as <select>s — name, label,
    current value, and the allowed options. Empty tiers are omitted."""
    out: list[dict[str, object]] = []
    for field in PRESET_FIELDS:
        choices = preset_choices.get(field) or []
        if not choices:
            continue
        out.append({
            "name": field,
            "label": _PRESET_LABELS.get(field, field),
            "current": getattr(cfg, field, None) if cfg else None,
            "choices": choices,
        })
    return out


@router.get("/config", response_class=HTMLResponse)
async def config_view(request: Request, saved: int = 0) -> HTMLResponse:
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        raise HTTPException(status_code=404)
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, user_id)
    preset_choices = _preset_choices(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "config/form.html",
        {"cfg": cfg, "saved": bool(saved), "error": None,
         "active_section": "config",
         "preset_fields": _preset_fields_for_template(cfg, preset_choices)},
    )


@router.post("/config", response_class=HTMLResponse)
async def config_save(request: Request) -> HTMLResponse:
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        raise HTTPException(status_code=404)
    form = await request.form()
    preset_choices = _preset_choices(request)

    updates: dict[str, object] = {}
    errors: list[str] = []
    for field, typ in editable_fields(preset_choices).items():
        if typ is bool:
            raw = "true" if field in form else "false"  # checkbox semantics
        elif field not in form:
            continue
        else:
            raw = form[field]
        try:
            updates[field] = coerce_config_value(
                field, raw, preset_choices=preset_choices
            )
        except ConfigError as exc:
            errors.append(str(exc))

    if errors:
        async with pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, user_id)
        return request.app.state.templates.TemplateResponse(
            request,
            "config/form.html",
            {"cfg": cfg, "saved": False, "error": "; ".join(errors),
             "active_section": "config",
             "preset_fields": _preset_fields_for_template(cfg, preset_choices)},
            status_code=400,
        )

    if updates:
        async with pool.acquire() as conn:
            await queries.update_user_config(conn, user_id, **updates)
    return RedirectResponse(url="/config?saved=1", status_code=303)
