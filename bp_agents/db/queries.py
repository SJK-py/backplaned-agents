"""bp_agents.db.queries — async query functions over the suite Postgres.

Every function takes an asyncpg connection (or pool-acquired conn) as
its first argument; callers own transaction scope. Row results are
parsed into the `models` types. Mutable-column allowlists guard the
few dynamic-SQL paths so column names can never come from caller input.

These cover the core read/write paths the channel + worker agents need;
cron tables (Phase 4) and their queries land later.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from bp_agents.db.models import (
    CronJobRow,
    PlatformMappingRow,
    SessionHistoryRow,
    SessionInfoRow,
    UserConfigRow,
)

if TYPE_CHECKING:
    import asyncpg


# ---------------------------------------------------------------------------
# session_info  (channel-owned — session.management)
# ---------------------------------------------------------------------------


async def get_session_info(
    conn: asyncpg.Connection, session_id: str
) -> SessionInfoRow | None:
    row = await conn.fetchrow(
        "SELECT * FROM session_info WHERE session_id = $1", session_id
    )
    return SessionInfoRow.model_validate(dict(row)) if row else None


async def purge_session_suite_data(
    conn: asyncpg.Connection, session_id: str
) -> dict[str, int]:
    """Reclaim a session's suite-side rows on a webapp 'remove' — the router
    purge ([webapp.md] §4) hard-deletes its own session/tasks/files but
    doesn't reach `bp_suite`. Deletes `session_history`, `cron_jobs`, and
    `session_info` for `session_id`. Caller MUST have verified ownership
    (session_ids are router-unique, but this is keyed only by session_id).
    Run inside a transaction for atomicity. Returns per-table delete counts."""
    counts: dict[str, int] = {}
    for table in ("session_history", "cron_jobs", "session_info"):
        status = await conn.execute(
            f"DELETE FROM {table} WHERE session_id = $1", session_id  # noqa: S608
        )
        # asyncpg returns e.g. "DELETE 3"; the trailing token is the count.
        counts[table] = int(status.rsplit(" ", 1)[-1]) if status else 0
    return counts


async def list_session_info_for_user(
    conn: asyncpg.Connection, user_id: str
) -> list[SessionInfoRow]:
    """Every session_info row this user owns, newest first. Powers the
    webapp session list's channel badge + delegation status ([webapp.md]
    §4); the router's `/v1/sessions` remains the authoritative open/closed
    list."""
    rows = await conn.fetch(
        "SELECT * FROM session_info WHERE user_id = $1 ORDER BY created_at DESC",
        user_id,
    )
    return [SessionInfoRow.model_validate(dict(r)) for r in rows]


async def create_session_info(
    conn: asyncpg.Connection,
    *,
    session_id: str,
    user_id: str,
    channel: str,
    chat_id: str | None = None,
) -> SessionInfoRow:
    """Insert a session_info row. Idempotent — an existing row for the
    session is returned unchanged (the channel may re-resolve a session
    it already tracks)."""
    row = await conn.fetchrow(
        """
        INSERT INTO session_info (session_id, user_id, channel, chat_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (session_id) DO NOTHING
        RETURNING *
        """,
        session_id,
        user_id,
        channel,
        chat_id,
    )
    if row is None:
        existing = await get_session_info(conn, session_id)
        assert existing is not None
        return existing
    return SessionInfoRow.model_validate(dict(row))


_SESSION_INFO_MUTABLE = frozenset(
    {"chat_id", "delegated_to", "history_summary", "delegate_summary"}
)


async def update_session_info(
    conn: asyncpg.Connection, session_id: str, **fields: Any
) -> None:
    """Patch channel-owned session_info columns (also bumps
    `updated_at`). Only `_SESSION_INFO_MUTABLE` columns are accepted —
    the column names are a fixed allowlist, never caller input, so the
    interpolated SET clause carries no injection surface. `None` values
    write SQL NULL (e.g. `delegated_to=None` clears the delegate on
    hand-back)."""
    cols = {k: v for k, v in fields.items() if k in _SESSION_INFO_MUTABLE}
    unknown = set(fields) - _SESSION_INFO_MUTABLE
    if unknown:
        raise ValueError(f"update_session_info: non-mutable columns {sorted(unknown)}")
    if not cols:
        return
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    await conn.execute(
        f"UPDATE session_info SET {set_clause}, updated_at = now() "
        "WHERE session_id = $1",
        session_id,
        *cols.values(),
    )


# ---------------------------------------------------------------------------
# session_history  (channel writes user rows + summaries; agents write
# their own assistant/tool rows — all within the per-session queue)
# ---------------------------------------------------------------------------


async def append_history(
    conn: asyncpg.Connection,
    *,
    session_id: str,
    agent_id: str,
    role: str,
    message: str,
    incumbent: bool = True,
    hidden: bool = False,
) -> int:
    """Append one conversation row; returns its `id`."""
    return await conn.fetchval(
        """
        INSERT INTO session_history
            (session_id, agent_id, role, message, incumbent, hidden)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        session_id,
        agent_id,
        role,
        message,
        incumbent,
        hidden,
    )


