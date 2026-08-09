"""bp_router.api.llm — user-facing LLM preset discovery and preferences.

Implements `docs/design/router-resolved-preset-slots.md` §5. Two endpoints,
both under a **session JWT** (the user's own authority over their own
choice), which together replace the suite's static
`selectable_presets_*` allow-list:

  * `GET /v1/llm/presets` — the presets **this caller's level satisfies**.
    The menu becomes correct by construction: it filters with the same
    `user_level_satisfies` the call path gates on, so a user is never
    offered a model they cannot run. The suite previously hand-maintained
    a global list that could not be right for a tier1 and a tier3 user in
    the same deployment.
  * `GET | PUT /v1/llm/preferences` — the caller's slot → preset map.
    `PUT` re-checks the gate and refuses with `preset_not_allowed`, moving
    the refusal from mid-conversation (where it is not retriable and the
    user has already sent a message) to selection time.

This is a deliberately thin projection: name, description, and which slots
a preset is the default for. Never `api_key`, `api_key_ref`, or `base_url`
— those stay in the admin view (`GET /v1/admin/llm/presets`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bp_router.db import queries
from bp_router.principals import user_level_satisfies
from bp_router.security.jwt import SessionPrincipal, require_authenticated

router = APIRouter()


class PresetChoice(BaseModel):
    """One preset a caller may actually use."""

    name: str
    description: str | None = None
    # Slots this preset is the operator's default for — lets a UI show
    # "(default)" without a second call.
    default_for: list[str] = Field(default_factory=list)


class PreferenceView(BaseModel):
    slot: str
    preset_name: str


class SetPreferenceRequest(BaseModel):
    slot: str
    # Null clears the preference, returning the slot to the operator default.
    preset_name: str | None = None


async def _caller_level(state: Any, user_id: str) -> str | None:
    async with state.db_pool.acquire() as conn:
        return await state.llm_service.resolve_user_level(conn, user_id)


@router.get("/presets", response_model=list[PresetChoice])
async def list_available_presets(
    request: Request,
    slot: str | None = None,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[PresetChoice]:
    """Presets this caller's level satisfies, optionally narrowed to those
    sensible for one slot.

    Filtering here — rather than in a suite-side allow-list — is the whole
    point: entitlement IS the allow-list, so the menu cannot drift from the
    gate."""
    state = request.app.state.bp
    level = await _caller_level(state, principal.user_id)
    defaults: dict[str, str] = getattr(state.settings, "llm_default_presets", {}) or {}
    if slot is not None and slot not in defaults:
        raise HTTPException(status_code=404, detail="unknown slot")

    default_for: dict[str, list[str]] = {}
    for slot_name, preset_name in defaults.items():
        default_for.setdefault(preset_name, []).append(slot_name)

    out: list[PresetChoice] = []
    for name, preset in sorted(state.llm_service.presets.items()):
        if not user_level_satisfies(level, preset.min_user_level):
            continue
        out.append(
            PresetChoice(
                name=name,
                description=preset.description,
                default_for=sorted(default_for.get(name, [])),
            )
        )
    return out


@router.get("/preferences", response_model=list[PreferenceView])
async def get_preferences(
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[PreferenceView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        prefs = await queries.get_user_llm_preferences(conn, principal.user_id)
    return [
        PreferenceView(slot=slot, preset_name=name)
        for slot, name in sorted(prefs.items())
    ]


@router.put("/preferences", response_model=list[PreferenceView])
async def set_preference(
    req: SetPreferenceRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> list[PreferenceView]:
    """Set (or clear) one slot preference, gate-checked NOW.

    The refusal a user gets here is the one that used to arrive on their
    next turn as an un-retriable LLM error, after they had already sent a
    message."""
    state = request.app.state.bp
    defaults: dict[str, str] = getattr(state.settings, "llm_default_presets", {}) or {}
    if req.slot not in defaults:
        raise HTTPException(status_code=404, detail="unknown slot")

    if req.preset_name is not None:
        preset = state.llm_service.get_preset(req.preset_name)
        if preset is None:
            raise HTTPException(status_code=404, detail="unknown preset")
        level = await _caller_level(state, principal.user_id)
        if not user_level_satisfies(level, preset.min_user_level):
            # Tell them what it needs: the point of refusing here is that
            # the user can act on it.
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "preset_not_allowed",
                    "preset": req.preset_name,
                    "required_level": preset.min_user_level,
                },
            )

    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            await queries.set_user_llm_preference(
                conn, principal.user_id, req.slot, req.preset_name
            )
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="llm.preference_set",
                target_kind="user",
                target_id=principal.user_id,
                payload={"slot": req.slot, "preset": req.preset_name},
            )
        prefs = await queries.get_user_llm_preferences(conn, principal.user_id)

    # The resolution path caches preferences beside the user's level; a
    # write that didn't invalidate would leave the old model running for up
    # to the cache TTL.
    state.llm_service.invalidate_user_level(principal.user_id)
    return [
        PreferenceView(slot=slot, preset_name=name)
        for slot, name in sorted(prefs.items())
    ]
