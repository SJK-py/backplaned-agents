"""bp_agents.user_prefs — the user's own settings, in the router's user scope.

These six values used to be columns on the suite's `user_config` table. They
are now keys in the router's **user-scoped state** — the `(user_id, key)`
namespace the session store exposes when a batch runs with `scope="user"` and
`session_scoped=True` ([../docs/design/router-managed-session-store.md] §3.1).
That is the whole point of the move: the ten agents that read a name and a
timezone at turn start no longer need a Postgres credential to do it.

**What did NOT move, and why.** `sandbox_uid` and `default_session_id` stay in
`user_config`. Both are read *outside* any task — by the sandbox host and the
cron scheduler — where neither an agent's `ctx.history` nor a steward's
carrier session exists, and `sandbox_uid` additionally needs cross-user
uniqueness that a per-key namespace does not give. The four `preset_*` fields
went somewhere else again, to their own router table: the router *acts* on
those, and user-scoped state is writable by any agent in the session, so a
value the router enforces policy on cannot live here
([../docs/design/router-resolved-preset-slots.md] §4). The test for anything
tempted into this module: **does the router read it to make a decision?** If
yes, it belongs in a table with a user-authority write path, not here.

**Two reader shapes, one vocabulary.**

  * *Inside a task* — `load_prefs(ctx, settings)` rides `ctx.history.user_scope`,
    the agent's own authenticated socket. Every stateful agent does this once
    per turn.
  * *Outside one* — `load_prefs_via_store(...)` / `save_prefs_via_store(...)`
    go through the steward HTTP surface (`POST /v1/sessions/{id}/ops` with
    `scope="user"`), which the webapp and the chatbot gateway already hold.
    User-scoped state is addressable only *through* a session, so those take a
    carrier `session_id`; a CLOSED session serves, since ownership is what the
    router checks there.

**No CAS on a write, deliberately.** Every write here is a blind set of one
independent key to an absolute value the user just supplied ("set my timezone
to Asia/Seoul"). Two writers touching different keys do not conflict at all,
and two writers touching the same key want last-write-wins — a
`version_conflict` would be a spurious failure for a settings form. CAS is for
read-modify-write, which this is not; the rolling summary is where the store's
`expected_version` earns its keep.

**A missing key is the default, not an error.** A user who never opened the
settings page has no rows at all, which is the normal case rather than an
edge one. So is a malformed value: decoding falls back to the default and
logs rather than failing a turn, because a bad `max_context_token_limit`
must never cost the user their answer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bp_protocol.frames import GetStateOp, SetStateOp

if TYPE_CHECKING:
    from bp_agents.channel.store import SessionStore
    from bp_agents.settings import SuiteSettings
    from bp_protocol.frames import StateValue
    from bp_sdk import TaskContext

logger = logging.getLogger(__name__)

# The editable set, mapped to the type each value coerces to. This is the
# single source of truth for BOTH the config agent's `set_config` tool (the
# natural-language path) and the webapp's settings form, so the two cannot
# drift their own notion of "editable" or "valid".
PREF_TYPES: dict[str, type] = {
    "full_name": str,
    "timezone": str,
    "language": str,
    "verbose_default": bool,
    "custom_note": str,
    "max_context_token_limit": int,
}

PREF_KEYS: tuple[str, ...] = tuple(PREF_TYPES)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ConfigError(ValueError):
    """An unknown field or an un-coercible value."""


@dataclass(frozen=True)
class UserPrefs:
    """One user's settings, with defaults already applied.

    Field names match the old `UserConfigRow` columns on purpose: every
    reader (`prompts.user_config_note`, the context-limit lookup, the
    timezone lookup) keeps working against this instead, and there is no
    `if cfg else settings.default_…` at any call site any more — an absent
    key resolved to the default before this object was built.
    """

    full_name: str = ""
    timezone: str = "UTC"
    language: str = "en"
    verbose_default: bool = False
    custom_note: str = ""
    max_context_token_limit: int = 120_000


def defaults_from(settings: SuiteSettings) -> UserPrefs:
    """The operator's configured defaults — what a user with no stored
    preferences gets."""
    return UserPrefs(
        timezone=settings.default_timezone,
        language=settings.default_language,
        max_context_token_limit=settings.default_max_context_token_limit,
    )


# ---------------------------------------------------------------------------
# Coercion — one implementation, two faces
# ---------------------------------------------------------------------------


def _coerce(field: str, raw: Any) -> Any:
    """Coerce `raw` to `field`'s stored type. Raises on an unknown field or
    an un-coercible value; callers choose whether that is fatal."""
    typ = PREF_TYPES.get(field)
    if typ is None:
        raise ConfigError(
            f"Unknown field {field!r}. Editable: {sorted(PREF_TYPES)}"
        )
    try:
        if typ is bool:
            return str(raw).strip().lower() in _TRUTHY
        if typ is int:
            return int(raw)
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for {field}: {raw!r}") from exc


def editable_fields() -> dict[str, type]:
    """The fields a user may SET."""
    return dict(PREF_TYPES)


def displayable_fields() -> list[str]:
    """The fields a user may SEE on a read. Identical to the editable set —
    every stored field a user can read is one they can change."""
    return list(PREF_TYPES)


def coerce_config_value(field: str, raw: Any) -> Any:
    """Validate `field` is editable and coerce `raw` to its stored type.
    Raises `ConfigError` (a `ValueError`) — the form and the `set_config`
    tool both surface the message to the user."""
    return _coerce(field, raw)


def encode_pref(field: str, value: Any) -> str:
    """A coerced value as the string the store holds. Booleans go as
    `true`/`false` rather than Python's `True`/`False` so a value read back
    by anything other than this module still reads naturally."""
    typ = PREF_TYPES.get(field)
    if typ is bool:
        return "true" if value else "false"
    return str(value)


def decode_prefs(
    state: Mapping[str, StateValue], *, defaults: UserPrefs
) -> UserPrefs:
    """Build a `UserPrefs` from what the store returned. Absent keys take
    the default; a malformed value takes the default and logs."""
    values: dict[str, Any] = {}
    for field in PREF_KEYS:
        entry = state.get(field)
        if entry is None or entry.value is None:
            continue
        try:
            values[field] = _coerce(field, entry.value)
        except ConfigError:
            logger.warning(
                "user_pref_undecodable",
                extra={"event": "user_pref_undecodable", "field": field},
            )
    return UserPrefs(
        **{f: values.get(f, getattr(defaults, f)) for f in PREF_KEYS}
    )


def as_display(prefs: UserPrefs) -> str:
    """The settings rendered for a model or a user to read back."""
    return "\n".join(f"{f}: {getattr(prefs, f)}" for f in displayable_fields())


# ---------------------------------------------------------------------------
# Reading and writing — in a task
# ---------------------------------------------------------------------------


async def load_prefs(ctx: TaskContext, settings: SuiteSettings) -> UserPrefs:
    """This user's settings, read through the executing agent's own socket.

    One round trip, independent of everything else `open_turn` does, so a
    caller that cares about latency can `asyncio.gather` it with the turn
    open. A store failure degrades to the operator defaults rather than
    failing the turn — preferences are context, not correctness."""
    defaults = defaults_from(settings)
    try:
        state = await ctx.history.user_scope.state(
            *PREF_KEYS, session_scoped=True
        )
    except Exception:  # noqa: BLE001 — settings must never cost an answer
        logger.warning(
            "user_prefs_read_failed",
            extra={"event": "user_prefs_read_failed", "bp.user_id": ctx.user_id},
        )
        return defaults
    return decode_prefs(state, defaults=defaults)


async def save_pref(ctx: TaskContext, field: str, value: Any) -> None:
    """Set one field from inside a task (the config agent's `set_config`).
    `value` must already be coerced — `coerce_config_value` is the gate."""
    await ctx.history.user_scope.set_state(
        field, encode_pref(field, value), session_scoped=True
    )


# ---------------------------------------------------------------------------
# Reading and writing — from a steward (webapp, chatbot gateway)
# ---------------------------------------------------------------------------


async def load_prefs_via_store(
    store: SessionStore,
    *,
    user_id: str,
    session_id: str,
    settings: SuiteSettings,
) -> UserPrefs:
    """This user's settings, read over the steward HTTP surface.

    `session_id` is a CARRIER, not a scope: the batch runs with
    `scope="user"`, so the session only supplies the ownership check the
    endpoint performs. Any session the user owns serves, closed included."""
    defaults = defaults_from(settings)
    try:
        results = await store.ops(
            user_id=user_id,
            session_id=session_id,
            scope="user",
            ops=[GetStateOp(session_scoped=True, keys=list(PREF_KEYS))],
        )
    except Exception:  # noqa: BLE001 — same degradation as the in-task read
        logger.warning(
            "user_prefs_read_failed",
            extra={"event": "user_prefs_read_failed", "bp.user_id": user_id},
        )
        return defaults
    state = {sv.key: sv for sv in (results[0].state or [])}
    return decode_prefs(state, defaults=defaults)


async def save_prefs_via_store(
    store: SessionStore,
    *,
    user_id: str,
    session_id: str,
    updates: Mapping[str, Any],
) -> None:
    """Set several fields in ONE batch. Values must already be coerced.

    One batch rather than one call per field so a settings form saves
    atomically — a refusal mid-list would otherwise leave the user looking
    at a page that reports some of what they typed."""
    ops = [
        SetStateOp(
            session_scoped=True, key=field, value=encode_pref(field, value)
        )
        for field, value in updates.items()
    ]
    if not ops:
        return
    await store.ops(
        user_id=user_id, session_id=session_id, scope="user", ops=ops
    )
