"""bp_agents.config_edit — shared user-config field validation.

The single source of truth for which `user_config` fields a user may edit
and how raw input coerces to a stored value. Used by BOTH the config
agent's `set_config` tool (NL path) and the webapp's structured config
form ([webapp.md] §5, Decision 2) so the two agree — neither can drift its
own notion of "editable" or "valid".
"""

from __future__ import annotations

from typing import Any

# Fields a user may read/set (a subset of `user_config`), mapped to the
# type each coerces to. Presets, sandbox_uid, default_session_id are NOT
# here — they're system-managed, not user-editable.
EDITABLE_FIELDS: dict[str, type] = {
    "full_name": str,
    "timezone": str,
    "language": str,
    "verbose_default": bool,
    "custom_note": str,
    "max_context_token_limit": int,
}

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ConfigError(ValueError):
    """An unknown field or an un-coercible value."""


def coerce_config_value(field: str, raw: Any) -> Any:
    """Validate `field` is editable and coerce `raw` to its stored type.
    Raises `ConfigError` (a `ValueError`) on an unknown field or bad value."""
    if field not in EDITABLE_FIELDS:
        raise ConfigError(
            f"Unknown field {field!r}. Editable: {sorted(EDITABLE_FIELDS)}"
        )
    typ = EDITABLE_FIELDS[field]
    try:
        if typ is bool:
            return str(raw).strip().lower() in _TRUTHY
        if typ is int:
            return int(raw)
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for {field}: {raw!r}") from exc
