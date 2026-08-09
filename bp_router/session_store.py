"""bp_router.session_store — the router-managed conversation log + session state.

Implements `docs/design/router-managed-session-store.md`. One module backs
BOTH surfaces (the `SessionOp` WS frame and the session-JWT HTTP endpoints)
so they cannot drift, exactly as `file_store` backs the file frames and
`/v1/files/names`.

The invariant everything else hangs off (§7): **a message is written only by
the agent whose thread it lands in.** `StoreScope.agent_id` is derived by the
caller from the task's active executor and is the only source of
`owner_agent_id` on a write — no op carries a writable owner field, so
writing in another agent's name is unrepresentable rather than rejected. A
steward (HTTP, `agent_id=None`) can read, set session-scoped state, enqueue
hand-overs, and take the turn lease; it can never append.

`execute_batch` runs an ordered op list inside a transaction the CALLER owns
(§6.1): reads observe the writes that precede them, and any refusal raises
`SessionStoreError`, which rolls the whole batch back. That is what makes a
summarize-apply (`SetState` + `SetFloor`) atomic without the router knowing
what a summary is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bp_protocol.frames import (
    AcquireLeaseOp,
    AppendOp,
    AssertThreadOp,
    ConsumeHandoversOp,
    GetStateOp,
    HandoverItem,
    HandOverOp,
    LeaseStatus,
    ListThreadsOp,
    ReadOp,
    RedactOp,
    ReleaseLeaseOp,
    RenewLeaseOp,
    SessionMessage,
    SessionOpResult,
    SetFloorOp,
    SetStateOp,
    StateValue,
    StatThreadOp,
    ThreadStat,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

logger = logging.getLogger(__name__)


# Conservative default budget for one `Read` reply: the router fills
# newest-first up to this many bytes so the most recent turns always
# survive truncation (§9.2). Callers pass the negotiated payload cap;
# this is the fraction of it a single read may consume, leaving room for
# the rest of the batch's results plus frame overhead.
READ_BUDGET_FRACTION = 0.6

# Hard ceiling on one message's stored text (§9.4). Large payloads belong
# in the file stash, referenced by name from `metadata` — the store must
# not become a second blob path.
MAX_CONTENT_BYTES = 256 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


class SessionStoreError(Exception):
    """A refused op. `code` is the wire error (§5.3); `index` is the op's
    position in the batch, so the caller can tell the agent WHICH op
    failed. Raising rolls back the whole batch — there are no partial
    applications."""

    def __init__(self, code: str, index: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.index = index


@dataclass
class StoreScope:
    """Authoritative scope for a batch. Every field is DERIVED by the
    caller — from the task row (frames) or the session JWT (HTTP) — and
    never taken from the request body.

    `session_id is None` selects the `user` scope: cross-session,
    user-wide threads that survive session purge (§3.1).

    `agent_id is None` marks a steward: no thread-writing authority.
    """

    user_id: str
    session_id: str | None
    agent_id: str | None


@dataclass
class LeasePromotion:
    """A waiter that just became the holder. Returned from `execute_batch`
    so the transport layer can push `SessionLease` after the transaction
    commits — never inside it, or a rollback would announce a lease that
    does not exist."""

    agent_id: str
    session_id: str
    ticket: int
    holder_expires_at: datetime | None = None


@dataclass
class BatchOutcome:
    results: list[SessionOpResult] = field(default_factory=list)
    promotions: list[LeasePromotion] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scope predicates
#
# Session- and user-scoped rows live in the same tables, distinguished by a
# NULL `session_id`. NULL never equals itself in SQL, so every lookup needs
# the right form of the predicate — hence one helper rather than an `=` that
# silently matches nothing.
# ---------------------------------------------------------------------------


def _scope_sql(scope: StoreScope, *, start: int) -> tuple[str, list[Any]]:
    if scope.session_id is None:
        return (f"user_id = ${start} AND session_id IS NULL", [scope.user_id])
    return (
        f"user_id = ${start} AND session_id = ${start + 1}",
        [scope.user_id, scope.session_id],
    )


def _require_agent(scope: StoreScope, index: int) -> str:
    """Thread-writing ops need a derived owner. A steward has none — and
    that is the whole point: the HTTP surface has no way to append."""
    if scope.agent_id is None:
        raise SessionStoreError("denied", index)
    return scope.agent_id


def _resolve_owner(scope: StoreScope, requested: str | None, index: int) -> str:
    """Reads may name any owner in the session (reads are session-scoped —
    reading cannot fabricate). A steward MUST name one, since it has no
    identity of its own to default to."""
    if requested is not None:
        return requested
    if scope.agent_id is None:
        raise SessionStoreError("denied", index)
    return scope.agent_id


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


async def _ensure_thread(
    conn: asyncpg.Connection, scope: StoreScope, owner: str, thread_key: str
) -> dict[str, Any]:
    """Get the thread row, creating it on first use, locked FOR UPDATE.

    The lock serialises concurrent appends to one thread within their
    transactions, which is what keeps the denormalised counters and
    `last_message_id` consistent under the concurrency the store
    deliberately permits (§6.2)."""
    where, args = _scope_sql(scope, start=1)
    n = len(args)
    row = await conn.fetchrow(
        f"""
        SELECT * FROM session_threads
        WHERE {where} AND owner_agent_id = ${n + 1} AND thread_key = ${n + 2}
        FOR UPDATE
        """,
        *args,
        owner,
        thread_key,
    )
    if row is not None:
        return dict(row)
    await conn.execute(
        """
        INSERT INTO session_threads
            (user_id, session_id, owner_agent_id, thread_key)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        scope.user_id,
        scope.session_id,
        owner,
        thread_key,
    )
    row = await conn.fetchrow(
        f"""
        SELECT * FROM session_threads
        WHERE {where} AND owner_agent_id = ${n + 1} AND thread_key = ${n + 2}
        FOR UPDATE
        """,
        *args,
        owner,
        thread_key,
    )
    if row is None:  # pragma: no cover - insert+select under one txn
        raise SessionStoreError("denied")
    return dict(row)


