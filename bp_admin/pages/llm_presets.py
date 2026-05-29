"""bp_admin.pages.llm_presets — LLM preset editor.

Mounts under `/admin/llm/presets`. Wraps:
  - GET    /v1/admin/llm/presets
  - POST   /v1/admin/llm/presets
  - GET    /v1/admin/llm/presets/{name}
  - PATCH  /v1/admin/llm/presets/{name}
  - DELETE /v1/admin/llm/presets/{name}

Presets are bundled (provider, concrete_model, sampling defaults,
provider_options defaults, min_user_level) configurations that
agents reference by name via `ctx.llm.generate(preset="...")`. The
`min_user_level` field gates access using the same grammar as ACL
rules: `*` (any) | `admin` | `service` | `tierN`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from bp_admin._helpers import (
    access_token,
    detail_message,
    error_response,
    pop_flash,
    redirect_with_flash,
    upstream,
)
from bp_admin.upstream import UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter()


# Driven from the router's authoritative list. Keeping a duplicated
# tuple here let the dropdown drift behind any future provider added
# to `SUPPORTED_PROVIDERS`. Listed = ordered for UI consistency.
from bp_router.llm.presets import (  # noqa: E402
    PROVIDERS_REQUIRING_BASE_URL as PROVIDERS_WITH_BASE_URL,  # noqa: F401  # re-export
)
from bp_router.llm.presets import (  # noqa: E402
    SUPPORTED_PROVIDERS,
)

PROVIDERS = list(SUPPORTED_PROVIDERS)
USER_LEVELS = ["*", "admin", "service", "tier0", "tier1", "tier2", "tier3"]


def _form_to_payload(
    *,
    description: str,
    provider: str,
    concrete_model: str,
    api_key_ref: str,
    min_user_level: str,
    default_temperature: str,
    default_max_tokens: str,
    default_provider_options: str,
    api_key: str = "",
    base_url: str = "",
    fallback_preset: str = "",
    max_retries: str = "0",
) -> tuple[dict[str, Any], list[str]]:
    """Translate form fields → JSON payload + a list of validation
    errors. Returns the payload even on partial errors so the form
    can repopulate.

    Notes on the new optional fields:
      - `api_key`: a non-empty value sets the inline secret.  An
        empty string is dropped (no change). To CLEAR an existing
        inline key the caller pairs that with `clear_api_key=True`,
        which is handled by the route handler, not here.
      - `fallback_preset`: empty string = no fallback. The route
        handler emits the appropriate `null`/empty payload value.
      - `max_retries`: validated 0..10. Default 0.
    """
    errors: list[str] = []
    payload: dict[str, Any] = {
        "provider": provider.strip(),
        "concrete_model": concrete_model.strip(),
        "api_key_ref": api_key_ref.strip(),
        "min_user_level": min_user_level.strip() or "*",
    }
    if description.strip():
        payload["description"] = description.strip()

    if api_key.strip():
        payload["api_key"] = api_key  # admin chose to set/replace

    # base_url: required for openai-compatible*, ignored for hosted.
    # Always send the value (possibly empty) so PATCH can clear an
    # obsolete URL when switching providers.
    payload["base_url"] = base_url.strip()

    # Range bounds come from `bp_router.llm.presets` so the form
    # helper and the API can't drift on boundary values. Error
    # messages stay user-friendly here; the API surfaces its own.
    from bp_router.llm.presets import (  # noqa: PLC0415
        MAX_RETRIES_MAX,
        MAX_RETRIES_MIN,
        TEMPERATURE_MAX,
        TEMPERATURE_MIN,
        max_retries_in_range,
        max_tokens_in_range,
        temperature_in_range,
    )

    if default_temperature.strip():
        try:
            t = float(default_temperature)
            if not temperature_in_range(t):
                # `{:g}` strips the trailing `.0` from float-valued
                # bounds (0.0 → "0", 2.0 → "2"). Pre-fix the
                # message read "between 0.0 and 2.0" which looked
                # awkward — the test pinned the cleaner "between 0
                # and 2" shape but the constants were promoted to
                # floats and the message format drifted. Keep `:g`
                # so fractional bounds (e.g. 0.5) still render
                # naturally if the constants change again.
                errors.append(
                    f"Temperature must be between {TEMPERATURE_MIN:g} and "
                    f"{TEMPERATURE_MAX:g}."
                )
            else:
                payload["default_temperature"] = t
        except ValueError:
            errors.append("Temperature must be a number.")
    if default_max_tokens.strip():
        try:
            n = int(default_max_tokens)
            if not max_tokens_in_range(n):
                errors.append("Max tokens must be a positive integer.")
            else:
                payload["default_max_tokens"] = n
        except ValueError:
            errors.append("Max tokens must be an integer.")

    opts_raw = default_provider_options.strip()
    if opts_raw:
        try:
            obj = json.loads(opts_raw)
            if not isinstance(obj, dict):
                errors.append("Default provider_options must be a JSON object.")
            else:
                payload["default_provider_options"] = obj
        except json.JSONDecodeError as exc:
            errors.append(f"Default provider_options is not valid JSON: {exc}")

    fallback = fallback_preset.strip()
    payload["fallback_preset"] = fallback  # "" → backend treats as null

    retries_raw = max_retries.strip() or "0"
    try:
        r = int(retries_raw)
        if not max_retries_in_range(r):
            errors.append(
                f"Max retries must be between {MAX_RETRIES_MIN} and "
                f"{MAX_RETRIES_MAX}."
            )
        else:
            payload["max_retries"] = r
    except ValueError:
        errors.append("Max retries must be an integer.")

    return payload, errors


def _form_for_create() -> dict[str, str]:
    """Initial values for the create form."""
    return {
        "name": "",
        "description": "",
        "provider": "gemini",
        "concrete_model": "",
        "api_key_ref": "env://GEMINI_API_KEY",
        "api_key": "",
        "base_url": "",
        "min_user_level": "*",
        "default_temperature": "",
        "default_max_tokens": "",
        "default_provider_options": "",
        "fallback_preset": "",
        "max_retries": "0",
    }


def _form_from_preset(preset: dict[str, Any]) -> dict[str, str]:
    opts = preset.get("default_provider_options") or {}
    return {
        "name": preset["name"],
        "description": preset.get("description") or "",
        "provider": preset["provider"],
        "concrete_model": preset["concrete_model"],
        "api_key_ref": preset["api_key_ref"],
        # Inline api_key is never returned by the API. The form field
        # always renders blank on edit; "blank = leave unchanged".
        "api_key": "",
        "base_url": preset.get("base_url") or "",
        "min_user_level": preset["min_user_level"],
        "default_temperature": (
            "" if preset.get("default_temperature") is None
            else str(preset["default_temperature"])
        ),
        "default_max_tokens": (
            "" if preset.get("default_max_tokens") is None
            else str(preset["default_max_tokens"])
        ),
        "default_provider_options": (
            json.dumps(opts, indent=2, sort_keys=True) if opts else ""
        ),
        "fallback_preset": preset.get("fallback_preset") or "",
        "max_retries": str(preset.get("max_retries") or 0),
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_presets(request: Request) -> HTMLResponse:
    try:
        presets = await upstream(request).admin_request(
            "GET", "/llm/presets", access_token=access_token(request)
        )
    except UpstreamError as exc:
        return error_response(request, exc, partial=False, active_section="llm_presets")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "llm_presets/list.html",
        {
            "active_section": "llm_presets",
            "presets": presets,
            "flash": pop_flash(request),
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def _existing_preset_names(request: Request) -> list[str]:
    """Fetch the current set of preset names so the fallback dropdown
    can offer them. On upstream failure return an empty list — the
    form still renders, just without fallback options."""
    try:
        rows = await upstream(request).admin_request(
            "GET", "/llm/presets", access_token=access_token(request)
        )
    except UpstreamError:
        return []
    return [row["name"] for row in rows]


@router.get("/new", response_class=HTMLResponse)
async def new_preset_form(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "llm_presets/new.html",
        {
            "active_section": "llm_presets",
            "providers": PROVIDERS,
            "user_levels": USER_LEVELS,
            "fallback_choices": await _existing_preset_names(request),
            "form": _form_for_create(),
            "errors": [],
        },
    )


@router.post("/new", response_class=HTMLResponse)
async def create_preset(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    provider: str = Form(...),
    concrete_model: str = Form(...),
    api_key_ref: str = Form(""),
    api_key: str = Form(""),
    base_url: str = Form(""),
    min_user_level: str = Form("*"),
    default_temperature: str = Form(""),
    default_max_tokens: str = Form(""),
    default_provider_options: str = Form(""),
    fallback_preset: str = Form(""),
    max_retries: str = Form("0"),
) -> Response:
    templates = request.app.state.templates
    form = {
        "name": name,
        "description": description,
        "provider": provider,
        "concrete_model": concrete_model,
        "api_key_ref": api_key_ref,
        # Never echo the secret back to the form on re-render.
        "api_key": "",
        "base_url": base_url,
        "min_user_level": min_user_level,
        "default_temperature": default_temperature,
        "default_max_tokens": default_max_tokens,
        "default_provider_options": default_provider_options,
        "fallback_preset": fallback_preset,
        "max_retries": max_retries,
    }
    payload, errors = _form_to_payload(
        description=description,
        provider=provider,
        concrete_model=concrete_model,
        api_key_ref=api_key_ref,
        api_key=api_key,
        base_url=base_url,
        min_user_level=min_user_level,
        default_temperature=default_temperature,
        default_max_tokens=default_max_tokens,
        default_provider_options=default_provider_options,
        fallback_preset=fallback_preset,
        max_retries=max_retries,
    )
    payload["name"] = name.strip()

    if errors:
        return templates.TemplateResponse(
            request,
            "llm_presets/new.html",
            {
                "active_section": "llm_presets",
                "providers": PROVIDERS,
                "user_levels": USER_LEVELS,
                "fallback_choices": await _existing_preset_names(request),
                "form": form,
                "errors": errors,
            },
            status_code=400,
        )

    try:
        await upstream(request).admin_request(
            "POST",
            "/llm/presets",
            access_token=access_token(request),
            json=payload,
        )
    except UpstreamError as exc:
        return templates.TemplateResponse(
            request,
            "llm_presets/new.html",
            {
                "active_section": "llm_presets",
                "providers": PROVIDERS,
                "user_levels": USER_LEVELS,
                "fallback_choices": await _existing_preset_names(request),
                "form": form,
                "errors": [detail_message(exc)],
            },
            status_code=exc.status_code,
        )

    return redirect_with_flash(request, "/admin/llm/presets", f"preset {name!r} created")


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@router.get("/{name}/edit", response_class=HTMLResponse)
async def edit_preset_form(request: Request, name: str) -> HTMLResponse:
    try:
        preset = await upstream(request).admin_request(
            "GET", f"/llm/presets/{name}", access_token=access_token(request)
        )
    except UpstreamError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="preset not found") from exc
        return error_response(request, exc, partial=False, active_section="llm_presets")

    fallback_choices = [n for n in await _existing_preset_names(request) if n != name]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "llm_presets/edit.html",
        {
            "active_section": "llm_presets",
            "providers": PROVIDERS,
            "user_levels": USER_LEVELS,
            "preset_name": name,
            "fallback_choices": fallback_choices,
            "has_api_key": bool(preset.get("has_api_key")),
            "form": _form_from_preset(preset),
            "errors": [],
        },
    )


@router.post("/{name}/edit", response_class=HTMLResponse)
async def update_preset(
    request: Request,
    name: str,
    description: str = Form(""),
    provider: str = Form(...),
    concrete_model: str = Form(...),
    api_key_ref: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: str = Form(""),
    base_url: str = Form(""),
    min_user_level: str = Form("*"),
    default_temperature: str = Form(""),
    default_max_tokens: str = Form(""),
    default_provider_options: str = Form(""),
    fallback_preset: str = Form(""),
    max_retries: str = Form("0"),
) -> Response:
    templates = request.app.state.templates
    # Checkbox arrives as "on" when ticked; treat any truthy string as
    # the request to clear the inline secret.
    clear_flag = bool(clear_api_key.strip())
    form = {
        "name": name,
        "description": description,
        "provider": provider,
        "concrete_model": concrete_model,
        "api_key_ref": api_key_ref,
        "api_key": "",  # never echo back
        "base_url": base_url,
        "min_user_level": min_user_level,
        "default_temperature": default_temperature,
        "default_max_tokens": default_max_tokens,
        "default_provider_options": default_provider_options,
        "fallback_preset": fallback_preset,
        "max_retries": max_retries,
    }
    payload, errors = _form_to_payload(
        description=description,
        provider=provider,
        concrete_model=concrete_model,
        api_key_ref=api_key_ref,
        api_key=api_key,
        base_url=base_url,
        min_user_level=min_user_level,
        default_temperature=default_temperature,
        default_max_tokens=default_max_tokens,
        default_provider_options=default_provider_options,
        fallback_preset=fallback_preset,
        max_retries=max_retries,
    )

    if clear_flag and api_key.strip():
        errors.append("Choose either a new API key or clear — not both.")

    if errors:
        fallback_choices = [n for n in await _existing_preset_names(request) if n != name]
        return templates.TemplateResponse(
            request,
            "llm_presets/edit.html",
            {
                "active_section": "llm_presets",
                "providers": PROVIDERS,
                "user_levels": USER_LEVELS,
                "preset_name": name,
                "fallback_choices": fallback_choices,
                # has_api_key state from before submit is unknown here;
                # showing the same warning regardless is fine.
                "has_api_key": False,
                "form": form,
                "errors": errors,
            },
            status_code=400,
        )

    # PATCH semantics: clearing optional fields needs explicit None;
    # an empty form field means "drop / unset". We strip those keys
    # from the payload, then explicitly null them out below to match
    # PATCH-with-`exclude_none=False` behaviour.
    clear_fields: dict[str, Any] = {}
    if not default_temperature.strip():
        clear_fields["default_temperature"] = None
    if not default_max_tokens.strip():
        clear_fields["default_max_tokens"] = None
    if not default_provider_options.strip():
        clear_fields["default_provider_options"] = None
    if not description.strip():
        clear_fields["description"] = None
    payload = {**payload, **clear_fields}

    if clear_flag:
        payload["clear_api_key"] = True
        # `_form_to_payload` won't have set api_key (empty input).

    try:
        await upstream(request).admin_request(
            "PATCH",
            f"/llm/presets/{name}",
            access_token=access_token(request),
            json=payload,
        )
    except UpstreamError as exc:
        fallback_choices = [n for n in await _existing_preset_names(request) if n != name]
        return templates.TemplateResponse(
            request,
            "llm_presets/edit.html",
            {
                "active_section": "llm_presets",
                "providers": PROVIDERS,
                "user_levels": USER_LEVELS,
                "preset_name": name,
                "fallback_choices": fallback_choices,
                "has_api_key": False,
                "form": form,
                "errors": [detail_message(exc)],
            },
            status_code=exc.status_code,
        )

    return redirect_with_flash(
        request, "/admin/llm/presets", f"preset {name!r} updated"
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.post("/{name}/delete")
async def delete_preset(request: Request, name: str) -> Response:
    try:
        await upstream(request).admin_request(
            "DELETE",
            f"/llm/presets/{name}",
            access_token=access_token(request),
        )
    except UpstreamError as exc:
        return redirect_with_flash(
            request, "/admin/llm/presets", detail_message(exc)
        )
    return redirect_with_flash(
        request, "/admin/llm/presets", f"preset {name!r} deleted"
    )
