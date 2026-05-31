"""bp_agents.config_edit — shared user-config field validation.

The single source of truth for which `user_config` fields a user may edit
and how raw input coerces to a stored value. Used by BOTH the config
agent's `set_config` tool (NL path) and the webapp's structured config
form ([webapp.md] §5, Decision 2) so the two agree — neither can drift its
own notion of "editable" or "valid".

The LLM-preset tier fields (`preset_pro` / `preset_balanced` /
`preset_lite`) are **opt-in, tier-gated**: a user may set one only when the
operator has configured a non-empty allow-list of selectable preset names
for that tier (`SuiteSettings.selectable_presets_*`), and only to a value
in that list. With no allow-list (the default) they stay system-managed and
un-editable, exactly as before. Embeddings (`preset_embedding`) and
`sandbox_uid` / `default_session_id` are never user-editable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bp_agents.settings import SuiteSettings

# Always-editable fields (a subset of `user_config`), mapped to the type
# each coerces to. The preset/tier fields are layered on top of this per
# the operator's allow-list (see `editable_fields`).
EDITABLE_FIELDS: dict[str, type] = {
    "full_name": str,
    "timezone": str,
    "language": str,
    "verbose_default": bool,
    "custom_note": str,
    "max_context_token_limit": int,
}

# Preset (tier) field → the `SuiteSettings` attribute holding that tier's
# user-selectable allow-list. A preset field becomes editable only when its
# allow-list is non-empty. (Embeddings stay system-managed — not here.)
PRESET_FIELDS: dict[str, str] = {
    "preset_pro": "selectable_presets_pro",
    "preset_balanced": "selectable_presets_balanced",
    "preset_lite": "selectable_presets_lite",
}

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# A `preset_choices` mapping: preset-field name → allowed preset names.
PresetChoices = dict[str, list[str]]


class ConfigError(ValueError):
    """An unknown field or an un-coercible value."""


def preset_choices_from_settings(settings: SuiteSettings | None) -> PresetChoices:
    """The per-tier user-selectable preset allow-lists, read off
    `SuiteSettings`. A tier with no configured list (the default) maps to an
    empty list → that preset stays system-managed. `None` settings (e.g. a
    read-only webapp built without them) → all tiers empty."""
    if settings is None:
        return {field: [] for field in PRESET_FIELDS}
    return {
        field: list(getattr(settings, attr, []) or [])
        for field, attr in PRESET_FIELDS.items()
    }


def editable_fields(preset_choices: PresetChoices | None = None) -> dict[str, type]:
    """The fields a user may read/set: the always-editable base, plus any
    preset/tier field whose allow-list (`preset_choices`) is non-empty."""
    fields = dict(EDITABLE_FIELDS)
    for field in PRESET_FIELDS:
        if preset_choices and preset_choices.get(field):
            fields[field] = str
    return fields


def displayable_fields() -> dict[str, type]:
    """The fields a user may SEE on a read — the always-editable base PLUS the
    per-tier preset fields, regardless of the allow-lists. Seeing which model
    each tier currently uses is their own config; only CHANGING it is gated
    (see `editable_fields`). Without this a bare `/config` hid the model info
    entirely whenever the operator hadn't opened up the tiers for editing."""
    return {**EDITABLE_FIELDS, **{f: str for f in PRESET_FIELDS}}


def coerce_config_value(
    field: str,
    raw: Any,
    *,
    preset_choices: PresetChoices | None = None,
) -> Any:
    """Validate `field` is editable and coerce `raw` to its stored type.
    A preset/tier field is editable only when `preset_choices` supplies a
    non-empty allow-list for it, and its value must be one of those names.
    Raises `ConfigError` (a `ValueError`) on an unknown field, a disallowed
    preset, or an un-coercible value."""
    allowed = editable_fields(preset_choices)
    if field not in allowed:
        raise ConfigError(
            f"Unknown field {field!r}. Editable: {sorted(allowed)}"
        )
    if field in PRESET_FIELDS:
        choices = (preset_choices or {}).get(field) or []
        value = str(raw)
        if value not in choices:
            raise ConfigError(
                f"Invalid preset for {field}: {value!r}. Choose one of: "
                f"{choices}"
            )
        return value
    typ = allowed[field]
    try:
        if typ is bool:
            return str(raw).strip().lower() in _TRUTHY
        if typ is int:
            return int(raw)
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for {field}: {raw!r}") from exc