async def _thread_row(
    conn: asyncpg.Connection, scope: StoreScope, owner: str, thread_key: str
) -> dict[str, Any] | None:
    where, args = _scope_sql(scope, start=1)
    n = len(args)
    row = await conn.fetchrow(
        f"""
        SELECT * FROM session_threads
        WHERE {where} AND owner_agent_id = ${n + 1} AND thread_key = ${n + 2}
        """,
        *args,
        owner,
        thread_key,
    )
    return dict(row) if row else None


def _stat_from_row(row: dict[str, Any] | None, owner: str, thread_key: str) -> ThreadStat:
    if row is None:
        return ThreadStat(owner_agent_id=owner, thread_key=thread_key)
    return ThreadStat(
        owner_agent_id=row["owner_agent_id"],
        thread_key=row["thread_key"],
        message_count=row["message_count"],
        content_bytes=row["content_bytes"],
        last_message_id=row["last_message_id"],
        floor_id=row["floor_id"],
    )


async def _recount_thread(
    conn: asyncpg.Connection, scope: StoreScope, owner: str, thread_key: str
) -> None:
    """Recompute a thread's above-floor counters with one aggregate.

    Appends and redactions adjust by delta; a floor move cannot (it
    retires an arbitrary prefix), so it pays for an aggregate over the
    thread instead. Floor moves are rare — one per summarization — which
    is the trade the denormalised counters are chosen for (§4)."""
    where, args = _scope_sql(scope, start=1)
    n = len(args)
    agg = await conn.fetchrow(
        f"""
        SELECT count(*) AS cnt,
               COALESCE(sum(octet_length(content)), 0) AS bytes,
               COALESCE(max(id), 0) AS last_id
        FROM session_messages
        WHERE {where} AND owner_agent_id = ${n + 1} AND thread_key = ${n + 2}
          AND id > (
              SELECT floor_id FROM session_threads
              WHERE {where} AND owner_agent_id = ${n + 1} AND thread_key = ${n + 2}
          )
        """,
        *args,
        owner,
        thread_key,
    )
    await conn.execute(
        f"""
        UPDATE session_threads
        SET message_count = ${n + 3}, content_bytes = ${n + 4}, updated_at = now()
        WHERE {where} AND owner_agent_id = ${n + 1} AND thread_key = ${n + 2}
        """,
        *args,
        owner,
        thread_key,
        int(agg["cnt"]) if agg else 0,
        int(agg["bytes"]) if agg else 0,
    )


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


