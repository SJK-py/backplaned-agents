"""bp_agents.db.queries — async query functions over the suite Postgres.

Every function takes an asyncpg connection (or pool-acquired conn) as
its first argument; callers own transaction scope. Row results are
parsed into the `models` types. Mutable-column allowlists guard the
few dynamic-SQL paths so column names can never come from caller input.

What is left here after the session store moved conversation into the
router: per-user config, cron, chat platform mappings, and the GC reclaim
paths for each.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from bp_agents.db.models import (
    CronJobRow,
    PlatformMappingRow,
    UserConfigRow,
)

if TYPE_CHECKING:
    import asyncpg


# ---------------------------------------------------------------------------
# GC / purge reclaim
#
# The conversation and its session descriptors live in the ROUTER's session
# store now, so a purge there takes them with it. What is left here is the
# suite's own: cron and chat mappings.
# ---------------------------------------------------------------------------


async def purge_session_suite_data(
    conn: asyncpg.Connection, session_id: str
) -> dict[str, int]:
    """Reclaim a session's suite-side rows on a webapp 'remove' — the router
    purge ([webapp.md] §4) hard-deletes its own session/tasks/files (which
    now includes the conversation itself) but doesn't reach `bp_suite`.

    `cron_jobs` is all that is left keyed by session: history and session
    descriptors moved into the router's session store. Caller MUST have
    verified ownership (session_ids are router-unique, but this is keyed only
    by session_id). Run inside a transaction for atomicity. Returns per-table
    delete counts."""
    counts: dict[str, int] = {}
    for table in ("cron_jobs",):
        status = await conn.execute(
            f"DELETE FROM {table} WHERE session_id = $1", session_id  # noqa: S608
        )
        # asyncpg returns e.g. "DELETE 3"; the trailing token is the count.
        counts[table] = int(status.rsplit(" ", 1)[-1]) if status else 0
    return counts


async def purge_user_suite_data(
    conn: asyncpg.Connection, user_id: str
) -> dict[str, int]:
    """Erase ALL of a user's suite-side rows on a permanent user purge — the
    router hard-deletes its own store + scrubs PII, but doesn't reach
    `bp_suite`. Deletes `cron_executions`, `cron_jobs`,
    `suite_platform_mappings`, `user_config`. Run inside a transaction.
    Returns per-table delete counts. Caller MUST have confirmed (via the
    router) that the user is purged."""
    counts: dict[str, int] = {}
    for table in (
        "cron_executions", "cron_jobs", "suite_platform_mappings",
        "user_config",
    ):
        status = await conn.execute(
            f"DELETE FROM {table} WHERE user_id = $1", user_id  # noqa: S608
        )
        counts[table] = int(status.rsplit(" ", 1)[-1]) if status else 0
    return counts


async def list_user_config_ids(
    conn: asyncpg.Connection, *, limit: int
) -> list[str]:
    """All user ids the suite holds config for, capped. The user-purge
    reconcile enumerates these, asks the router which are purged, and erases
    those. `user_config` is the canonical per-user row; session-scoped rows are
    additionally covered by the session-GC reconcile."""
    rows = await conn.fetch(
        "SELECT user_id FROM user_config ORDER BY user_id LIMIT $1", limit
    )
    return [r["user_id"] for r in rows]


async def list_old_session_ids(
    conn: asyncpg.Connection, *, before: datetime, limit: int
) -> list[str]:
    """Session ids the suite still holds rows for, created before `before`,
    oldest first.

    A cheap pre-filter for the suite session-GC reconcile: a session closed
    past the retention window must have been created before it, so this bounds
    the set before the router existence check decides which to reap. Now keyed
    on `cron_jobs` — the only session-scoped suite table left once the
    conversation moved into the router's session store. Global (all users):
    the GC is a deployment-wide maintenance sweep."""
    rows = await conn.fetch(
        "SELECT DISTINCT session_id FROM cron_jobs WHERE created_at < $1 "
        "ORDER BY session_id LIMIT $2",
        before,
        limit,
    )
    return [r["session_id"] for r in rows]


async def get_user_config(
    conn: asyncpg.Connection, user_id: str
) -> UserConfigRow | None:
    row = await conn.fetchrow(
        "SELECT * FROM user_config WHERE user_id = $1", user_id
    )
    return UserConfigRow.model_validate(dict(row)) if row else None


async def list_user_ids(conn: asyncpg.Connection) -> list[str]:
    """All known user ids (the memory GC sweep iterates these)."""
    rows = await conn.fetch("SELECT user_id FROM user_config")
    return [r["user_id"] for r in rows]


async def create_user_config(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    sandbox_uid: int | None = None,
    default_session_id: str | None = None,
) -> UserConfigRow:
    """Create a user_config row. Idempotent — an existing row is returned
    unchanged.

    The user's settings are NOT here any more; they are keys in the router's
    user scope, where an absent key is simply the operator default and there
    is nothing to pre-create (`bp_agents.user_prefs`)."""
    row = await conn.fetchrow(
        """
        INSERT INTO user_config (user_id, sandbox_uid, default_session_id)
        VALUES ($1,$2,$3)
        ON CONFLICT (user_id) DO NOTHING
        RETURNING *
        """,
        user_id,
        sandbox_uid,
        default_session_id,
    )
    if row is None:
        existing = await get_user_config(conn, user_id)
        assert existing is not None
        return existing
    return UserConfigRow.model_validate(dict(row))


async def set_default_session_id(
    conn: asyncpg.Connection, *, user_id: str, session_id: str | None
) -> None:
    """Move the per-user cron-fallback pointer ([cron.md] §4)."""
    await conn.execute(
        "UPDATE user_config SET default_session_id = $2, updated_at = now() "
        "WHERE user_id = $1",
        user_id,
        session_id,
    )


# ---------------------------------------------------------------------------
# suite_platform_mappings  (inbound identity — chat_id → user_id)
# ---------------------------------------------------------------------------


async def resolve_user_id(
    conn: asyncpg.Connection, *, platform: str, chat_id: str
) -> str | None:
    """The inbound entry point: `(platform, chat_id) → user_id`. `None`
    ⇒ an unmapped chat (→ the `/register` prompt — [channel.md] §2)."""
    return await conn.fetchval(
        "SELECT user_id FROM suite_platform_mappings "
        "WHERE platform = $1 AND chat_id = $2",
        platform,
        chat_id,
    )


async def get_platform_mapping(
    conn: asyncpg.Connection, *, platform: str, chat_id: str
) -> PlatformMappingRow | None:
    """The full mapping row — `user_id` AND the chat's current `session_id` —
    or None for an unmapped chat. Inbound routing uses this to ride the chat's
    OWN session, falling back to `default_session_id` when `session_id` is
    NULL."""
    row = await conn.fetchrow(
        "SELECT * FROM suite_platform_mappings WHERE platform = $1 AND chat_id = $2",
        platform,
        chat_id,
    )
    return PlatformMappingRow.model_validate(dict(row)) if row else None


async def upsert_platform_mapping(
    conn: asyncpg.Connection,
    *,
    platform: str,
    chat_id: str,
    user_id: str,
    session_id: str | None = None,
) -> PlatformMappingRow:
    """Bind a channel-native chat to a user (the admin approve-registration
    flow). Re-binding a chat updates the `user_id`. `session_id` SEEDS the
    chat's current session but never clobbers one already set — a later
    reconcile (which may carry a stale serviced session) must not overwrite a
    session the chat moved to via `/new` or `/setdefault`."""
    row = await conn.fetchrow(
        """
        INSERT INTO suite_platform_mappings (platform, chat_id, user_id, session_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (platform, chat_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            session_id = COALESCE(
                suite_platform_mappings.session_id, EXCLUDED.session_id
            )
        RETURNING *
        """,
        platform,
        chat_id,
        user_id,
        session_id,
    )
    return PlatformMappingRow.model_validate(dict(row))


async def set_mapping_session_id(
    conn: asyncpg.Connection, *, platform: str, chat_id: str, session_id: str
) -> None:
    """Point a chat at its current live session (unconditional). Used by
    `/new` (the chat's fresh session) and `/link` (the linked chat's own
    session); `/setdefault` is the inverse — it copies this onto
    `default_session_id`."""
    await conn.execute(
        "UPDATE suite_platform_mappings SET session_id = $3 "
        "WHERE platform = $1 AND chat_id = $2",
        platform,
        chat_id,
        session_id,
    )


async def list_platform_mappings_for_user(
    conn: asyncpg.Connection, *, user_id: str, platform: str | None = None
) -> list[PlatformMappingRow]:
    """Reverse lookup `user_id → mappings` (uses the `user_id` index).
    Optionally filtered to one `platform`. Used by the cron scheduler to
    find a user's out-of-band channel (e.g. their Telegram `chat_id`) when
    a fired job's target session can't be live-reached ([cron.md] §6)."""
    if platform is not None:
        rows = await conn.fetch(
            "SELECT * FROM suite_platform_mappings "
            "WHERE user_id = $1 AND platform = $2 ORDER BY created_at",
            user_id, platform,
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM suite_platform_mappings "
            "WHERE user_id = $1 ORDER BY created_at",
            user_id,
        )
    return [PlatformMappingRow.model_validate(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# cron_jobs / cron_executions  (chatbot scheduler — [cron.md])
# ---------------------------------------------------------------------------


async def create_cron_job(
    conn: asyncpg.Connection,
    *,
    cron_id: str,
    user_id: str,
    session_id: str,
    cron_expression: str,
    cron_message: str,
    timezone: str = "UTC",
    report: str = "case_by_case",
    execute_until: datetime | None = None,
) -> CronJobRow:
    row = await conn.fetchrow(
        """
        INSERT INTO cron_jobs (
            cron_id, user_id, session_id, cron_expression, cron_message,
            timezone, report, execute_until
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        cron_id, user_id, session_id, cron_expression, cron_message,
        timezone, report, execute_until,
    )
    return CronJobRow.model_validate(dict(row))


async def get_cron_job(
    conn: asyncpg.Connection, cron_id: str
) -> CronJobRow | None:
    row = await conn.fetchrow("SELECT * FROM cron_jobs WHERE cron_id = $1", cron_id)
    return CronJobRow.model_validate(dict(row)) if row else None


async def list_cron_jobs(
    conn: asyncpg.Connection, *, user_id: str, status: str | None = None
) -> list[CronJobRow]:
    if status is None:
        rows = await conn.fetch(
            "SELECT * FROM cron_jobs WHERE user_id = $1 ORDER BY created_at", user_id
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM cron_jobs WHERE user_id = $1 AND status = $2 "
            "ORDER BY created_at",
            user_id, status,
        )
    return [CronJobRow.model_validate(dict(r)) for r in rows]


async def list_active_cron_jobs(conn: asyncpg.Connection) -> list[CronJobRow]:
    """Active jobs that haven't expired — the scheduler's scan set."""
    rows = await conn.fetch(
        "SELECT * FROM cron_jobs WHERE status = 'active' "
        "AND (execute_until IS NULL OR execute_until > now())"
    )
    return [CronJobRow.model_validate(dict(r)) for r in rows]


_CRON_MUTABLE = frozenset(
    {"session_id", "cron_expression", "timezone", "report", "cron_message",
     "status", "execute_until"}
)


async def update_cron_job(
    conn: asyncpg.Connection, cron_id: str, **fields: Any
) -> None:
    cols = {k: v for k, v in fields.items() if k in _CRON_MUTABLE}
    unknown = set(fields) - _CRON_MUTABLE
    if unknown:
        raise ValueError(f"update_cron_job: non-mutable columns {sorted(unknown)}")
    if not cols:
        return
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    await conn.execute(
        f"UPDATE cron_jobs SET {set_clause} WHERE cron_id = $1",
        cron_id, *cols.values(),
    )


async def remove_cron_job(conn: asyncpg.Connection, cron_id: str) -> int:
    status = await conn.execute("DELETE FROM cron_jobs WHERE cron_id = $1", cron_id)
    return int(status.rsplit(" ", 1)[-1]) if status else 0


async def claim_cron_job(
    conn: asyncpg.Connection, *, cron_id: str, due: datetime, now: datetime
) -> bool:
    """Atomic claim ([cron.md] §1): set `last_executed_at = now` iff the
    job is active and hasn't already been claimed for this `due` window.
    Only one worker wins — no double-fire."""
    row = await conn.fetchrow(
        """
        UPDATE cron_jobs SET last_executed_at = $2
        WHERE cron_id = $1 AND status = 'active'
          AND (last_executed_at IS NULL OR last_executed_at < $3)
        RETURNING cron_id
        """,
        cron_id, now, due,
    )
    return row is not None


async def deactivate_cron_job(conn: asyncpg.Connection, cron_id: str) -> None:
    await conn.execute(
        "UPDATE cron_jobs SET status = 'inactive' WHERE cron_id = $1", cron_id
    )


async def record_cron_execution(
    conn: asyncpg.Connection,
    *,
    cron_id: str,
    user_id: str,
    session_id: str,
    reported: bool,
    reason: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO cron_executions
            (cron_id, user_id, session_id, reported, reason, message, error)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        cron_id, user_id, session_id, reported, reason, message, error,
    )
