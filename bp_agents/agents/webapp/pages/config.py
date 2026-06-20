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
from bp_agents.agents.webapp.upstream import UpstreamError
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

# Chat platforms a web user can connect for password recovery / notifications.
# `telegram` is called out specially because it's the only channel that
# delivers scheduled-task notifications out-of-band ([cron.md] §6).
_LINKABLE_PLATFORMS = ("telegram", "kakao")


async def _linked_platforms(pool: object, user_id: str) -> set[str]:
    """The chat platforms this account already has a mapping for, so the UI
    can tell the user what's connected (and nudge linking what isn't)."""
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        mappings = await queries.list_platform_mappings_for_user(
            conn, user_id=user_id
        )
    return {m.platform for m in mappings}

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
    """Every model tier to render — name, label, current value, allowed
    options, and `editable`. A tier with a non-empty allow-list renders as an
    editable <select>; a tier with none still shows its CURRENT model as
    read-only text (so the user always sees which model each tier uses — only
    changing it is gated)."""
    out: list[dict[str, object]] = []
    for field in PRESET_FIELDS:
        choices = preset_choices.get(field) or []
        out.append({
            "name": field,
            "label": _PRESET_LABELS.get(field, field),
            "current": getattr(cfg, field, None) if cfg else None,
            "choices": choices,
            "editable": bool(choices),
        })
    return out


# Messages for a failed `POST /change-password` (auth_pages), keyed by the
# code it puts on the redirect — never reflect the router's raw error text.
_PW_ERRORS = {
    "mismatch": "The new password and its confirmation don’t match.",
    "weak": "Choose a new password of at least 8 characters.",
    "current": "Your current password is incorrect.",
    "same": "The new password must differ from your current one.",
    "conflict": "This account can’t use a password login.",
    "ratelimited": "Too many attempts — please wait a moment and try again.",
    "failed": "Couldn’t change your password. Please try again.",
}


@router.get("/config", response_class=HTMLResponse)
async def config_view(
    request: Request, saved: int = 0, pw_error: str | None = None
) -> HTMLResponse:
    pool = request.app.state.pool
    user_id = session_user_id(request)
    if pool is None or not user_id:
        raise HTTPException(status_code=404)
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, user_id)
    preset_choices = _preset_choices(request)
    linked = await _linked_platforms(pool, user_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "config/form.html",
        {"cfg": cfg, "saved": bool(saved), "error": None,
         "active_section": "config",
         "pw_error": _PW_ERRORS.get(pw_error or ""),
         "preset_fields": _preset_fields_for_template(cfg, preset_choices),
         "linkable_platforms": _LINKABLE_PLATFORMS,
         "linked_platforms": linked,
         "link_token": None},
    )


@router.post("/link-token", response_class=HTMLResponse)
async def mint_link_token(request: Request) -> HTMLResponse:
    """Mint a single-use channel-link token for the logged-in user and show
    it once (re-renders Settings). The user pastes `/link <token>` into the
    Telegram/Kakao bot to connect that chat to this account. Rendered in the
    response body — never on the URL — so it can't leak via history/referer."""
    pool = request.app.state.pool
    user_id = session_user_id(request)
    access = request.session.get("access_token")
    if pool is None or not user_id or not access:
        raise HTTPException(status_code=404)
    preset_choices = _preset_choices(request)
    link_token: str | None = None
    error: str | None = None
    try:
        body = await request.app.state.upstream.mint_link_token(access_token=access)
        link_token = body["link_token"]
    except UpstreamError as exc:
        error = (
            "Too many attempts — please wait a moment and try again."
            if exc.status_code == 429
            else "Couldn’t create a link token. Please try again."
        )
        logger.info(
            "webapp_mint_link_token_failed",
            extra={"event": "webapp_mint_link_token_failed",
                   "status_code": exc.status_code},
        )
    async with pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, user_id)
    linked = await _linked_platforms(pool, user_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "config/form.html",
        {"cfg": cfg, "saved": False, "error": None,
         "active_section": "config", "pw_error": None,
         "preset_fields": _preset_fields_for_template(cfg, preset_choices),
         "linkable_platforms": _LINKABLE_PLATFORMS,
         "linked_platforms": linked,
         "link_token": link_token,
         "link_error": error},
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
        linked = await _linked_platforms(pool, user_id)
        return request.app.state.templates.TemplateResponse(
            request,
            "config/form.html",
            {"cfg": cfg, "saved": False, "error": "; ".join(errors),
             "active_section": "config", "pw_error": None,
             "preset_fields": _preset_fields_for_template(cfg, preset_choices),
             "linkable_platforms": _LINKABLE_PLATFORMS,
             "linked_platforms": linked,
             "link_token": None},
            status_code=400,
        )

    if updates:
        async with pool.acquire() as conn:
            await queries.update_user_config(conn, user_id, **updates)
    return RedirectResponse(url="/config?saved=1", status_code=303)