async def user_content_bytes(conn: asyncpg.Connection, user_id: str) -> int:
    row = await conn.fetchrow(
        "SELECT COALESCE(sum(content_bytes), 0) AS b FROM session_threads WHERE user_id = $1",
        user_id,
    )
    return int(row["b"]) if row else 0


# ---------------------------------------------------------------------------
# Op handlers
# ---------------------------------------------------------------------------


async def _op_append(
    conn: asyncpg.Connection,
    scope: StoreScope,
    op: AppendOp,
    index: int,
    *,
    task_id: str | None,
    quota_ceiling: int | None,
) -> SessionOpResult:
    owner = _require_agent(scope, index)
    size = len(op.content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise SessionStoreError("content_too_large", index)

    # Idempotent append: a redelivered task must not double-write. Look the
    # prior row up FIRST so the caller gets the original id back rather than
    # a unique-violation it would have to interpret.
    if op.idempotency_key is not None and task_id is not None:
        prior = await conn.fetchrow(
            """
            SELECT id FROM session_messages
            WHERE task_id = $1 AND idempotency_key = $2
            """,
            task_id,
            op.idempotency_key,
        )
        if prior is not None:
            return SessionOpResult(kind=op.kind, message_id=prior["id"])

    if quota_ceiling is not None:
        usage = await user_content_bytes(conn, scope.user_id)
        if usage + size > quota_ceiling:
            raise SessionStoreError("quota_exceeded", index)

    thread = await _ensure_thread(conn, scope, owner, op.thread_key)
    row = await conn.fetchrow(
        """
        INSERT INTO session_messages
            (user_id, session_id, owner_agent_id, thread_key, role, content,
             hidden, metadata, task_id, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        scope.user_id,
        scope.session_id,
        owner,
        op.thread_key,
        op.role,
        op.content,
        op.hidden,
        op.metadata,
        task_id,
        op.idempotency_key,
    )
    message_id = int(row["id"])
    # The new row is above the floor by construction (ids are monotonic and
    # the floor only ever moves up), so a delta beats a recount here.
    await conn.execute(
        """
        UPDATE session_threads
        SET message_count = message_count + 1,
            content_bytes = content_bytes + $1,
            last_message_id = $2,
            updated_at = now()
        WHERE user_id = $3 AND session_id IS NOT DISTINCT FROM $4
          AND owner_agent_id = $5 AND thread_key = $6
        """,
        size,
        message_id,
        scope.user_id,
        scope.session_id,
        owner,
        op.thread_key,
    )
    _ = thread
    return SessionOpResult(kind=op.kind, message_id=message_id)


async def _op_set_floor(
    conn: asyncpg.Connection, scope: StoreScope, op: SetFloorOp, index: int
) -> SessionOpResult:
    owner = _require_agent(scope, index)
    thread = await _ensure_thread(conn, scope, owner, op.thread_key)
    if op.floor_id < thread["floor_id"]:
        # Monotonic by contract: a stale writer must never un-retire rows a
        # newer one folded into a summary (§6.3).
        raise SessionStoreError("floor_regression", index)
    await conn.execute(
        """
        UPDATE session_threads SET floor_id = $1, updated_at = now()
        WHERE user_id = $2 AND session_id IS NOT DISTINCT FROM $3
          AND owner_agent_id = $4 AND thread_key = $5
        """,
        op.floor_id,
        scope.user_id,
        scope.session_id,
        owner,
        op.thread_key,
    )
    await _recount_thread(conn, scope, owner, op.thread_key)
    return SessionOpResult(kind=op.kind, affected=1)


async def _op_set_state(
    conn: asyncpg.Connection, scope: StoreScope, op: SetStateOp, index: int
) -> SessionOpResult:
    if op.session_scoped:
        owner: str | None = None
        thread_key = ""
    else:
        owner = _require_agent(scope, index)
        thread_key = op.thread_key

    where, args = _scope_sql(scope, start=1)
    n = len(args)
    owner_pred = (
        f"owner_agent_id = ${n + 1}" if owner is not None else "owner_agent_id IS NULL"
    )
    key_args = [*args] + ([owner] if owner is not None else [])
    k = len(key_args)
    current = await conn.fetchrow(
        f"""
        SELECT version FROM session_state
        WHERE {where} AND {owner_pred} AND thread_key = ${k + 1} AND key = ${k + 2}
        FOR UPDATE
        """,
        *key_args,
        thread_key,
        op.key,
    )
    current_version = int(current["version"]) if current else 0
    if op.expected_version is not None and op.expected_version != current_version:
        # The CAS that makes two concurrent summarize passes safe: the loser
        # fails here, and because the batch is atomic its floor move rolls
        # back with it (§6.3).
        raise SessionStoreError("version_conflict", index)

    if op.value is None:
        await conn.execute(
            f"""
            DELETE FROM session_state
            WHERE {where} AND {owner_pred} AND thread_key = ${k + 1} AND key = ${k + 2}
            """,
            *key_args,
            thread_key,
            op.key,
        )
        return SessionOpResult(kind=op.kind, affected=1 if current else 0)

    if current is None:
        await conn.execute(
            """
            INSERT INTO session_state
                (user_id, session_id, owner_agent_id, thread_key, key, value,
                 version, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, 1, $7)
            """,
            scope.user_id,
            scope.session_id,
            owner,
            thread_key,
            op.key,
            op.value,
            op.metadata,
        )
        new_version = 1
    else:
        new_version = current_version + 1
        await conn.execute(
            f"""
            UPDATE session_state
            SET value = ${k + 3}, version = ${k + 4}, metadata = ${k + 5},
                updated_at = now()
            WHERE {where} AND {owner_pred} AND thread_key = ${k + 1} AND key = ${k + 2}
            """,
            *key_args,
            thread_key,
            op.key,
            op.value,
            new_version,
            op.metadata,
        )
    return SessionOpResult(
        kind=op.kind,
        state=[StateValue(key=op.key, value=op.value, version=new_version)],
    )


async def _op_redact(
    conn: asyncpg.Connection, scope: StoreScope, op: RedactOp, index: int
) -> SessionOpResult:
    owner = _require_agent(scope, index)
    if not op.message_ids:
        return SessionOpResult(kind=op.kind, affected=0)
    where, args = _scope_sql(scope, start=1)
    n = len(args)
    # Content is BLANKED, not flagged: a user-facing delete has to be honest
    # at the storage layer. The id survives so floors and cursors stay valid.
    rows = await conn.fetch(
        f"""
        UPDATE session_messages
        SET content = '', redacted_at = now()
        WHERE {where} AND owner_agent_id = ${n + 1}
          AND id = ANY(${n + 2}::bigint[]) AND redacted_at IS NULL
        RETURNING thread_key
        """,
        *args,
        owner,
        op.message_ids,
    )
    for thread_key in {r["thread_key"] for r in rows}:
        await _recount_thread(conn, scope, owner, thread_key)
    return SessionOpResult(kind=op.kind, affected=len(rows))


async def _op_hand_over(
    conn: asyncpg.Connection, scope: StoreScope, op: HandOverOp, index: int
) -> SessionOpResult:
    if not op.target_agent_id:
        raise SessionStoreError("denied", index)
    row = await conn.fetchrow(
        """
        INSERT INTO session_handovers
            (user_id, session_id, target_agent_id, thread_key, from_agent_id,
             item_kind, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        scope.user_id,
        scope.session_id,
        op.target_agent_id,
        op.thread_key,
        scope.agent_id,
        op.item_kind,
        op.payload,
    )
    return SessionOpResult(kind=op.kind, message_id=int(row["id"]))


async def _op_consume_handovers(
    conn: asyncpg.Connection,
    scope: StoreScope,
    op: ConsumeHandoversOp,
    index: int,
    *,
    task_id: str | None,
) -> SessionOpResult:
    # Only the TARGET drains its own queue — a steward can enqueue but never
    # consume, which is what keeps materialisation the owner's act (§3.5).
    owner = _require_agent(scope, index)
    where, args = _scope_sql(scope, start=1)
    n = len(args)
    kind_pred = ""
    extra: list[Any] = []
    if op.item_kinds:
        kind_pred = f"AND item_kind = ANY(${n + 2}::text[])"
        extra.append(op.item_kinds)
    limit_pos = n + 2 + len(extra)
    rows = await conn.fetch(
        f"""
        UPDATE session_handovers SET consumed_at = now(), consumed_by_task_id = ${limit_pos + 1}
        WHERE id IN (
            SELECT id FROM session_handovers
            WHERE {where} AND target_agent_id = ${n + 1} AND consumed_at IS NULL
              {kind_pred}
            ORDER BY id
            LIMIT ${limit_pos}
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, item_kind, thread_key, payload, created_at
        """,
        *args,
        owner,
        *extra,
        op.limit,
        task_id,
    )
    items = [
        HandoverItem(
            id=r["id"],
            item_kind=r["item_kind"],
            thread_key=r["thread_key"],
            payload=r["payload"] or {},
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return SessionOpResult(kind=op.kind, items=items)


async def _op_assert_thread(
    conn: asyncpg.Connection, scope: StoreScope, op: AssertThreadOp, index: int
) -> SessionOpResult:
    owner = _resolve_owner(scope, op.owner_agent_id, index)
    row = await _thread_row(conn, scope, owner, op.thread_key)
    actual = int(row["last_message_id"]) if row else 0
    if actual != op.last_message_id:
        # The thread moved under the caller: another turn appended between
        # its read and this write. Refusing the batch is the whole point —
        # an interleaved append becomes a detected conflict (§6.3).
        raise SessionStoreError("thread_conflict", index)
    return SessionOpResult(kind=op.kind, last_message_id=actual)


async def _op_read(
    conn: asyncpg.Connection,
    scope: StoreScope,
    op: ReadOp,
    index: int,
    *,
    budget_bytes: int,
) -> SessionOpResult:
    owner = _resolve_owner(scope, op.owner_agent_id, index)
    thread = await _thread_row(conn, scope, owner, op.thread_key)
    floor = 0 if op.include_retired else int(thread["floor_id"]) if thread else 0

    where, args = _scope_sql(scope, start=1)
    conds = [where, f"owner_agent_id = ${len(args) + 1}", f"thread_key = ${len(args) + 2}"]
    params: list[Any] = [*args, owner, op.thread_key]
    if floor:
        params.append(floor)
        conds.append(f"id > ${len(params)}")
    if not op.include_retired:
        conds.append("redacted_at IS NULL")
    if not op.include_hidden:
        conds.append("hidden = false")
    if op.roles is not None:
        params.append(op.roles)
        conds.append(f"role = ANY(${len(params)}::text[])")
    if op.since_id is not None:
        params.append(op.since_id)
        conds.append(f"id > ${len(params)}")
    if op.before_id is not None:
        params.append(op.before_id)
        conds.append(f"id < ${len(params)}")
    predicate = " AND ".join(conds)

    # The predicate's params stand alone: the pagination probe below reuses
    # the predicate but NOT the limit/budget placeholders, and asyncpg
    # rejects a query handed more arguments than it references.
    pred_params = list(params)
    budget = min(op.max_bytes or budget_bytes, budget_bytes)
    params.append(op.limit)
    limit_pos = len(params)
    params.append(budget)
    budget_pos = len(params)
    # Fill newest-first under the budget, then re-order for the caller: the
    # most recent turns must survive truncation, and a single oversized row
    # still comes back (rn = 1) rather than an empty window (§9.2).
    rows = await conn.fetch(
        f"""
        SELECT id, owner_agent_id, thread_key, role, content, hidden, metadata,
               redacted_at, created_at
        FROM (
            SELECT *,
                   sum(octet_length(content)) OVER (ORDER BY id DESC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running,
                   row_number() OVER (ORDER BY id DESC) AS rn
            FROM session_messages
            WHERE {predicate}
            ORDER BY id DESC
            LIMIT ${limit_pos}
        ) w
        WHERE w.running <= ${budget_pos} OR w.rn = 1
        ORDER BY id ASC
        """,
        *params,
    )
    messages = [
        SessionMessage(
            id=r["id"],
            owner_agent_id=r["owner_agent_id"],
            thread_key=r["thread_key"],
            role=r["role"],
            content=r["content"],
            hidden=r["hidden"],
            metadata=r["metadata"] or {},
            redacted=r["redacted_at"] is not None,
            created_at=r["created_at"],
        )
        for r in rows
    ]
    if op.order == "desc":
        messages.reverse()

    truncated_before_id: int | None = None
    if messages:
        oldest = min(m.id for m in messages)
        more = await conn.fetchrow(
            f"""
            SELECT 1 FROM session_messages
            WHERE {predicate} AND id < ${len(pred_params) + 1}
            LIMIT 1
            """,
            *pred_params,
            oldest,
        )
        if more is not None:
            truncated_before_id = oldest

    return SessionOpResult(
        kind=op.kind,
        messages=messages,
        last_message_id=int(thread["last_message_id"]) if thread else 0,
        truncated_before_id=truncated_before_id,
    )


async def _op_get_state(
    conn: asyncpg.Connection, scope: StoreScope, op: GetStateOp, index: int
) -> SessionOpResult:
    where, args = _scope_sql(scope, start=1)
    n = len(args)
    params: list[Any] = [*args]
    if op.session_scoped:
        owner_pred = "owner_agent_id IS NULL"
    elif op.owner_agent_id == "*":
        owner_pred = "owner_agent_id IS NOT NULL"
    else:
        owner = _resolve_owner(scope, op.owner_agent_id, index)
        params.append(owner)
        owner_pred = f"owner_agent_id = ${len(params)}"
        params.append(op.thread_key)
        owner_pred += f" AND thread_key = ${len(params)}"
    key_pred = ""
    if op.keys is not None:
        params.append(op.keys)
        key_pred = f"AND key = ANY(${len(params)}::text[])"
    rows = await conn.fetch(
        f"""
        SELECT key, value, version, metadata FROM session_state
        WHERE {where} AND {owner_pred} {key_pred}
        ORDER BY key
        """,
        *params,
    )
    _ = n
    return SessionOpResult(
        kind=op.kind,
        state=[
            StateValue(
                key=r["key"],
                value=r["value"],
                version=r["version"],
                metadata=r["metadata"] or {},
            )
            for r in rows
        ],
    )


async def _op_stat_thread(
    conn: asyncpg.Connection, scope: StoreScope, op: StatThreadOp, index: int
) -> SessionOpResult:
    owner = _resolve_owner(scope, op.owner_agent_id, index)
    row = await _thread_row(conn, scope, owner, op.thread_key)
    return SessionOpResult(kind=op.kind, stat=_stat_from_row(row, owner, op.thread_key))


async def _op_list_threads(
    conn: asyncpg.Connection, scope: StoreScope, op: ListThreadsOp, index: int
) -> SessionOpResult:
    where, args = _scope_sql(scope, start=1)
    params: list[Any] = [*args]
    owner_pred = ""
    if op.owner_agent_id is not None:
        params.append(op.owner_agent_id)
        owner_pred = f"AND owner_agent_id = ${len(params)}"
    rows = await conn.fetch(
        f"""
        SELECT * FROM session_threads
        WHERE {where} {owner_pred}
        ORDER BY owner_agent_id, thread_key
        """,
        *params,
    )
    _ = index
    return SessionOpResult(
        kind=op.kind,
        threads=[_stat_from_row(dict(r), r["owner_agent_id"], r["thread_key"]) for r in rows],
    )


# ---------------------------------------------------------------------------
# Turn lease (§6.4)
# ---------------------------------------------------------------------------


async def _expire_stale(conn: asyncpg.Connection, session_id: str) -> None:
    """Drop timed-out holders AND waiters. Called on every lease op, which
    is what makes promotion lazy: no background sweep exists to fall
    behind, and an idle session costs nothing."""
    await conn.execute(
        "DELETE FROM session_turn_queue WHERE session_id = $1 AND expires_at < now()",
        session_id,
    )


async def _promote(conn: asyncpg.Connection, session_id: str) -> dict[str, Any] | None:
    """Make the lowest waiting ticket active, if no live holder remains.

    Ordering comes from the ticket sequence, not from retry timing — which
    is why two messages that arrived in order cannot be answered out of
    order by a backoff loop (§6.4)."""
    row = await conn.fetchrow(
        """
        UPDATE session_turn_queue SET state = 'active',
               expires_at = greatest(expires_at, now() + interval '30 seconds')
        WHERE ticket = (
            SELECT ticket FROM session_turn_queue
            WHERE session_id = $1 AND state = 'waiting'
            ORDER BY ticket
            LIMIT 1
        )
        AND NOT EXISTS (
            SELECT 1 FROM session_turn_queue
            WHERE session_id = $1 AND state = 'active'
        )
        RETURNING ticket, agent_id, holder_id, expires_at
        """,
        session_id,
    )
    return dict(row) if row else None


async def _op_acquire_lease(
    conn: asyncpg.Connection, scope: StoreScope, op: AcquireLeaseOp, index: int
) -> tuple[SessionOpResult, LeasePromotion | None]:
    if scope.session_id is None:
        # A lease orders turns within one conversation; there is no
        # meaningful user-scope equivalent.
        raise SessionStoreError("denied", index)
    session_id = scope.session_id
    await _expire_stale(conn, session_id)
    expires = _now() + timedelta(milliseconds=op.ttl_ms)
    # Re-acquiring with the same holder_id keeps the original ticket, so a
    # retry never loses its place in line.
    row = await conn.fetchrow(
        """
        INSERT INTO session_turn_queue
            (session_id, user_id, holder_id, agent_id, state, expires_at)
        VALUES ($1, $2, $3, $4, 'waiting', $5)
        ON CONFLICT (session_id, holder_id) DO UPDATE
            SET expires_at = EXCLUDED.expires_at
        RETURNING ticket, state
        """,
        session_id,
        scope.user_id,
        op.holder_id,
        scope.agent_id or "",
        expires,
    )
    my_ticket = int(row["ticket"])
    if row["state"] == "active":
        return (
            SessionOpResult(
                kind=op.kind,
                lease=LeaseStatus(granted=True, ticket=my_ticket, holder_expires_at=expires),
            ),
            None,
        )

    promoted = await _promote(conn, session_id)
    if promoted is not None and int(promoted["ticket"]) == my_ticket:
        await conn.execute(
            "UPDATE session_turn_queue SET expires_at = $2 WHERE ticket = $1",
            my_ticket,
            expires,
        )
        return (
            SessionOpResult(
                kind=op.kind,
                lease=LeaseStatus(granted=True, ticket=my_ticket, holder_expires_at=expires),
            ),
            None,
        )

    holder = await conn.fetchrow(
        "SELECT expires_at FROM session_turn_queue WHERE session_id = $1 AND state = 'active'",
        session_id,
    )
    promotion = (
        LeasePromotion(
            agent_id=promoted["agent_id"],
            session_id=session_id,
            ticket=int(promoted["ticket"]),
            holder_expires_at=promoted["expires_at"],
        )
        if promoted is not None and promoted["agent_id"]
        else None
    )
    return (
        SessionOpResult(
            kind=op.kind,
            lease=LeaseStatus(
                granted=False,
                ticket=my_ticket,
                # The waiter arms its own timer on this: a holder that dies
                # without releasing leaves nobody to promote (§6.4).
                holder_expires_at=holder["expires_at"] if holder else None,
            ),
        ),
        promotion,
    )


async def _op_renew_lease(
    conn: asyncpg.Connection, scope: StoreScope, op: RenewLeaseOp, index: int
) -> SessionOpResult:
    if scope.session_id is None:
        raise SessionStoreError("denied", index)
    expires = _now() + timedelta(milliseconds=op.ttl_ms)
    row = await conn.fetchrow(
        """
        UPDATE session_turn_queue SET expires_at = $3
        WHERE session_id = $1 AND holder_id = $2 AND state = 'active'
          AND expires_at > now()
        RETURNING ticket
        """,
        scope.session_id,
        op.holder_id,
        expires,
    )
    if row is None:
        # The lease expired under a long turn. The caller is told so it can
        # decide; its AssertThread ops are what keep the writes safe
        # meanwhile (§6.3).
        raise SessionStoreError("lease_lost", index)
    return SessionOpResult(
        kind=op.kind,
        lease=LeaseStatus(granted=True, ticket=int(row["ticket"]), holder_expires_at=expires),
    )


async def _op_release_lease(
    conn: asyncpg.Connection, scope: StoreScope, op: ReleaseLeaseOp, index: int
) -> tuple[SessionOpResult, LeasePromotion | None]:
    if scope.session_id is None:
        raise SessionStoreError("denied", index)
    session_id = scope.session_id
    await conn.execute(
        "DELETE FROM session_turn_queue WHERE session_id = $1 AND holder_id = $2",
        session_id,
        op.holder_id,
    )
    await _expire_stale(conn, session_id)
    promoted = await _promote(conn, session_id)
    promotion = (
        LeasePromotion(
            agent_id=promoted["agent_id"],
            session_id=session_id,
            ticket=int(promoted["ticket"]),
            holder_expires_at=promoted["expires_at"],
        )
        if promoted is not None and promoted["agent_id"]
        else None
    )
    return SessionOpResult(kind=op.kind, affected=1), promotion


# ---------------------------------------------------------------------------
# Batch executor
# ---------------------------------------------------------------------------


async def execute_batch(
    conn: asyncpg.Connection,
    scope: StoreScope,
    ops: list[Any],
    *,
    task_id: str | None = None,
    budget_bytes: int = 600_000,
    quota_ceiling: int | None = None,
) -> BatchOutcome:
    """Apply `ops` in order. The CALLER owns the transaction — wrap this in
    `async with conn.transaction():` so a `SessionStoreError` rolls the
    whole batch back.

    Returns the positional results plus any lease promotions the caller
    must push AFTER the commit."""
    outcome = BatchOutcome()
    for index, op in enumerate(ops):
        if isinstance(op, AppendOp):
            res = await _op_append(
                conn, scope, op, index, task_id=task_id, quota_ceiling=quota_ceiling
            )
        elif isinstance(op, SetFloorOp):
            res = await _op_set_floor(conn, scope, op, index)
        elif isinstance(op, SetStateOp):
            res = await _op_set_state(conn, scope, op, index)
        elif isinstance(op, RedactOp):
            res = await _op_redact(conn, scope, op, index)
        elif isinstance(op, HandOverOp):
            res = await _op_hand_over(conn, scope, op, index)
        elif isinstance(op, ConsumeHandoversOp):
            res = await _op_consume_handovers(conn, scope, op, index, task_id=task_id)
        elif isinstance(op, AssertThreadOp):
            res = await _op_assert_thread(conn, scope, op, index)
        elif isinstance(op, ReadOp):
            res = await _op_read(conn, scope, op, index, budget_bytes=budget_bytes)
        elif isinstance(op, GetStateOp):
            res = await _op_get_state(conn, scope, op, index)
        elif isinstance(op, StatThreadOp):
            res = await _op_stat_thread(conn, scope, op, index)
        elif isinstance(op, ListThreadsOp):
            res = await _op_list_threads(conn, scope, op, index)
        elif isinstance(op, AcquireLeaseOp):
            res, promo = await _op_acquire_lease(conn, scope, op, index)
            if promo is not None:
                outcome.promotions.append(promo)
        elif isinstance(op, RenewLeaseOp):
            res = await _op_renew_lease(conn, scope, op, index)
        elif isinstance(op, ReleaseLeaseOp):
            res, promo = await _op_release_lease(conn, scope, op, index)
            if promo is not None:
                outcome.promotions.append(promo)
        else:  # pragma: no cover - the union is closed; a new op lands here
            raise SessionStoreError("unsupported_op", index)
        outcome.results.append(res)
    return outcome
