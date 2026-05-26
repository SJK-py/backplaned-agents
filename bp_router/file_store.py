"""bp_router.file_store — shared named-store mechanics.

The router-managed named store (docs/design/router-managed-file-store.md)
maps a NAME → blob in the content-addressed `files` registry. The
caller-facing name is `{filename}` (session scope) or `persist/{filename}`
(user-wide persistent scope).

These helpers — name parsing, scope keys, the dedup allocation policy,
and the per-user quota gate — are shared by BOTH the WS frame handlers
(`bp_router.dispatch`) and the session-authed HTTP endpoints
(`bp_router.api.files`) so the two surfaces behave identically. Lives
outside `bp_router.api` and `bp_router.dispatch` so neither has to import
the other to reuse the logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bp_router.filename_utils import _FILENAME_REJECT

if TYPE_CHECKING:
    from bp_router.app import AppState

_PERSIST_PREFIX = "persist/"
# Bound the dedup append-counter loop. A user with this many
# same-named files is pathological; refuse rather than spin.
_MAX_DEDUP_ATTEMPTS = 1000


def _valid_bare_filename(name: str) -> bool:
    """A flat stash filename: non-empty, no path separator, no
    control chars / quotes (reuses the upload-endpoint reject set)."""
    return bool(name) and "/" not in name and not _FILENAME_REJECT.search(name)


def _split_stash_name(name: str) -> tuple[bool, str] | None:
    """Parse a `{filename}` / `persist/{filename}` reference into
    `(persistent, bare_filename)`. `persist/` is the reserved scope
    prefix. Returns None if the bare name is invalid."""
    if name.startswith(_PERSIST_PREFIX):
        bare = name[len(_PERSIST_PREFIX):]
        persistent = True
    else:
        bare = name
        persistent = False
    if not _valid_bare_filename(bare):
        return None
    return persistent, bare


def _scope_for(persistent: bool, session_id: str) -> str:
    """Directory scope key: `persist` (user-wide) or
    `session:{session_id}` (ephemeral baseline)."""
    return "persist" if persistent else f"session:{session_id}"


def _display_name(persistent: bool, bare: str) -> str:
    """Render a saved bare filename back to its caller-facing form
    (`persist/` prefix re-attached for the persistent scope)."""
    return f"{_PERSIST_PREFIX}{bare}" if persistent else bare


def _next_dedup_candidate(filename: str, n: int) -> str:
    """`report.pdf`, 1 → `report_1.pdf`; `README`, 2 → `README_2`.
    Splits on the LAST dot so multi-dot names keep their extension."""
    stem, dot, ext = filename.rpartition(".")
    if dot:
        return f"{stem}_{n}.{ext}"
    return f"{filename}_{n}"


async def _allocate_name(
    sq: Any,
    *,
    scope: str,
    filename: str,
    file_id: str,
    byte_size: int,
    dedup: str,
) -> tuple[str | None, str | None, int]:
    """Bind `filename` in `scope` to `file_id` per the dedup policy.

    Returns `(saved_bare_name, error, added_bytes)`:
      * new name → inserted; added_bytes = byte_size.
      * same name + same blob → idempotent no-op; added_bytes = 0.
      * different blob + `error` → ("", "filename_exists", 0).
      * different blob + `overwrite` → repoint; added_bytes = delta.
      * different blob + `append_count` → `name_N`; added_bytes =
        byte_size.
    The caller has ALREADY passed the quota gate for the worst-case
    add; `added_bytes` is returned for observability/audit only.
    """
    existing = await sq.resolve_file_name(scope, filename)
    if existing is None:
        if await sq.insert_file_name(
            scope=scope, filename=filename, file_id=file_id,
            byte_size=byte_size,
        ):
            return filename, None, byte_size
        # Lost an insert race; re-read and fall through to dedup.
        existing = await sq.resolve_file_name(scope, filename)

    if existing is not None and existing.file_id == file_id:
        # Same content under the same name → idempotent.
        return filename, None, 0

    if dedup == "error":
        return None, "filename_exists", 0
    if dedup == "overwrite" and existing is not None:
        await sq.repoint_file_name(
            scope=scope, filename=filename, file_id=file_id,
            byte_size=byte_size,
        )
        return filename, None, byte_size - existing.byte_size

    # append_count
    for n in range(1, _MAX_DEDUP_ATTEMPTS + 1):
        candidate = _next_dedup_candidate(filename, n)
        if await sq.insert_file_name(
            scope=scope, filename=candidate, file_id=file_id,
            byte_size=byte_size,
        ):
            return candidate, None, byte_size
    return None, "filename_exists", 0


async def _quota_ok(
    state: AppState, sq: Any, user_id: str, added_bytes: int
) -> bool:
    """True if adding `added_bytes` keeps the user under their
    level's `file_storage_quota_bytes` ceiling. `added_bytes <= 0`
    (idempotent / shrinking overwrite) always passes; `None` ceiling
    is uncapped."""
    if added_bytes <= 0:
        return True
    from bp_router.tasks import _session_level  # noqa: PLC0415

    level = await _session_level(state, user_id)
    ceiling = state.settings.file_storage_quota_bytes.get(  # type: ignore[attr-defined]
        level or ""
    )
    if ceiling is None:
        return True
    usage = await sq.count_user_storage_bytes()
    return usage + added_bytes <= ceiling