async def reload_incumbent(
    conn: asyncpg.Connection,
    *,
    session_id: str,
    agent_id: str,
    up_to_id: int | None = None,
) -> list[SessionHistoryRow]:
    """The reload query ([sessions.md] §2.1): incumbent `user`/`assistant`
    rows for one agent's thread, in chronological order. `tool_call` /
    `tool_result` rows are never reloaded — the live loop holds the tool
    sequence in memory; persisted tool rows exist for render + audit.

    `up_to_id` bounds the read to rows with `id <= up_to_id` — the
    summarizer reads the cutoff window the channel asks it to fold."""
    args: list[Any] = [session_id, agent_id]
    bound = ""
    if up_to_id is not None:
        args.append(up_to_id)
        bound = "AND id <= $3"
    rows = await conn.fetch(
        f"""
        SELECT * FROM session_history
        WHERE session_id = $1 AND agent_id = $2
          AND incumbent = true
          AND role IN ('user', 'assistant')
          {bound}
        ORDER BY created_at ASC, id ASC
        """,
        *args,
    )
    return [SessionHistoryRow.model_validate(dict(r)) for r in rows]


async def demote_incumbent_through(
    conn: asyncpg.Connection,
    *,
    session_id: str,
    agent_id: str,
    up_to_id: int,
) -> int:
    """Flip `incumbent = false` on a thread's rows with `id <= up_to_id`
    — the summarization apply step ([sessions.md] §3.1). Returns the
    number of rows demoted."""
    status = await conn.execute(
        """
        UPDATE session_history SET incumbent = false
        WHERE session_id = $1 AND agent_id = $2
          AND id <= $3 AND incumbent = true
        """,
        session_id,
        agent_id,
        up_to_id,
    )
    # asyncpg returns e.g. "UPDATE 5"
    return int(status.rsplit(" ", 1)[-1]) if status else 0


async def demote_thread(
    conn: asyncpg.Connection, *, session_id: str, agent_id: str
) -> int:
    """Flip `incumbent = false` on ALL of a thread's rows — used when a
    delegation episode ends ([delegation.md] Phase 3): the orchestrator
    retires the delegate's whole thread (incl. the `delegate_prompt`
    seed) on hand-back."""
    status = await conn.execute(
        "UPDATE session_history SET incumbent = false "
        "WHERE session_id = $1 AND agent_id = $2 AND incumbent = true",
        session_id,
        agent_id,
    )
    return int(status.rsplit(" ", 1)[-1]) if status else 0


# ---------------------------------------------------------------------------
# user_config
# ---------------------------------------------------------------------------


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
    full_name: str = "",
    timezone: str = "UTC",
    preset_pro: str = "default",
    preset_balanced: str = "default",
    preset_lite: str = "default",
    preset_embedding: str = "default_embedding",
    max_context_token_limit: int = 120_000,
    verbose_default: bool = False,
    language: str = "en",
    sandbox_uid: int | None = None,
    default_session_id: str | None = None,
    custom_note: str = "",
) -> UserConfigRow:
    """Create a user_config row (defaults seeded from `SuiteSettings` at
    the call site). Idempotent — an existing row is returned unchanged."""
    row = await conn.fetchrow(
        """
        INSERT INTO user_config (
            user_id, full_name, timezone,
            preset_pro, preset_balanced, preset_lite, preset_embedding,
            max_context_token_limit, verbose_default, language,
            sandbox_uid, default_session_id, custom_note
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (user_id) DO NOTHING
        RETURNING *
        """,
        user_id,
        full_name,
        timezone,
        preset_pro,
        preset_balanced,
        preset_lite,
        preset_embedding,
        max_context_token_limit,
        verbose_default,
        language,
        sandbox_uid,
        default_session_id,
        custom_note,
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


_USER_CONFIG_MUTABLE = frozenset(
    {
        "full_name",
        "timezone",
        "preset_pro",
        "preset_balanced",
        "preset_lite",
        "preset_embedding",
        "max_context_token_limit",
        "verbose_default",
        "language",
        "sandbox_uid",
        "default_session_id",
        "custom_note",
    }
)


async def update_user_config(
    conn: asyncpg.Connection, user_id: str, **fields: Any
) -> None:
    """Patch user_config columns (config agent / channel). Column names
    are a fixed allowlist — no injection surface in the SET clause."""
    cols = {k: v for k, v in fields.items() if k in _USER_CONFIG_MUTABLE}
    unknown = set(fields) - _USER_CONFIG_MUTABLE
    if unknown:
        raise ValueError(f"update_user_config: non-mutable columns {sorted(unknown)}")
    if not cols:
        return
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    await conn.execute(
        f"UPDATE user_config SET {set_clause}, updated_at = now() "
        "WHERE user_id = $1",
        user_id,
        *cols.values(),
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


async def upsert_platform_mapping(
    conn: asyncpg.Connection, *, platform: str, chat_id: str, user_id: str
) -> PlatformMappingRow:
    """Bind a channel-native chat to a user (the admin approve-registration
    flow). Re-binding a chat updates the `user_id`."""
    row = await conn.fetchrow(
        """
        INSERT INTO suite_platform_mappings (platform, chat_id, user_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (platform, chat_id) DO UPDATE SET user_id = EXCLUDED.user_id
        RETURNING *
        """,
        platform,
        chat_id,
        user_id,
    )
    return PlatformMappingRow.model_validate(dict(row))


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
