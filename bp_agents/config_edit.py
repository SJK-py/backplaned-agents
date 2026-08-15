"""bp_agents.config_edit — shared user-config field validation.

The single source of truth for which `user_config` fields a user may edit
and how raw input coerces to a stored value. Used by BOTH the config
agent's `set_config` tool (NL path) and the webapp's structured config
form ([webapp.md] §5, Decision 2) so the two agree — neither can drift its
own notion of "editable" or "valid".

**Model selection is not here any more.** It moved to router preset slots
([../docs/design/router-resolved-preset-slots.md]): the user's choice is a
router-side preference, gate-checked at selection time against their own
tier, and written only under the user's own session JWT. The webapp
settings form carries that surface (`GET /v1/llm/presets`,
`PUT /v1/llm/preferences`); an agent has no write path to it by design,
because the router *acts* on the value it stores. `sandbox_uid` and
`default_session_id` were never user-editable and still aren't.
"""

from __future__ import annotations

from typing import Any

# The editable subset of `user_config`, mapped to the type each coerces to.
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


def editable_fields() -> dict[str, type]:
    """The fields a user may SET."""
    return dict(EDITABLE_FIELDS)


def displayable_fields() -> list[str]:
    """The fields a user may SEE on a read. Identical to the editable set —
    every stored field a user can read is one they can change."""
    return list(EDITABLE_FIELDS)


def coerce_config_value(field: str, raw: Any) -> Any:
    """Validate `field` is editable and coerce `raw` to its stored type.
    Raises `ConfigError` (a `ValueError`) on an unknown field or an
    un-coercible value."""
    allowed = editable_fields()
    if field not in allowed:
        raise ConfigError(
            f"Unknown field {field!r}. Editable: {sorted(allowed)}"
        )
    typ = allowed[field]
    try:
        if typ is bool:
            return str(raw).strip().lower() in _TRUTHY
        if typ is int:
            return int(raw)
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for {field}: {raw!r}") from exc
