"""bp_router.db.queries — Query helpers with the user_id scoping invariant.

EVERY read of a user-owned table goes through `Scope.user(user_id)`. The
returned wrapper enforces `WHERE user_id = $1` on its method's queries.
CI greps for `SELECT|UPDATE|DELETE` patterns in this module that are not
behind `Scope` to catch invariant violations.

See `docs/security.md` §8 for the data isolation rationale.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bp_protocol.types import TaskPriority, TaskState
from bp_router.db.models import (
    AgentRow,
    FileNameRow,
    FileRow,
    InvitationRow,
    LlmPresetRow,
    McpServerRow,
    PendingRegistrationRow,
    SessionRow,
    TaskEventRow,
    TaskRow,
    UserRow,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_urlsafe(16)}"


def _like_escape(s: str) -> str:
    r"""Escape the SQL `LIKE` metacharacters (`%`, `_`) and the escape
    character itself so `s` matches LITERALLY under `LIKE … ESCAPE
    '\'`. Escape `\` FIRST, so the escapes added for `%`/`_` aren't
    themselves double-escaped."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Maximum depth bound on recursive task-tree walks. The CTEs in
# `list_descendants` and `task_has_ancestor_with_agent` use this to
# guarantee termination even if a malformed `parent_task_id` chain
# forms a cycle (which the FK doesn't prevent — it only checks
# existence). 64 is comfortably above any realistic spawn depth and
# bounded enough that a malicious chain can't pin Postgres
# work-mem trying to enumerate the loop. See
# `docs/security.md` §8.
_MAX_TASK_TREE_DEPTH = 64


class CrossUserTaskAccess(Exception):
    """Raised when a user-scoped operation references a task owned by
    a different user.

    The most consequential trigger is `Scope.create_task` receiving
    a `parent_task_id` that exists but belongs to another user — the
    FK on `tasks.parent_task_id` only checks existence, so without
    this guard the row would land with a foreign parent pointer
    (privacy + tree-integrity violation).

    Caller sites translate this to the appropriate user-facing error
    code (`AdmitError("invalid_parent_task", ...)` for `admit_task`,
    HTTP 403 / 404 for admin endpoints).
    """

    def __init__(self, task_id: str, message: str = "") -> None:
        super().__init__(message or f"task {task_id!r} not accessible to current user")
        self.task_id = task_id


# ---------------------------------------------------------------------------
# Scope wrapper — enforces WHERE user_id = $current_user
# ---------------------------------------------------------------------------


class Scope:
    """Data-access wrapper bound to a single user_id.

    Use as `await Scope.user(conn, user_id).get_session(session_id)`. The
    wrapper's queries always include `user_id = $X` in the WHERE clause.
    Callers that must read across users (admin endpoints) use `Scope.admin()`.
    """

    def __init__(self, conn: asyncpg.Connection, user_id: str | None) -> None:
        self._conn = conn
        self._user_id = user_id

    @classmethod
    def user(cls, conn: asyncpg.Connection, user_id: str) -> Scope:
        return cls(conn, user_id)

    @classmethod
    def admin(cls, conn: asyncpg.Connection) -> Scope:
        """Cross-user reads. Reserved for endpoints under `admin` role."""
        return cls(conn, None)

    @property
    def is_admin(self) -> bool:
        return self._user_id is None

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def _require_user(self) -> str:
        if self._user_id is None:
            raise RuntimeError("user_id-scoped query attempted in admin scope")
        return self._user_id

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def open_session(
        self, *, metadata: dict[str, Any] | None = None
    ) -> SessionRow:
        user_id = self._require_user()
        session_id = _new_id("ses_")
        row = await self._conn.fetchrow(
            """
            INSERT INTO sessions (session_id, user_id, opened_at, metadata)
            VALUES ($1, $2, $3, $4)
            RETURNING session_id, user_id, opened_at, closed_at, metadata
            """,
            session_id,
            user_id,
            _now(),
            metadata or {},
        )
        return SessionRow.model_validate(dict(row))

    async def close_session(self, session_id: str) -> None:
        user_id = self._require_user()
        await self._conn.execute(
            """
            UPDATE sessions
            SET closed_at = $3
            WHERE session_id = $1 AND user_id = $2 AND closed_at IS NULL
            """,
            session_id,
            user_id,
            _now(),
        )

    async def reopen_session(self, session_id: str) -> bool:
        """Clear `closed_at` so the session re-admits task injection
        (`admit_task` gates on `closed_at IS NULL`). History and metadata are
        retained; cancelled tasks and GC'd file-name rows are NOT restored.
        Idempotent: a no-op on an already-open session. Returns True iff a
        closed row was actually transitioned to open."""
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            UPDATE sessions
            SET closed_at = NULL
            WHERE session_id = $1 AND user_id = $2 AND closed_at IS NOT NULL
            RETURNING session_id
            """,
            session_id,
            user_id,
        )
        return row is not None

    async def get_session(self, session_id: str) -> SessionRow | None:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            SELECT session_id, user_id, opened_at, closed_at, metadata
            FROM sessions
            WHERE session_id = $1 AND user_id = $2
            """,
            session_id,
            user_id,
        )
        return SessionRow.model_validate(dict(row)) if row else None

    async def list_sessions(self, *, limit: int = 50) -> list[SessionRow]:
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            SELECT session_id, user_id, opened_at, closed_at, metadata
            FROM sessions
            WHERE user_id = $1
            ORDER BY opened_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
        return [SessionRow.model_validate(dict(r)) for r in rows]

    async def update_session_metadata(
        self,
        session_id: str,
        *,
        merge: dict[str, Any],
    ) -> SessionRow | None:
        """Shallow-merge `merge` into the session's metadata JSONB via
        Postgres `||` (jsonb concat). User-scoped; silently no-ops if
        `session_id` doesn't belong to the Scope's user.

        IMPORTANT: pass `merge` as a Python dict, NOT `json.dumps`-d.
        The asyncpg pool registers a global jsonb codec
        (`bp_router.db.connection.open_pool`) that maps dict ↔ jsonb.
        Pre-serialising to a string and binding to `$N::jsonb` double-
        encodes — Postgres stores the value as a jsonb STRING rather
        than the intended jsonb OBJECT, and `metadata || string`
        yields a list rather than a merged dict.
        """
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            UPDATE sessions
            SET metadata = metadata || $3::jsonb
            WHERE session_id = $1 AND user_id = $2
            RETURNING session_id, user_id, opened_at, closed_at, metadata
            """,
            session_id,
            user_id,
            merge,
        )
        return SessionRow.model_validate(dict(row)) if row else None

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        session_id: str,
        agent_id: str,
        caller_agent_id: str,
        parent_task_id: str | None,
        priority: TaskPriority,
        deadline: datetime | None,
        idempotency_key: str | None,
        input: dict[str, Any],
        active_agent_id: str | None = None,
    ) -> TaskRow:
        """Create a task row. `active_agent_id` defaults to `agent_id`
        (the destination); delegation flips it later via
        `reassign_active_agent`. `caller_agent_id` is the agent that
        issued the task — Progress and Result frames fan out to this
        id, which works for root tasks too (the previous
        parent_task_id→agent_id walk dropped frames when
        parent_task_id was NULL).
        """
        user_id = self._require_user()
        task_id = _new_id("tsk_")
        # root_task_id propagates from parent or is set to self for new roots.
        root_task_id = task_id
        if parent_task_id is not None:
            row = await self._conn.fetchrow(
                "SELECT root_task_id FROM tasks WHERE task_id = $1 AND user_id = $2",
                parent_task_id,
                user_id,
            )
            if row is None:
                # Two cases collapse to the same outcome:
                #   1. `parent_task_id` doesn't exist anywhere — the FK
                #      below would reject the INSERT regardless, but we
                #      raise here so the caller sees a typed error rather
                #      than a generic asyncpg ForeignKeyViolation.
                #   2. `parent_task_id` exists but belongs to ANOTHER user
                #      — the FK would happily accept the row, leaving an
                #      orphaned cross-user pointer with `root_task_id`
                #      defaulted to self. Privacy + tree-integrity bug.
                # Both cases are caller errors; refuse the create.
                raise CrossUserTaskAccess(parent_task_id)
            root_task_id = row["root_task_id"]
        if active_agent_id is None:
            active_agent_id = agent_id
        now = _now()
        row = await self._conn.fetchrow(
            """
            INSERT INTO tasks (
                task_id, parent_task_id, root_task_id,
                user_id, session_id, agent_id,
                caller_agent_id, active_agent_id,
                state,
                idempotency_key, priority, deadline,
                created_at, updated_at, input
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13, $14)
            RETURNING *
            """,
            task_id,
            parent_task_id,
            root_task_id,
            user_id,
            session_id,
            agent_id,
            caller_agent_id,
            active_agent_id,
            TaskState.QUEUED.value,
            idempotency_key,
            priority.value,
            deadline,
            now,
            input,
        )
        return TaskRow.model_validate(dict(row))

    async def get_task(self, task_id: str) -> TaskRow | None:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM tasks WHERE task_id = $1 AND user_id = $2",
            task_id,
            user_id,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def get_task_for_update(self, task_id: str) -> TaskRow | None:
        """SELECT ... FOR UPDATE — used by `task_transition`.

        The caller MUST hold an open transaction on the underlying
        connection. asyncpg's `conn.transaction()` is the standard way.
        """
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM tasks WHERE task_id = $1 AND user_id = $2 FOR UPDATE",
            task_id,
            user_id,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def list_session_tasks(
        self, session_id: str, *, limit: int = 100
    ) -> list[TaskRow]:
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            SELECT * FROM tasks
            WHERE user_id = $1 AND session_id = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            user_id,
            session_id,
            limit,
        )
        return [TaskRow.model_validate(dict(r)) for r in rows]

    async def find_idempotent(
        self, idempotency_key: str, *, caller_agent_id: str
    ) -> TaskRow | None:
        """Dedup lookup, scoped to `(caller_agent_id, user_id,
        idempotency_key)` — per AGENT per user (matching the
        `tasks_idempotency_unique` constraint). A different caller agent
        reusing the same key string for the same user gets its OWN task,
        never this one's replayed terminal (no cross-agent leak)."""
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            SELECT * FROM tasks
            WHERE caller_agent_id = $1 AND user_id = $2
              AND idempotency_key = $3
            """,
            caller_agent_id,
            user_id,
            idempotency_key,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def lock_task_for_delegation(self, task_id: str) -> TaskRow | None:
        """SELECT ... FOR UPDATE on a task by id, user-scoped. Returns
        None when the task doesn't exist or belongs to another user
        (delegation must run inside the original task's user scope).

        Used by `admit_task` on the delegation branch to serialise the
        active_agent flip against concurrent delegations / cancels on
        the same task_id.
        """
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM tasks WHERE task_id = $1 AND user_id = $2 FOR UPDATE",
            task_id,
            user_id,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def reassign_active_agent(
        self,
        task_id: str,
        *,
        new_active_agent_id: str,
        expected_current_agent_id: str,
    ) -> bool:
        """Atomic flip of `tasks.active_agent_id`, guarded by the
        caller's view of the current active agent. Returns True on
        success, False when the WHERE guard rejected the update
        (another delegation already moved it).
        """
        user_id = self._require_user()
        result = await self._conn.execute(
            "UPDATE tasks SET active_agent_id = $1, updated_at = now() "
            "WHERE task_id = $2 AND user_id = $3 AND active_agent_id = $4",
            new_active_agent_id,
            task_id,
            user_id,
            expected_current_agent_id,
        )
        # asyncpg returns "UPDATE N" — parse the count off the end.
        try:
            updated = int(result.rsplit(" ", 1)[-1])
        except ValueError:
            updated = 0
        return updated == 1

    async def force_fail_task(
        self,
        task_id: str,
        *,
        status_code: int,
        error: dict[str, Any] | None,
    ) -> bool:
        """Single-statement FAILED transition for the degraded
        admission-failure path (`tasks._safe_fail`) when the normal
        `fail_task` itself raised (pool exhausted / DB blip).

        Guarded by `state NOT IN (terminal)` so it can ONLY move a
        still-non-terminal row (a QUEUED row that was created but
        never dispatched) — it can never clobber a task that
        legitimately reached SUCCEEDED/FAILED/CANCELLED/TIMED_OUT.
        Returns True iff a row was transitioned. Best-effort: the
        caller swallows failures (it is already the degraded path),
        but eliminating the QUEUED zombie here means a later
        `find_idempotent` retry never gets back a never-dispatched
        task (which would hang the caller) and the row stops
        consuming `spawn_max_depth` budget."""
        user_id = self._require_user()
        result = await self._conn.execute(
            """
            UPDATE tasks
            SET state = 'FAILED', status_code = $3, error = $4,
                updated_at = now()
            WHERE task_id = $1 AND user_id = $2
              AND state NOT IN
                  ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')
            """,
            task_id,
            user_id,
            status_code,
            error,
        )
        try:
            updated = int(result.rsplit(" ", 1)[-1])
        except ValueError:
            updated = 0
        return updated == 1

    async def list_descendants(self, task_id: str) -> list[TaskRow]:
        """Recursive CTE on `parent_task_id`. Used by cancel propagation.

        Bounded by `_MAX_TASK_TREE_DEPTH` via a depth counter so a
        malformed `parent_task_id` cycle (FK only checks existence,
        not acyclicity) can't pin Postgres work-mem looping
        indefinitely. Same defensive cap shape as
        `task_has_ancestor_with_agent`.
        """
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            WITH RECURSIVE descendants AS (
                SELECT t.task_id, 0 AS _depth
                FROM tasks t
                WHERE t.parent_task_id = $1 AND t.user_id = $2
              UNION ALL
                SELECT t.task_id, d._depth + 1
                FROM tasks t
                JOIN descendants d ON t.parent_task_id = d.task_id
                WHERE t.user_id = $2 AND d._depth < $3
            )
            SELECT t.* FROM tasks t
            JOIN descendants d ON t.task_id = d.task_id
            """,
            task_id,
            user_id,
            _MAX_TASK_TREE_DEPTH,
        )
        return [TaskRow.model_validate(dict(r)) for r in rows]

    async def update_task_state(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        status_code: int | None = None,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Low-level state write. ONLY callable from `bp_router.state.task_transition`."""
        user_id = self._require_user()
        await self._conn.execute(
            """
            UPDATE tasks
            SET state = $3, status_code = COALESCE($4, status_code),
                output = COALESCE($5, output), error = COALESCE($6, error),
                updated_at = $7
            WHERE task_id = $1 AND user_id = $2
            """,
            task_id,
            user_id,
            new_state.value,
            status_code,
            output,
            error,
            _now(),
        )

    # ------------------------------------------------------------------
    # Task events (audit)
    # ------------------------------------------------------------------

    async def insert_task_event(
        self,
        *,
        task_id: str,
        kind: str,
        actor_agent_id: str | None,
        from_state: TaskState | None = None,
        to_state: TaskState | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TaskEventRow:
        """Append one row to `task_events` for the scoped user's task.

        Ownership enforced via `INSERT ... SELECT ... WHERE EXISTS`
        so the event row only lands when `task_id` belongs to the
        current user. Without this guard a misuse (or a future caller
        passing a user-supplied `task_id`) would write audit events
        under another user's task — the original implementation
        relied entirely on caller discipline.

        Raises `CrossUserTaskAccess` when the EXISTS clause fails;
        the wider audit transaction rolls back rather than silently
        skipping the event.
        """
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            INSERT INTO task_events
                (task_id, ts, kind, actor_agent_id, from_state, to_state, payload)
            SELECT $1, $2, $3, $4, $5, $6, $7
            WHERE EXISTS (
                SELECT 1 FROM tasks WHERE task_id = $1 AND user_id = $8
            )
            RETURNING event_id, task_id, ts, kind, actor_agent_id,
                      from_state, to_state, payload
            """,
            task_id,
            _now(),
            kind,
            actor_agent_id,
            from_state.value if from_state else None,
            to_state.value if to_state else None,
            payload or {},
            user_id,
        )
        if row is None:
            # EXISTS clause failed — task either doesn't exist or
            # belongs to another user. Treat both the same way the
            # rest of `Scope` does: surface as `CrossUserTaskAccess`
            # so callers get a typed error rather than a silent
            # skipped audit row.
            raise CrossUserTaskAccess(task_id)
        return TaskEventRow.model_validate(dict(row))

    async def list_task_events(
        self, task_id: str, *, limit: int = 200
    ) -> list[TaskEventRow]:
        # Cross-check ownership via the parent task before exposing events.
        user_id = self._require_user()
        owner = await self._conn.fetchval(
            "SELECT user_id FROM tasks WHERE task_id = $1",
            task_id,
        )
        if owner != user_id:
            return []
        rows = await self._conn.fetch(
            """
            SELECT event_id, task_id, ts, kind, actor_agent_id,
                   from_state, to_state, payload
            FROM task_events
            WHERE task_id = $1
            ORDER BY ts ASC
            LIMIT $2
            """,
            task_id,
            limit,
        )
        return [TaskEventRow.model_validate(dict(r)) for r in rows]

    async def list_delegation_destinations(
        self, task_id: str, *, limit: int = 128
    ) -> list[str]:
        """Return the `to` agent_id of every prior delegation on this
        task, in chronological order. Used by `_admit_delegation`'s
        cycle detection to refuse a delegation that would re-enter an
        agent the task has already visited.

        User-scoped via the `tasks` row, so a misuse passing another
        user's task_id silently returns an empty list rather than
        leaking the chain. The `LIMIT` is one larger than the user-
        facing `task_delegation_max_depth` cap so the caller can
        distinguish "at cap" from "way past cap" if the chain ever
        grew unboundedly (defence-in-depth — the cap is enforced
        before this can happen)."""
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            SELECT payload FROM task_events
            WHERE task_id = $1 AND kind = 'delegated'
              AND EXISTS (
                  SELECT 1 FROM tasks WHERE task_id = $1 AND user_id = $2
              )
            ORDER BY ts ASC
            LIMIT $3
            """,
            task_id,
            user_id,
            limit,
        )
        out: list[str] = []
        for r in rows:
            payload = r["payload"]
            # asyncpg returns jsonb as a native dict; defend against a
            # row that lost its shape (legacy data, schema migration in
            # progress) by skipping anything that isn't `{"to": str}`.
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    continue
            if isinstance(payload, dict):
                to = payload.get("to")
                if isinstance(to, str):
                    out.append(to)
        return out

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    async def insert_file(
        self,
        *,
        sha256: str,
        session_id: str | None,
        task_id: str | None,
        byte_size: int,
        mime_type: str | None,
        storage_url: str,
        original_filename: str | None,
        expires_at: datetime | None,
    ) -> FileRow:
        user_id = self._require_user()
        # Content-addressed dedup: if the user already uploaded the same
        # bytes, return the existing row.
        existing = await self._conn.fetchrow(
            "SELECT * FROM files WHERE user_id = $1 AND sha256 = $2",
            user_id,
            sha256,
        )
        if existing is not None:
            return FileRow.model_validate(dict(existing))

        file_id = _new_id("fil_")
        row = await self._conn.fetchrow(
            """
            INSERT INTO files
                (file_id, sha256, user_id, session_id, task_id,
                 byte_size, mime_type, storage_url, original_filename,
                 created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            file_id,
            sha256,
            user_id,
            session_id,
            task_id,
            byte_size,
            mime_type,
            storage_url,
            original_filename,
            _now(),
            expires_at,
        )
        return FileRow.model_validate(dict(row))

    async def get_file(self, file_id: str) -> FileRow | None:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM files WHERE file_id = $1 AND user_id = $2",
            file_id,
            user_id,
        )
        return FileRow.model_validate(dict(row)) if row else None

    async def get_file_by_sha256(self, sha256: str) -> FileRow | None:
        """Content-addressed dedup lookup; user-scoped."""
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM files WHERE user_id = $1 AND sha256 = $2",
            user_id,
            sha256,
        )
        return FileRow.model_validate(dict(row)) if row else None

    # ------------------------------------------------------------------
    # Named file directory (router-managed file store)
    #
    # The `file_names` table is a NAME → blob directory over the
    # content-addressed `files` registry. These primitives are the
    # raw row ops; the dedup-counter / overwrite / error allocation
    # policy lives in the frame handler (it composes resolve + insert
    # under the same transaction). `scope` is 'session:{session_id}'
    # or 'persist'. See docs/design/router-managed-file-store.md.
    # ------------------------------------------------------------------

    async def resolve_file_name(
        self, scope: str, filename: str
    ) -> FileNameRow | None:
        """Look up the directory row for `(user, scope, filename)`.
        Returns None when the name is unbound."""
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            SELECT user_id, scope, filename, file_id, byte_size,
                   created_at, updated_at
            FROM file_names
            WHERE user_id = $1 AND scope = $2 AND filename = $3
            """,
            user_id,
            scope,
            filename,
        )
        return FileNameRow.model_validate(dict(row)) if row else None

    async def insert_file_name(
        self,
        *,
        scope: str,
        filename: str,
        file_id: str,
        byte_size: int,
    ) -> bool:
        """Bind a NEW name to a blob. Returns True on insert, False
        if the name already exists (the PK conflict is the atomic
        name-allocation guard — the caller bumps the dedup counter
        and retries). No-op `ON CONFLICT DO NOTHING` so a concurrent
        insert of the same name doesn't raise."""
        user_id = self._require_user()
        result = await self._conn.execute(
            """
            INSERT INTO file_names
                (user_id, scope, filename, file_id, byte_size,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $6)
            ON CONFLICT (user_id, scope, filename) DO NOTHING
            """,
            user_id,
            scope,
            filename,
            file_id,
            byte_size,
            _now(),
        )
        # asyncpg returns 'INSERT 0 1' on insert, 'INSERT 0 0' on conflict.
        return result.endswith(" 1")

    async def repoint_file_name(
        self,
        *,
        scope: str,
        filename: str,
        file_id: str,
        byte_size: int,
    ) -> None:
        """Overwrite an EXISTING name to point at a new blob
        (`dedup="overwrite"`). Updates `byte_size` (quota) +
        `updated_at`. The old blob is left for the refcount sweep."""
        user_id = self._require_user()
        await self._conn.execute(
            """
            UPDATE file_names
            SET file_id = $4, byte_size = $5, updated_at = $6
            WHERE user_id = $1 AND scope = $2 AND filename = $3
            """,
            user_id,
            scope,
            filename,
            file_id,
            byte_size,
            _now(),
        )

    async def list_file_names(
        self,
        scope: str,
        *,
        query: str | None = None,
        stored_after: datetime | None = None,
    ) -> list[FileNameRow]:
        """List directory rows in `scope`, newest first. `query` is a
        case-insensitive LITERAL substring match on the filename — its
        `%`/`_` are escaped, not treated as wildcards; `stored_after`
        filters by `created_at`."""
        user_id = self._require_user()
        like = f"%{_like_escape(query)}%" if query is not None else None
        rows = await self._conn.fetch(
            r"""
            SELECT user_id, scope, filename, file_id, byte_size,
                   created_at, updated_at
            FROM file_names
            WHERE user_id = $1 AND scope = $2
              AND ($3::text IS NULL OR filename ILIKE $3 ESCAPE '\')
              AND ($4::timestamptz IS NULL OR created_at > $4)
            ORDER BY created_at DESC
            """,
            user_id,
            scope,
            like,
            stored_after,
        )
        return [FileNameRow.model_validate(dict(r)) for r in rows]

    async def delete_file_name(self, scope: str, filename: str) -> int:
        """Delete one directory row by exact name. Returns the count
        deleted (0 or 1). The blob is left for the refcount sweep."""
        user_id = self._require_user()
        result = await self._conn.execute(
            """
            DELETE FROM file_names
            WHERE user_id = $1 AND scope = $2 AND filename = $3
            """,
            user_id,
            scope,
            filename,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def delete_file_names_glob(self, scope: str, pattern: str) -> int:
        """Delete directory rows whose filename matches a `*`-glob
        (e.g. `draft_*`). The glob is translated to a SQL LIKE
        pattern with `*`→`%`; literal `%`/`_` in the name are escaped
        so they aren't treated as wildcards. Returns the count
        deleted."""
        user_id = self._require_user()
        # Escape literal LIKE metachars, THEN translate the `*` glob to
        # the `%` wildcard (shared escaper with `list_file_names`).
        like = _like_escape(pattern).replace("*", "%")
        result = await self._conn.execute(
            r"""
            DELETE FROM file_names
            WHERE user_id = $1 AND scope = $2
              AND filename LIKE $3 ESCAPE '\'
            """,
            user_id,
            scope,
            like,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def delete_file_names_for_scope(self, scope: str) -> int:
        """Delete EVERY directory row in `scope` (session-close GC).
        Returns the count deleted. Blobs are left for the refcount
        sweep."""
        user_id = self._require_user()
        result = await self._conn.execute(
            "DELETE FROM file_names WHERE user_id = $1 AND scope = $2",
            user_id,
            scope,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def purge_session(self, session_id: str) -> bool:
        """Hard-delete a session and its router-side dependents, user-scoped,
        in FK order. Caller MUST run inside a transaction and should have
        closed the session first (so no task is mid-flight).

        `files` rows are **detached** (`session_id`/`task_id` → NULL), not
        deleted: they're content-addressed, dedup'd per `(user, sha256)`,
        and refcounted by `file_names`, so deleting them could break a
        `persist/` name that shares the row. The reclaim sweep frees the
        blob once no name references it — same contract as session close.
        Returns True if a session row was removed (False ⇒ not found)."""
        user_id = self._require_user()
        tids = [
            r["task_id"]
            for r in await self._conn.fetch(
                "SELECT task_id FROM tasks WHERE session_id = $1 AND user_id = $2",
                session_id, user_id,
            )
        ]
        if tids:
            await self._conn.execute(
                "DELETE FROM task_events WHERE task_id = ANY($1::text[])", tids
            )
        await self.delete_file_names_for_scope(f"session:{session_id}")
        await self._conn.execute(
            "UPDATE files SET session_id = NULL WHERE user_id = $1 AND session_id = $2",
            user_id, session_id,
        )
        if tids:
            await self._conn.execute(
                "UPDATE files SET task_id = NULL "
                "WHERE user_id = $1 AND task_id = ANY($2::text[])",
                user_id, tids,
            )
        await self._conn.execute(
            "DELETE FROM tasks WHERE session_id = $1 AND user_id = $2",
            session_id, user_id,
        )
        status = await self._conn.execute(
            "DELETE FROM sessions WHERE session_id = $1 AND user_id = $2",
            session_id, user_id,
        )
        return status.endswith(" 1")

    async def count_user_storage_bytes(self) -> int:
        """Per-user storage usage = SUM(byte_size) over the user's
        directory rows (session + persist). Drives the quota gate.
        A name pointing at a shared blob still counts — namespace
        accounting, not physical storage."""
        user_id = self._require_user()
        val = await self._conn.fetchval(
            "SELECT COALESCE(SUM(byte_size), 0) FROM file_names "
            "WHERE user_id = $1",
            user_id,
        )
        return int(val)

    async def count_names_for_file(self, file_id: str) -> int:
        """How many directory rows (any scope, this user) still point
        at `file_id`. Zero ⇒ the blob is GC-collectable (subject to
        the cross-user `count_other_file_refs` check the sweep already
        applies)."""
        user_id = self._require_user()
        val = await self._conn.fetchval(
            "SELECT COUNT(*) FROM file_names "
            "WHERE user_id = $1 AND file_id = $2",
            user_id,
            file_id,
        )
        return int(val)


# ---------------------------------------------------------------------------
# Cross-user / admin reads (don't fit the Scope wrapper)
# ---------------------------------------------------------------------------


async def get_file_by_id(
    conn: asyncpg.Connection, file_id: str
) -> FileRow | None:
    """Unscoped lookup by `file_id` (no `user_id` filter).

    Used by the attachment resolver and the keyed download path,
    where a verified, file-bound fetch capability `key` — not
    row-level ownership — is the authorization. A key may
    legitimately reference another user's file (delegation /
    forwarding), so this MUST NOT back any path whose authorization
    is a user principal; those keep `Scope.user`."""
    row = await conn.fetchrow(
        "SELECT * FROM files WHERE file_id = $1", file_id
    )
    return FileRow.model_validate(dict(row)) if row else None


def user_is_active(user: UserRow | None) -> bool:
    """True if the user exists and is neither suspended nor soft-
    deleted. Folds the repeated triplet from auth.py login /
    refresh / change_password / reset_password into one call site
    so a future lifecycle flag (e.g. password expiry) can extend
    the predicate in one place."""
    return (
        user is not None
        and user.suspended_at is None
        and user.deleted_at is None
    )


async def get_user_by_id(
    conn: asyncpg.Connection, user_id: str
) -> UserRow | None:
    row = await conn.fetchrow(
        "SELECT * FROM users WHERE user_id = $1",
        user_id,
    )
    return UserRow.model_validate(dict(row)) if row else None


async def get_user_by_email(
    conn: asyncpg.Connection, email: str
) -> UserRow | None:
    row = await conn.fetchrow(
        "SELECT * FROM users WHERE email = $1",
        email,
    )
    return UserRow.model_validate(dict(row)) if row else None


async def insert_user(
    conn: asyncpg.Connection,
    *,
    user_id: str | None = None,
    email: str | None,
    level: str,
    auth_kind: str,
    auth_secret_hash: str | None,
    serviced_by: list[str] | None = None,
) -> UserRow:
    """Insert a user row. `serviced_by` (F8) is the initial list of
    service-principal user_ids authorised to mint credentials for
    this user; the caller is responsible for validating each entry
    points at a `level="service"` row.
    """
    user_id = user_id or _new_id("usr_")
    row = await conn.fetchrow(
        """
        INSERT INTO users
            (user_id, email, level, auth_kind, auth_secret_hash,
             created_at, serviced_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        user_id,
        email,
        level,
        auth_kind,
        auth_secret_hash,
        _now(),
        serviced_by or [],
    )
    return UserRow.model_validate(dict(row))


async def append_to_serviced_by(
    conn: asyncpg.Connection,
    target_user_id: str,
    service_user_id: str,
) -> bool:
    """Idempotently append `service_user_id` to `target_user_id.serviced_by`.

    Returns True if the array changed (entry was new), False if the
    entry was already present. Returns False also when the target
    user doesn't exist (no exception — the admin endpoint
    pre-validates).
    """
    result = await conn.execute(
        """
        UPDATE users
        SET serviced_by = CASE
            WHEN $2 = ANY(serviced_by) THEN serviced_by
            ELSE array_append(serviced_by, $2)
        END
        WHERE user_id = $1 AND NOT ($2 = ANY(serviced_by))
        """,
        target_user_id,
        service_user_id,
    )
    try:
        return int(result.rsplit(" ", 1)[-1]) == 1
    except ValueError:
        return False


async def remove_from_serviced_by(
    conn: asyncpg.Connection,
    target_user_id: str,
    service_user_id: str,
) -> bool:
    """Idempotently remove `service_user_id` from
    `target_user_id.serviced_by`. Returns True if the array changed.
    """
    result = await conn.execute(
        "UPDATE users SET serviced_by = array_remove(serviced_by, $2) "
        "WHERE user_id = $1 AND $2 = ANY(serviced_by)",
        target_user_id,
        service_user_id,
    )
    try:
        return int(result.rsplit(" ", 1)[-1]) == 1
    except ValueError:
        return False


async def list_serviced_sessions(
    conn: asyncpg.Connection,
    *,
    service_user_id: str,
    channel: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[Any]:
    """Sessions belonging to users serviced by `service_user_id`, newest-
    open last (ascending `opened_at` for cursor paging).

    Backs the `require_service` discovery endpoint a channel uses to learn
    which of its serviced users have been provisioned (admin approval
    opens a session whose `metadata.external_id` is the channel-native
    id). Scoped strictly to the caller's serviced users — never the whole
    table. `channel` filters on `metadata->>'kind'`; `since` returns only
    sessions opened strictly after the cursor.
    """
    clauses = ["$1 = ANY(u.serviced_by)", "u.deleted_at IS NULL"]
    args: list[Any] = [service_user_id]
    if channel is not None:
        args.append(channel)
        clauses.append(f"s.metadata->>'kind' = ${len(args)}")
    if since is not None:
        args.append(since)
        clauses.append(f"s.opened_at > ${len(args)}")
    args.append(limit)
    where = " AND ".join(clauses)
    return await conn.fetch(
        f"""
        SELECT s.session_id, s.user_id, s.opened_at, s.closed_at,
               s.metadata
        FROM sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE {where}
        ORDER BY s.opened_at ASC
        LIMIT ${len(args)}
        """,
        *args,
    )


async def sweep_serviced_by_references(
    conn: asyncpg.Connection,
    service_user_id: str,
) -> int:
    """Remove `service_user_id` from EVERY user's `serviced_by` array.
    Call this when a service-principal user is deleted so dead
    references don't linger.

    Returns the number of rows updated.
    """
    result = await conn.execute(
        "UPDATE users SET serviced_by = array_remove(serviced_by, $1) "
        "WHERE $1 = ANY(serviced_by)",
        service_user_id,
    )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Pending user registrations (F7)
# ---------------------------------------------------------------------------


async def upsert_pending_registration(
    conn: asyncpg.Connection,
    *,
    channel: str,
    external_id: str,
    display_name: str | None,
    requested_email: str | None,
    metadata: dict[str, Any],
    submitted_by_service_user_id: str | None,
) -> PendingRegistrationRow:
    """Insert a new pending registration, OR upsert: bump `attempts`
    and refresh `last_attempt_at` when `(channel, external_id)`
    already exists. Display name / requested_email are filled in
    when previously NULL; non-empty metadata replaces. Service-
    principal submitter is overwritten by the latest non-NULL value
    (most-recent wins so admin sees the current submitter on the
    queue row).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO pending_user_registrations
            (channel, external_id, display_name, requested_email,
             metadata, submitted_by_service_user_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (channel, external_id) DO UPDATE
        SET attempts        = pending_user_registrations.attempts + 1,
            last_attempt_at = now(),
            display_name    = COALESCE(EXCLUDED.display_name,
                                       pending_user_registrations.display_name),
            requested_email = COALESCE(EXCLUDED.requested_email,
                                       pending_user_registrations.requested_email),
            metadata        = CASE
                WHEN EXCLUDED.metadata = '{}'::jsonb
                    THEN pending_user_registrations.metadata
                ELSE EXCLUDED.metadata
            END,
            submitted_by_service_user_id = COALESCE(
                EXCLUDED.submitted_by_service_user_id,
                pending_user_registrations.submitted_by_service_user_id
            )
        RETURNING registration_id::text, channel, external_id,
                  display_name, requested_email, metadata,
                  requested_at, attempts, last_attempt_at,
                  submitted_by_service_user_id
        """,
        channel, external_id, display_name, requested_email,
        metadata, submitted_by_service_user_id,
    )
    assert row is not None
    return PendingRegistrationRow.model_validate(dict(row))


async def log_registration_attempt(
    conn: asyncpg.Connection, *, channel: str, external_id: str
) -> None:
    await conn.execute(
        "INSERT INTO registration_attempts (channel, external_id) "
        "VALUES ($1, $2)",
        channel, external_id,
    )


async def gc_registration_attempts(
    conn: asyncpg.Connection, *, cutoff: datetime
) -> int:
    """Delete `registration_attempts` rows older than `cutoff`.

    The rate limiter's per-(channel, external_id) sliding window is
    bounded by minutes (see Settings.registration_rate_limit_*);
    rows beyond that are pure audit history. Caller picks a
    cutoff that retains enough audit (typically 30 days) while
    keeping the table size bounded.

    Returns the number of rows deleted."""
    result = await conn.execute(
        "DELETE FROM registration_attempts WHERE attempted_at < $1",
        cutoff,
    )
    parts = result.split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0


async def get_pending_registration(
    conn: asyncpg.Connection, registration_id: str
) -> PendingRegistrationRow | None:
    row = await conn.fetchrow(
        """
        SELECT registration_id::text, channel, external_id,
               display_name, requested_email, metadata,
               requested_at, attempts, last_attempt_at,
               submitted_by_service_user_id
        FROM pending_user_registrations
        WHERE registration_id = $1::uuid
        """,
        registration_id,
    )
    return PendingRegistrationRow.model_validate(dict(row)) if row else None


async def list_pending_registrations(
    conn: asyncpg.Connection,
    *,
    channel: str | None = None,
    submitted_by_service_user_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[PendingRegistrationRow]:
    """List pending registrations. Optional `channel` and/or
    `submitted_by_service_user_id` filters compose with AND.
    Sorted newest-first by `requested_at`."""
    where: list[str] = []
    args: list[Any] = []
    if channel is not None:
        args.append(channel)
        where.append(f"channel = ${len(args)}")
    if submitted_by_service_user_id is not None:
        args.append(submitted_by_service_user_id)
        where.append(f"submitted_by_service_user_id = ${len(args)}")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    args.extend([limit, offset])
    rows = await conn.fetch(
        f"""
        SELECT registration_id::text, channel, external_id,
               display_name, requested_email, metadata,
               requested_at, attempts, last_attempt_at,
               submitted_by_service_user_id
        FROM pending_user_registrations
        {where_clause}
        ORDER BY requested_at DESC
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    return [PendingRegistrationRow.model_validate(dict(r)) for r in rows]


async def delete_pending_registration(
    conn: asyncpg.Connection, registration_id: str
) -> bool:
    result = await conn.execute(
        "DELETE FROM pending_user_registrations WHERE registration_id = $1::uuid",
        registration_id,
    )
    try:
        return int(result.rsplit(" ", 1)[-1]) == 1
    except ValueError:
        return False


async def list_users(
    conn: asyncpg.Connection,
    *,
    level: str | None = None,
    include_deleted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[UserRow]:
    """Admin-scope listing. Optional `level` filter; paginated.

    `include_deleted=False` (default) hides users with non-null
    `deleted_at` — the common case for operator browsing. Set to
    True to include them, e.g. when surfacing the full historical
    set for audit drill-down."""
    where: list[str] = []
    args: list[Any] = []
    if level is not None:
        args.append(level)
        where.append(f"level = ${len(args)}")
    if not include_deleted:
        where.append("deleted_at IS NULL")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    args.extend([limit, offset])
    rows = await conn.fetch(
        f"""
        SELECT * FROM users
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    return [UserRow.model_validate(dict(r)) for r in rows]


async def delete_user_password_reset_tokens(
    conn: asyncpg.Connection, user_id: str
) -> int:
    """Companion to `delete_user_refresh_tokens`. Called from the
    soft-delete pipeline so a deleted user has no pending reset
    tokens that could reactivate them."""
    result = await conn.execute(
        "DELETE FROM password_reset_tokens WHERE user_id = $1",
        user_id,
    )
    parts = result.split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0


async def soft_delete_user(
    conn: asyncpg.Connection, user_id: str
) -> dict[str, Any] | None:
    """Mark a user as soft-deleted and run the cascade:

      1. Set `users.deleted_at = now()` if NULL. Idempotent —
         calling twice is a no-op on the timestamp.
      2. Delete every refresh token (forced logout).
      3. Delete every pending password-reset token.
      4. Run the F8 `sweep_serviced_by_references` so no other
         user's `serviced_by` array carries a now-deactivated
         service principal.
      5. **R6 add**: cap the `expires_at` on every file the user
         owns to 24h from now. The background file-GC
         (`_gc_files_once`) will then reap the bytes within the
         next sweep cycle. Pre-R6 files survived their natural
         TTL after a soft-delete (up to 7 days by default); a
         file with `expires_at IS NULL` (none today but the
         schema allows it) would survive indefinitely.

    Returns a dict of per-step counts plus `was_already_deleted`
    so the caller can audit the operation precisely. Returns
    None when `user_id` doesn't exist (caller surfaces a 404)."""
    user = await get_user_by_id(conn, user_id)
    if user is None:
        return None
    if user.deleted_at is not None:
        return {
            "was_already_deleted": True,
            "refresh_tokens_deleted": 0,
            "reset_tokens_deleted": 0,
            "serviced_by_sweep_count": 0,
            "files_expired_count": 0,
        }
    await conn.execute(
        "UPDATE users SET deleted_at = now() WHERE user_id = $1",
        user_id,
    )
    refresh_deleted = await delete_user_refresh_tokens(conn, user_id)
    reset_deleted = await delete_user_password_reset_tokens(conn, user_id)
    sweep_count = await sweep_serviced_by_references(conn, user_id)
    # Cap expiry on every file owned by this user. `LEAST(...,
    # now() + 24h)` preserves an earlier-set `expires_at` (a
    # short-lived ephemeral upload keeps its original schedule,
    # not extended). COALESCE handles the nullable case so a file
    # with `expires_at IS NULL` doesn't short-circuit the LEAST.
    files_status = await conn.execute(
        """
        UPDATE files
           SET expires_at = LEAST(
               COALESCE(expires_at, now() + INTERVAL '24 hours'),
               now() + INTERVAL '24 hours'
           )
         WHERE user_id = $1
        """,
        user_id,
    )
    # asyncpg returns the command tag, e.g. `"UPDATE 17"`. Parse
    # the last token; failures fall back to 0 (count is for audit
    # only, not authorisation).
    try:
        files_expired = int(files_status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        files_expired = 0
    return {
        "was_already_deleted": False,
        "refresh_tokens_deleted": refresh_deleted,
        "reset_tokens_deleted": reset_deleted,
        "serviced_by_sweep_count": sweep_count,
        "files_expired_count": files_expired,
    }


async def get_agent(
    conn: asyncpg.Connection, agent_id: str
) -> AgentRow | None:
    row = await conn.fetchrow(
        "SELECT * FROM agents WHERE agent_id = $1",
        agent_id,
    )
    return AgentRow.model_validate(dict(row)) if row else None


async def get_agent_for_update(
    conn: asyncpg.Connection, agent_id: str
) -> AgentRow | None:
    """Row-locking variant of `get_agent` — adds `FOR UPDATE` so the
    caller's enclosing `conn.transaction()` block serialises any
    other concurrent SELECT FOR UPDATE on this row.

    Use for read-modify-write flows on a single agent row
    (`_handle_agent_info_update` is the seed caller). Without the
    lock, two concurrent updates each read the pre-patch row, each
    merge against that snapshot, and the second `UPDATE` clobbers
    the first's field changes.

    MUST be called inside an `async with conn.transaction():`
    block. Outside a transaction, `FOR UPDATE` releases the lock
    immediately and the call is no better than `get_agent`."""
    row = await conn.fetchrow(
        "SELECT * FROM agents WHERE agent_id = $1 FOR UPDATE",
        agent_id,
    )
    return AgentRow.model_validate(dict(row)) if row else None


async def list_agents(conn: asyncpg.Connection) -> list[AgentRow]:
    rows = await conn.fetch("SELECT * FROM agents ORDER BY agent_id")
    return [AgentRow.model_validate(dict(r)) for r in rows]


async def insert_agent(
    conn: asyncpg.Connection,
    *,
    agent_id: str,
    kind: str,
    capabilities: list[str],
    groups: list[str],
    agent_info: dict[str, Any],
    auth_token_hash: str | None = None,
    public_key: str | None = None,
) -> AgentRow:
    row = await conn.fetchrow(
        """
        INSERT INTO agents (
            agent_id, kind, status, capabilities, groups,
            agent_info, auth_token_hash, public_key, registered_at
        )
        VALUES ($1, $2, 'active', $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        agent_id,
        kind,
        capabilities,
        groups,
        agent_info,
        auth_token_hash,
        public_key,
        _now(),
    )
    return AgentRow.model_validate(dict(row))


async def update_agent_last_seen(
    conn: asyncpg.Connection, agent_id: str
) -> None:
    await conn.execute(
        "UPDATE agents SET last_seen_at = $2 WHERE agent_id = $1",
        agent_id,
        _now(),
    )


async def update_agent_info(
    conn: asyncpg.Connection,
    agent_id: str,
    *,
    agent_info: dict[str, Any],
    groups: list[str],
    capabilities: list[str],
) -> bool:
    """Update the agent's published AgentInfo + the denormalised
    `groups` / `capabilities` columns (the ACL evaluator reads
    from those directly so they need to stay in sync with the
    JSONB).

    Phase 10e: invoked when an agent sends an
    `AgentInfoUpdateFrame`. Caller MUST have merged the patch
    onto the existing record + re-validated via Pydantic before
    writing — this helper just persists.

    Returns True if a row was updated, False when `agent_id` is
    unknown (caller surfaces an error)."""
    result = await conn.execute(
        """
        UPDATE agents
        SET agent_info   = $2,
            groups       = $3,
            capabilities = $4
        WHERE agent_id = $1
        """,
        agent_id,
        agent_info,
        groups,
        capabilities,
    )
    return result.endswith(" 1")


async def suspend_agent(conn: asyncpg.Connection, agent_id: str) -> None:
    await conn.execute(
        "UPDATE agents SET status = 'suspended' WHERE agent_id = $1",
        agent_id,
    )


async def unsuspend_agent(conn: asyncpg.Connection, agent_id: str) -> None:
    """Restore a suspended agent to 'active'. The status guard prevents
    accidental promotion of a 'removed' or 'pending' row."""
    await conn.execute(
        "UPDATE agents SET status = 'active' "
        "WHERE agent_id = $1 AND status = 'suspended'",
        agent_id,
    )


async def reactivate_agent_on_onboard(
    conn: asyncpg.Connection,
    *,
    agent_id: str,
    capabilities: list[str],
    groups: list[str],
    agent_info: dict[str, Any],
    public_key: str | None,
) -> AgentRow:
    """Re-onboard an EXISTING agent row (idempotent onboard): set it back to
    `active` and refresh the identity fields from the new AgentInfo, returning
    the updated row. Guarded to `active`/`pending`/`suspended` — never
    resurrects a `removed` (terminal) row (the onboard handler already refuses
    `removed`, but the WHERE guard is defence-in-depth). Mirrors
    `insert_agent`'s column set so a re-onboarded agent looks identical to a
    freshly-inserted one. `public_key` is COALESCEd so a re-onboard that omits
    it keeps the stored key."""
    row = await conn.fetchrow(
        """
        UPDATE agents
        SET status = 'active',
            capabilities = $2,
            groups = $3,
            agent_info = $4,
            public_key = COALESCE($5, public_key)
        WHERE agent_id = $1 AND status IN ('active', 'pending', 'suspended')
        RETURNING *
        """,
        agent_id,
        capabilities,
        groups,
        agent_info,
        public_key,
    )
    return AgentRow.model_validate(dict(row))


async def evict_agent(conn: asyncpg.Connection, agent_id: str) -> None:
    """Mark agent as `removed` (terminal). Row is preserved so that
    foreign keys from tasks and audit_log stay valid."""
    await conn.execute(
        "UPDATE agents SET status = 'removed' WHERE agent_id = $1",
        agent_id,
    )


# Tombstone name: `deleted_<id>_<epoch>`, kept inside the agents.agent_id
# CHECK (`^[A-Za-z_][A-Za-z0-9_-]{0,63}$` — ≤64 chars, underscore/dash only,
# no `.`/`:`). The original id is truncated if needed so the suffix always
# fits; the row is preserved (history intact), only the PK is freed for reuse.
_TOMBSTONE_MAX = 64


def tombstone_agent_id(agent_id: str, *, epoch: int) -> str:
    suffix = f"_{epoch}"
    head = f"deleted_{agent_id}"
    return f"{head[: _TOMBSTONE_MAX - len(suffix)]}{suffix}"


async def rename_evicted_agent(
    conn: asyncpg.Connection,
    agent_id: str,
    *,
    epoch: int,
    service_user_id: str | None = None,
) -> tuple[str, str | None]:
    """Free an evicted `agent_id` for reuse by renaming the `removed` row's
    PK to a tombstone (`deleted_<id>_<epoch>`). FK `ON UPDATE CASCADE`
    (migration 0002) rewrites every dependent `tasks` row, so history is
    preserved under the tombstone id. When `service_user_id` is supplied and
    that co-located service principal exists, it is renamed the same way (its
    user FKs cascade too) so a CHANNEL agent's id is reusable as well.

    Returns `(new_agent_id, new_service_user_id_or_None)`. Run inside a
    transaction AFTER failing in-flight tasks (which key off the old
    `agent_id`). Only renames a `removed` row; a no-op otherwise."""
    new_agent_id = tombstone_agent_id(agent_id, epoch=epoch)
    row = await conn.fetchrow(
        "UPDATE agents SET agent_id = $2 "
        "WHERE agent_id = $1 AND status = 'removed' "
        "RETURNING agent_id",
        agent_id, new_agent_id,
    )
    if row is None:
        return (agent_id, None)

    new_svc_id: str | None = None
    if service_user_id is not None:
        candidate = tombstone_agent_id(service_user_id, epoch=epoch)
        svc_row = await conn.fetchrow(
            "UPDATE users SET user_id = $2 "
            "WHERE user_id = $1 AND level = 'service' "
            "RETURNING user_id",
            service_user_id, candidate,
        )
        if svc_row is not None:
            new_svc_id = candidate
    return (new_agent_id, new_svc_id)


async def reset_agent_to_pending(conn: asyncpg.Connection, agent_id: str) -> bool:
    """Move an `active` or `suspended` agent back to `pending` — the storage
    primitive behind the `reset` / `reprovision` admin actions (force the
    agent off; it must re-onboard before serving again). The status guard
    never resurrects a `removed` (terminal) row nor touches an already-
    `pending` one. Returns True iff a row was transitioned.

    Note: re-onboard itself no longer requires `pending` — `POST /v1/onboard`
    reactivates an already-`active` row given a valid invitation. This is now
    an operational kick, not the recovery path it once was."""
    row = await conn.fetchrow(
        "UPDATE agents SET status = 'pending' "
        "WHERE agent_id = $1 AND status IN ('active', 'suspended') "
        "RETURNING agent_id",
        agent_id,
    )
    return row is not None


async def task_has_ancestor_with_agent(
    conn: asyncpg.Connection,
    *,
    task_id: str,
    agent_id: str,
    user_id: str,
) -> bool:
    """Return True if `task_id` (or any of its ancestors WITHIN
    `user_id`'s tree) was assigned to `agent_id`. Used by
    `_handle_cancel` to authorise an inbound `Cancel` from a WS-attached
    agent: an agent may cancel any task in its own subtree, but not
    arbitrary cross-user tasks.

    `user_id` scoping: the recursive walk filters
    on `tasks.user_id = $user_id` at every step. Without this, a
    malformed cross-user `parent_task_id` (which the FK doesn't reject)
    would let an agent in user V's tree authorise
    a Cancel against a task whose chain passes through V even though
    the leaf is owned by a different user. Defense-in-depth: the
    user_id boundary is enforced at the walk too.

    Bounded by `_MAX_TASK_TREE_DEPTH` so a malformed cycle in
    `parent_task_id` (FK only checks existence, not acyclicity) can't
    pin Postgres work-mem looping.
    """
    row = await conn.fetchrow(
        """
        WITH RECURSIVE ancestors(task_id, agent_id, parent_task_id, _depth) AS (
            SELECT task_id, agent_id, parent_task_id, 0
            FROM tasks WHERE task_id = $1 AND user_id = $3
          UNION ALL
            SELECT t.task_id, t.agent_id, t.parent_task_id, a._depth + 1
            FROM tasks t
            JOIN ancestors a ON t.task_id = a.parent_task_id
            WHERE t.user_id = $3 AND a._depth < $4
        )
        SELECT 1 FROM ancestors WHERE agent_id = $2 LIMIT 1
        """,
        task_id,
        agent_id,
        user_id,
        _MAX_TASK_TREE_DEPTH,
    )
    return row is not None


async def count_task_chain_depth(
    conn: asyncpg.Connection,
    *,
    task_id: str,
    user_id: str,
) -> int:
    """Return the depth of `task_id`'s ancestor chain (including
    `task_id` itself) within `user_id`'s tree.

    Used by `admit_task` to refuse a `peers.spawn(...)` whose
    parent's chain is already at the configured `spawn_max_depth`
    cap. Without the cap, agent A spawning B
    spawning A → ... is unbounded and exhausts the connection
    pool / WS outbox / `tasks` rows under runaway recursion or
    adversarial topology.

    Returns 0 when the task does not exist or belongs to a
    different user — caller should treat that as "no parent" or
    raise its own typed error.

    Bounded by `_MAX_TASK_TREE_DEPTH` (defense-in-depth against
    malformed cycles); the result is the minimum of actual depth
    and that cap.

    When the returned depth is exactly `_MAX_TASK_TREE_DEPTH` it
    likely indicates either a genuine deep chain at the safety
    cap OR a malformed `parent_task_id` cycle that the recursive
    CTE truncated.
    Both cases are notable; the WARNING log lets operators
    distinguish "ordinary deep-spawn rejection" from "cycle in
    the data." Cycles shouldn't be possible without an FK
    integrity violation, but defence-in-depth: surfacing the
    saturation in logs catches a class of bugs the depth-cap
    silently hides.
    """
    row = await conn.fetchrow(
        """
        WITH RECURSIVE ancestors(task_id, parent_task_id, _depth) AS (
            SELECT task_id, parent_task_id, 1
            FROM tasks WHERE task_id = $1 AND user_id = $2
          UNION ALL
            SELECT t.task_id, t.parent_task_id, a._depth + 1
            FROM tasks t
            JOIN ancestors a ON t.task_id = a.parent_task_id
            WHERE t.user_id = $2 AND a._depth < $3
        )
        SELECT COALESCE(MAX(_depth), 0) AS depth FROM ancestors
        """,
        task_id,
        user_id,
        _MAX_TASK_TREE_DEPTH,
    )
    depth = int(row["depth"]) if row is not None else 0
    if depth >= _MAX_TASK_TREE_DEPTH:
        # Saturation: either a legitimate chain at the cap or a
        # cycle the CTE truncated. Either is operator-actionable.
        logger.warning(
            "task_chain_depth_saturated",
            extra={
                "event": "task_chain_depth_saturated",
                "bp.task_id": task_id,
                "bp.user_id": user_id,
                "depth": depth,
                "max_depth": _MAX_TASK_TREE_DEPTH,
            },
        )
    return depth


# ---------------------------------------------------------------------------
# Sweep helpers (background loops in `bp_router.tasks`)
# ---------------------------------------------------------------------------


async def list_tasks_by_agent(
    conn: asyncpg.Connection,
    agent_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskRow]:
    """Cross-user view — recent tasks owned by an agent across all users.
    Reserved for admin endpoints; bypasses the per-user `Scope` invariant
    by design. Most recent first."""
    rows = await conn.fetch(
        """
        SELECT * FROM tasks
        WHERE agent_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        agent_id,
        limit,
        offset,
    )
    return [TaskRow.model_validate(dict(r)) for r in rows]


async def list_tasks_by_user(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskRow]:
    """Cross-session view of one user's tasks. Reserved for admin
    endpoints; reads outside the per-user `Scope` wrapper because the
    caller is the admin, not the data subject."""
    rows = await conn.fetch(
        """
        SELECT * FROM tasks
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id,
        limit,
        offset,
    )
    return [TaskRow.model_validate(dict(r)) for r in rows]


async def find_expired_tasks(
    conn: asyncpg.Connection, *, now: datetime, limit: int = 100
) -> list[TaskRow]:
    """Tasks with deadline < now and a non-terminal state."""
    rows = await conn.fetch(
        """
        SELECT * FROM tasks
        WHERE deadline IS NOT NULL
          AND deadline < $1
          AND state IN ('QUEUED', 'RUNNING', 'WAITING_CHILDREN')
        ORDER BY deadline ASC
        LIMIT $2
        """,
        now,
        limit,
    )
    return [TaskRow.model_validate(dict(r)) for r in rows]


async def find_expired_files(
    conn: asyncpg.Connection, *, now: datetime, limit: int = 1000
) -> list[FileRow]:
    """Blobs the GC may reclaim: the upload-TTL (`expires_at`) has
    elapsed AND **no `file_names` directory row references the blob**.

    The name-existence guard is the refcount sweep the file-store
    contract relies on (see `docs/design/router-managed-file-store.md`
    §3/§11.1 and the `repoint`/`delete`/`purge_session` docstrings):
    binding a name to a blob keeps it alive past its upload TTL, so a
    `persist/{name}` (or any still-named session blob) is NOT reaped
    while a name points at it. `expires_at` is purely the eligibility
    timer once the blob is nameless (a never-bound orphan upload, or a
    blob whose last name was deleted). Index-backed by
    `file_names_file_idx`.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM files
        WHERE expires_at IS NOT NULL AND expires_at < $1
          AND NOT EXISTS (
              SELECT 1 FROM file_names fn WHERE fn.file_id = files.file_id
          )
        ORDER BY expires_at ASC
        LIMIT $2
        """,
        now,
        limit,
    )
    return [FileRow.model_validate(dict(r)) for r in rows]


async def delete_file_row(conn: asyncpg.Connection, file_id: str) -> None:
    await conn.execute("DELETE FROM files WHERE file_id = $1", file_id)


async def count_other_file_refs(
    conn: asyncpg.Connection, *, sha256: str, exclude_file_id: str
) -> int:
    """Count rows in `files` referencing `sha256` other than
    `exclude_file_id`. Used by the GC to decide whether the
    underlying storage object can be safely deleted — per-user
    dedup means two users can independently hold
    rows pointing at the same content-addressed storage bytes.
    Caller is expected to hold the row-deleting transaction
    before calling this, so a row that's about-to-be-deleted
    isn't double-counted."""
    val = await conn.fetchval(
        "SELECT COUNT(*) FROM files WHERE sha256 = $1 AND file_id != $2",
        sha256,
        exclude_file_id,
    )
    return int(val or 0)


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


async def insert_refresh_token(
    conn: asyncpg.Connection,
    *,
    token_hash: str,
    user_id: str,
    expires_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO auth_refresh_tokens (token_hash, user_id, issued_at, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        token_hash,
        user_id,
        _now(),
        expires_at,
    )


async def revoke_refresh_token(
    conn: asyncpg.Connection, token_hash: str
) -> bool:
    """Mark a refresh token as used without rotating it (logout path).
    Returns True if a previously-unused row was revoked, False otherwise."""
    result = await conn.execute(
        """
        UPDATE auth_refresh_tokens
        SET used_at = $2
        WHERE token_hash = $1 AND used_at IS NULL
        """,
        token_hash,
        _now(),
    )
    return result.endswith(" 1")


async def delete_user_refresh_tokens(
    conn: asyncpg.Connection, user_id: str
) -> int:
    """Drop every refresh token owned by `user_id`. Used by
    change-password to force re-login on every device."""
    result = await conn.execute(
        "DELETE FROM auth_refresh_tokens WHERE user_id = $1", user_id
    )
    # asyncpg returns "DELETE n"; parse the count for callers that care.
    parts = result.split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0


# ---------------------------------------------------------------------------
# Password-reset tokens (F9)
# ---------------------------------------------------------------------------


async def insert_password_reset_token(
    conn: asyncpg.Connection,
    *,
    token_hash: str,
    user_id: str,
    expires_at: datetime,
    created_by: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO password_reset_tokens
            (token_hash, user_id, expires_at, created_by)
        VALUES ($1, $2, $3, $4)
        """,
        token_hash, user_id, expires_at, created_by,
    )


async def consume_password_reset_token(
    conn: asyncpg.Connection, *, token_hash: str
) -> str | None:
    """Single-use exchange. Returns the user_id on accept, None on
    miss / expired / already-used.

    Race-safe via FOR UPDATE — two concurrent consumers see the
    transition in order, and the second observes `used_at IS NOT NULL`
    on its FOR UPDATE re-read.
    """
    row = await conn.fetchrow(
        """
        SELECT user_id, expires_at, used_at
        FROM password_reset_tokens
        WHERE token_hash = $1
        FOR UPDATE
        """,
        token_hash,
    )
    if row is None or row["used_at"] is not None or row["expires_at"] < _now():
        return None
    await conn.execute(
        "UPDATE password_reset_tokens SET used_at = now() WHERE token_hash = $1",
        token_hash,
    )
    return row["user_id"]


async def set_user_password_hash(
    conn: asyncpg.Connection, *, user_id: str, auth_secret_hash: str
) -> None:
    """Update `users.auth_secret_hash`. Does NOT touch `auth_kind` —
    caller MUST enforce that the user is already password-
    authenticated. Silently flipping auth_kind would downgrade
    OIDC-only users to a weaker auth method; F9's consume handler
    refuses with 409 instead.
    """
    await conn.execute(
        "UPDATE users SET auth_secret_hash = $2 WHERE user_id = $1",
        user_id, auth_secret_hash,
    )


async def consume_refresh_token(
    conn: asyncpg.Connection,
    *,
    token_hash: str,
    replaced_by: str,
) -> str | None:
    """Single-use exchange. Returns the user_id if accepted; None otherwise.

    On replay (used_at already set), invalidates the entire family for
    that user — the caller surfaces this to the audit log.
    """
    row = await conn.fetchrow(
        """
        SELECT user_id, used_at FROM auth_refresh_tokens
        WHERE token_hash = $1 AND expires_at > $2
        FOR UPDATE
        """,
        token_hash,
        _now(),
    )
    if row is None:
        return None
    if row["used_at"] is not None:
        # Replay → blow away the family
        await conn.execute(
            "DELETE FROM auth_refresh_tokens WHERE user_id = $1",
            row["user_id"],
        )
        return None
    await conn.execute(
        """
        UPDATE auth_refresh_tokens
        SET used_at = $2, replaced_by = $3
        WHERE token_hash = $1
        """,
        token_hash,
        _now(),
        replaced_by,
    )
    return row["user_id"]


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def insert_invitation(
    conn: asyncpg.Connection,
    *,
    token_hash: str,
    level: str,
    expires_at: datetime,
    created_by: str,
    idempotency_key: str | None = None,
    provisions_service_user: bool = False,
) -> None:
    """Insert a new invitation row.

    `idempotency_key` is the per-admin retry
    key from the `Idempotency-Key` header. Two `POST /invitations`
    calls from the same admin with the same key violate the
    `(created_by, idempotency_key)` unique index — caller catches
    the asyncpg `UniqueViolationError` and falls back to looking
    up the existing row via `find_invitation_by_idempotency_key`.

    `provisions_service_user` marks the invitation so that consuming
    it at onboarding also provisions a co-located service principal
    (see `consume_invitation` / `api/onboard.py`).
    """
    await conn.execute(
        """
        INSERT INTO invitations
            (token_hash, level, expires_at, created_by, idempotency_key,
             provisions_service_user)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        token_hash,
        level,
        expires_at,
        created_by,
        idempotency_key,
        provisions_service_user,
    )


async def find_invitation_by_idempotency_key(
    conn: asyncpg.Connection,
    *,
    created_by: str,
    idempotency_key: str,
) -> InvitationRow | None:
    """Return the existing invitation a given admin previously
    issued with this `Idempotency-Key`, or None.

    Used by `POST /invitations` to dedupe network retries.
    Per-admin scope so two admins can use the same client-side key
    without collision."""
    row = await conn.fetchrow(
        """
        SELECT * FROM invitations
        WHERE created_by = $1 AND idempotency_key = $2
        """,
        created_by,
        idempotency_key,
    )
    return InvitationRow.model_validate(dict(row)) if row else None


async def list_invitations(
    conn: asyncpg.Connection,
    *,
    limit: int = 100,
    offset: int = 0,
    status_filter: str | None = None,
    now: datetime | None = None,
) -> list[InvitationRow]:
    """Admin-scope listing. Most recently created first.

    `status_filter` is applied IN-SQL so
    pagination boundaries are consistent — the previous code paginated
    first and filtered afterwards, which made `?status=valid` return 0
    rows when the first page happened to be all expired/used.

    `now` is the reference timestamp for the
    expiry comparison; defaults to `_now()`. Pass-through so the API
    layer can pin a single timestamp for the request and avoid clock-
    skew between the SQL `now()` server clock and the Python
    `_now()` admin-process clock.

    Order: `created_at DESC` — the docstring
    promises "most recently created first", but the previous SQL
    ordered by `expires_at DESC` which is a different shape (long-
    expiry tokens that were issued earlier could outrank fresh ones).
    Tie-break by `token_hash` so pagination is deterministic.
    """
    if now is None:
        now = datetime.now(UTC)

    where = ""
    args: list[Any] = []
    if status_filter == "valid":
        # Not used, not expired.
        args.append(now)
        where = "WHERE used_at IS NULL AND expires_at > $1"
    elif status_filter == "used":
        where = "WHERE used_at IS NOT NULL"
    elif status_filter == "expired":
        args.append(now)
        where = "WHERE used_at IS NULL AND expires_at <= $1"
    elif status_filter is not None:
        # Caller passed an unknown status; reject so the API surfaces
        # 400 rather than silently returning unfiltered rows.
        raise ValueError(f"unknown status_filter: {status_filter!r}")

    sql = f"""
        SELECT * FROM invitations
        {where}
        ORDER BY created_at DESC, token_hash DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
    """
    args.extend([limit, offset])
    rows = await conn.fetch(sql, *args)
    return [InvitationRow.model_validate(dict(r)) for r in rows]


async def get_invitation(
    conn: asyncpg.Connection, token_hash: str
) -> InvitationRow | None:
    row = await conn.fetchrow(
        "SELECT * FROM invitations WHERE token_hash = $1", token_hash
    )
    return InvitationRow.model_validate(dict(row)) if row else None


async def delete_invitation(
    conn: asyncpg.Connection, token_hash: str
) -> bool:
    """Hard-delete an unused invitation. Refuses to delete a used one
    (returns False) so the audit trail stays intact."""
    row = await conn.fetchrow(
        "SELECT used_at FROM invitations WHERE token_hash = $1 FOR UPDATE",
        token_hash,
    )
    if row is None:
        return False
    if row["used_at"] is not None:
        return False
    result = await conn.execute(
        "DELETE FROM invitations WHERE token_hash = $1 AND used_at IS NULL",
        token_hash,
    )
    return result.endswith(" 1")


async def consume_invitation(
    conn: asyncpg.Connection,
    *,
    token_hash: str,
    used_by: str,
) -> dict[str, Any] | None:
    """Mark an invitation used. Returns its claims or None if invalid/used."""
    row = await conn.fetchrow(
        """
        SELECT token_hash, level, expires_at, used_at, provisions_service_user
        FROM invitations
        WHERE token_hash = $1
        FOR UPDATE
        """,
        token_hash,
    )
    if row is None or row["used_at"] is not None or row["expires_at"] < _now():
        return None
    await conn.execute(
        """
        UPDATE invitations SET used_at = $2, used_by = $3
        WHERE token_hash = $1
        """,
        token_hash,
        _now(),
        used_by,
    )
    return {
        "level": row["level"],
        "provisions_service_user": row["provisions_service_user"],
    }


# ---------------------------------------------------------------------------
# ACL rule persistence — firewall-style rule list (see docs/acl.md)
# ---------------------------------------------------------------------------


from bp_router.db.models import AclRuleRow  # noqa: E402


async def list_acl_rules(conn: asyncpg.Connection) -> list[AclRuleRow]:
    rows = await conn.fetch("SELECT * FROM acl_rules ORDER BY ord ASC")
    return [AclRuleRow.model_validate(dict(r)) for r in rows]


async def get_acl_rule(
    conn: asyncpg.Connection, rule_id: str
) -> AclRuleRow | None:
    row = await conn.fetchrow("SELECT * FROM acl_rules WHERE rule_id = $1", rule_id)
    return AclRuleRow.model_validate(dict(row)) if row else None


async def insert_acl_rule(
    conn: asyncpg.Connection,
    *,
    ord: int,
    effect: str,
    user_level: str,
    caller_pattern: str,
    callee_pattern: str,
    name: str | None = None,
    description: str | None = None,
    created_by: str | None = None,
) -> AclRuleRow:
    rule_id = _new_id("rule_")
    row = await conn.fetchrow(
        """
        INSERT INTO acl_rules
            (rule_id, ord, name, description, effect, user_level,
             caller_pattern, callee_pattern, created_at, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        rule_id,
        ord,
        name,
        description,
        effect,
        user_level,
        caller_pattern,
        callee_pattern,
        _now(),
        created_by,
    )
    return AclRuleRow.model_validate(dict(row))


_ACL_RULE_PATCHABLE_COLUMNS = frozenset(
    {"ord", "name", "description", "effect", "user_level",
     "caller_pattern", "callee_pattern"}
)


async def update_acl_rule(
    conn: asyncpg.Connection,
    rule_id: str,
    *,
    fields: dict[str, Any],
) -> AclRuleRow | None:
    """Patch one or more columns. Column names are validated against an
    internal allowlist — the dynamic SET clause never interpolates
    user-supplied keys, so SQL injection via column name is impossible.
    Values are bound via $-parameters."""
    if not fields:
        return await get_acl_rule(conn, rule_id)
    bad = set(fields) - _ACL_RULE_PATCHABLE_COLUMNS
    if bad:
        raise ValueError(f"update_acl_rule: disallowed columns {sorted(bad)}")
    columns = list(fields)
    set_clause = ", ".join(f"{c} = ${i+2}" for i, c in enumerate(columns))
    sql = f"UPDATE acl_rules SET {set_clause} WHERE rule_id = $1 RETURNING *"
    row = await conn.fetchrow(sql, rule_id, *(fields[c] for c in columns))
    return AclRuleRow.model_validate(dict(row)) if row else None


async def delete_acl_rule(conn: asyncpg.Connection, rule_id: str) -> bool:
    result = await conn.execute("DELETE FROM acl_rules WHERE rule_id = $1", rule_id)
    return result.endswith(" 1")


async def replace_acl_rules(
    conn: asyncpg.Connection,
    rules: list[dict[str, Any]],
    *,
    created_by: str | None,
) -> int:
    """Atomically swap the entire rule set. Returns row count inserted.

    The supplied `ord` on each rule is HONOURED for relative ordering
    — rules are sorted by `ord` ascending before insert. Storage is
    then dense-packed at consecutive `ord` values starting at 0 so a
    future targeted UPDATE or admin reorder works on contiguous
    integers.

    Pre-R4: the supplied `ord` was silently ignored and rows were
    inserted in caller-list order. An admin submitting `[{ord:10,
    ...},{ord:5,...}]` saw the high-ord rule evaluated FIRST despite
    the lower `ord`, contradicting the "lower-ord wins" first-match
    semantics documented in `docs/acl.md`. R4 second-pass review.

    R6 third-pass review (HIGH): the function takes
    `pg_advisory_xact_lock(_ACL_REPLACE_LOCK_KEY)` at the top of the
    transaction to serialise concurrent admin replaces. Pre-R6 two
    admins racing this endpoint could lose-update each other:
    A's `DELETE` row-locks; B blocks; A commits its full ruleset;
    B's `DELETE` (now seeing A's committed rows) succeeds and B's
    `INSERT`s replace them — A's intent is silently overwritten.
    The advisory lock serialises the whole DELETE+INSERT block, so
    the second admin sees the first's commit before running their
    own.

    The caller is responsible for having validated the rules'
    grammar (use `bp_router.acl.Rule` model).
    """
    # Sort by caller-supplied ord. Stable sort preserves caller list
    # order when two rules share an ord — same tie-break as the
    # `ORDER BY ord ASC` queries elsewhere.
    sorted_rules = sorted(rules, key=lambda r: r.get("ord", 0))
    async with conn.transaction():
        # Advisory lock — released automatically at the end of the
        # transaction. Distinct key from `_AUDIT_LOCK_KEY` so the
        # two locks don't cross-serialise (the admin endpoint
        # `replace_rules` calls BOTH `replace_acl_rules` and
        # `append_audit_event` in one outer transaction; acquiring
        # the audit lock under the ACL lock is safe because the
        # ordering is fixed at the call site).
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1)", _ACL_REPLACE_LOCK_KEY
        )
        await conn.execute("DELETE FROM acl_rules")
        for ord_, rule in enumerate(sorted_rules):
            await conn.execute(
                """
                INSERT INTO acl_rules
                    (rule_id, ord, name, description, effect, user_level,
                     caller_pattern, callee_pattern, created_at, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                _new_id("rule_"),
                ord_,
                rule.get("name"),
                rule.get("description"),
                rule["effect"],
                rule["user_level"],
                rule["caller_pattern"],
                rule["callee_pattern"],
                _now(),
                created_by,
            )
    return len(rules)


async def reorder_acl_rules(
    conn: asyncpg.Connection,
    *,
    new_ords: dict[str, int],
) -> None:
    """Atomically renumber rules. Map of rule_id → new ord.

    Untouched rules are repacked into the lowest unused non-negative
    ords, preserving their original ascending order. Specified rules
    take exactly the requested ord. Raises ValueError on unknown
    rule_ids or duplicate target ords.
    """
    if not new_ords:
        return

    # Reject duplicate target ords up front — otherwise the second
    # write would hit UNIQUE(ord) and the whole transaction aborts.
    if len(set(new_ords.values())) != len(new_ords):
        raise ValueError("reorder targets contain duplicate ords")

    async with conn.transaction():
        rows = await conn.fetch(
            "SELECT rule_id, ord FROM acl_rules ORDER BY ord ASC FOR UPDATE"
        )
        existing_ids = {r["rule_id"] for r in rows}
        unknown = set(new_ords) - existing_ids
        if unknown:
            raise ValueError(f"reorder references unknown rule_ids: {sorted(unknown)}")

        # Phase 1: park every row in negative ord-space so the positive
        # range is free for unconstrained writes.
        await conn.execute("UPDATE acl_rules SET ord = -ord - 1")

        # Phase 2: explicit assignments for the supplied rule_ids.
        for rid, ord_ in new_ords.items():
            await conn.execute(
                "UPDATE acl_rules SET ord = $2 WHERE rule_id = $1", rid, ord_
            )

        # Phase 3: untouched rules get the lowest unused non-negative
        # ords, preserving their original order.
        used = set(new_ords.values())
        next_ord = 0
        untouched = [r for r in rows if r["rule_id"] not in new_ords]
        for r in untouched:
            while next_ord in used:
                next_ord += 1
            await conn.execute(
                "UPDATE acl_rules SET ord = $2 WHERE rule_id = $1",
                r["rule_id"],
                next_ord,
            )
            used.add(next_ord)
            next_ord += 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


_AUDIT_LOCK_KEY = 0x4144_5544_4954_0001  # ascii "ADUDIT" + sentinel
_ACL_REPLACE_LOCK_KEY = 0x4143_4C52_504C_4143  # ascii "ACLRPLAC" + sentinel


_AUDIT_PAYLOAD_MAX_BYTES = 8 * 1024
"""Per-row cap on the JSON-serialised `payload` written to
`audit_log`. Anything over this is REPLACED on the row with a
truncation marker carrying the original size — the chain hash
stays consistent because we hash the marker, not the original
payload. 8 KiB is generous for the structured payloads the
router writes today (kvs of IDs, counts, timestamps); a future
need for larger payloads should split the audit shape rather
than uncap this."""


# Namespaced marker key. R2 PR #134 originally used `_truncated`
# — a legitimate caller writing a payload that happened to have
# `_truncated: True` as a key would round-trip unchanged (under
# 8 KiB), giving audit-log readers no way to distinguish "real
# truncation" from "literal key collision". R5 second-pass review.
# The `__bp_audit_truncated__` shape is unlikely to collide with
# any legitimate caller's payload key — the dunder prefix +
# suffix mirrors Python's reserved-name convention.
AUDIT_TRUNCATION_MARKER_KEY = "__bp_audit_truncated__"


def _maybe_truncate_audit_payload(
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Cap audit_log payload size so a single misbehaving caller
    can't fill the table with multi-MB blobs (each row is hash-
    chained, so a 5 MB payload also adds 5 MB to the SHA input on
    every append). Returns either the original payload unchanged
    OR a small marker dict with size metadata.

    The truncation marker uses the namespaced key
    `AUDIT_TRUNCATION_MARKER_KEY` (a `__bp_audit_truncated__`
    shape) so audit-log readers can detect truncation without
    risk of collision with a legitimate caller's payload key.
    Audit-trail consumers MUST treat any row containing this
    key as truncated regardless of the surrounding shape."""
    import json  # noqa: PLC0415

    if not payload:
        return {}
    encoded_size = len(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    )
    if encoded_size <= _AUDIT_PAYLOAD_MAX_BYTES:
        return payload
    return {
        AUDIT_TRUNCATION_MARKER_KEY: True,
        "original_size_bytes": encoded_size,
        "max_bytes": _AUDIT_PAYLOAD_MAX_BYTES,
    }


async def append_audit_event(
    conn: asyncpg.Connection,
    *,
    actor_kind: str,
    actor_id: str | None,
    event: str,
    target_kind: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Hash-chained append.

    The chain links rows by sha256(prev_hash + body). Concurrent writers
    must agree on the same `prev_hash` to keep the chain linear, which
    means the read-then-write needs to be serialised. We achieve that
    via an advisory lock held for the duration of the write transaction:

      `SELECT pg_advisory_xact_lock(_AUDIT_LOCK_KEY)`

    Why advisory lock instead of `SELECT … FOR UPDATE LIMIT 1`:
    `FOR UPDATE` on the *most recent row* serialises only that row.
    When the table is empty (initial install / fresh DB), there is no
    row to lock, so two concurrent `INSERT`s both observe `prev=None`
    and both insert genesis rows with `prev_hash=""`. The chain then
    has two roots and integrity verification breaks forever. The
    advisory lock works on a sentinel key — no row required — and is
    released automatically when the transaction commits / aborts.
    """
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    # Truncate BEFORE hashing so the chain stays consistent: readers
    # verifying the chain re-hash the row they SEE, which is the
    # truncated form.
    stored_payload = _maybe_truncate_audit_payload(payload)

    async with conn.transaction():
        # Serialise the chain append. The lock is per-transaction; it
        # releases on commit / rollback.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _AUDIT_LOCK_KEY)
        # Pick the predecessor by the monotonic `seq` (bigserial),
        # NOT by `ts`/`event_id`. `ts` is wall-clock (non-monotonic
        # under an NTP step; equal at microsecond resolution within a
        # burst) and `event_id` is a RANDOM uuid — ordering by them
        # could select the wrong last row and fork the chain
        # permanently (the advisory lock serialises the append but
        # cannot fix a non-insertion-ordered head pick). `seq` is
        # assigned at INSERT under this same lock, so DESC LIMIT 1 is
        # exactly the genuinely-last-appended row.
        prev = await conn.fetchrow(
            """
            SELECT self_hash FROM audit_log
            ORDER BY seq DESC
            LIMIT 1
            """
        )
        prev_hash = prev["self_hash"] if prev else ""
        now = _now()
        body = json.dumps(
            {
                "ts": now.isoformat(),
                "actor_kind": actor_kind,
                "actor_id": actor_id,
                "event": event,
                "target_kind": target_kind,
                "target_id": target_id,
                "payload": stored_payload,
                "prev_hash": prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        self_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        await conn.execute(
            """
            INSERT INTO audit_log
                (ts, actor_kind, actor_id, event, target_kind, target_id,
                 payload, prev_hash, self_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            now,
            actor_kind,
            actor_id,
            event,
            target_kind,
            target_id,
            stored_payload,
            prev_hash or None,
            self_hash,
        )


# ---------------------------------------------------------------------------
# LLM presets
# ---------------------------------------------------------------------------


_PRESET_PATCHABLE_COLUMNS = frozenset({
    "description",
    "provider",
    "concrete_model",
    "api_key_ref",
    "api_key",
    "base_url",
    "min_user_level",
    "default_temperature",
    "default_max_tokens",
    "default_provider_options",
    "fallback_preset",
    "max_retries",
})


_PRESET_COLUMNS = (
    "name, description, provider, concrete_model, api_key_ref, api_key, "
    "base_url, min_user_level, default_temperature, default_max_tokens, "
    "default_provider_options, fallback_preset, max_retries, "
    "created_at, updated_at, created_by"
)


async def list_llm_presets(conn: asyncpg.Connection) -> list[LlmPresetRow]:
    rows = await conn.fetch(
        f"SELECT {_PRESET_COLUMNS} FROM llm_presets ORDER BY name",
    )
    return [LlmPresetRow.model_validate(dict(r)) for r in rows]


async def get_llm_preset(
    conn: asyncpg.Connection, name: str
) -> LlmPresetRow | None:
    row = await conn.fetchrow(
        f"SELECT {_PRESET_COLUMNS} FROM llm_presets WHERE name = $1",
        name,
    )
    return LlmPresetRow.model_validate(dict(row)) if row else None


async def insert_llm_preset(
    conn: asyncpg.Connection,
    *,
    name: str,
    description: str | None,
    provider: str,
    concrete_model: str,
    api_key_ref: str,
    min_user_level: str,
    default_temperature: float | None,
    default_max_tokens: int | None,
    default_provider_options: dict[str, Any] | None,
    created_by: str | None,
    api_key: str | None = None,
    base_url: str | None = None,
    fallback_preset: str | None = None,
    max_retries: int = 0,
) -> LlmPresetRow:
    """Insert a new preset. Raises asyncpg.UniqueViolationError on
    duplicate name; the admin API maps that to a 409."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO llm_presets
            (name, description, provider, concrete_model, api_key_ref, api_key,
             base_url, min_user_level, default_temperature, default_max_tokens,
             default_provider_options, fallback_preset, max_retries, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        RETURNING {_PRESET_COLUMNS}
        """,
        name,
        description,
        provider,
        concrete_model,
        api_key_ref,
        api_key,
        base_url,
        min_user_level,
        default_temperature,
        default_max_tokens,
        default_provider_options or {},
        fallback_preset,
        max_retries,
        created_by,
    )
    return LlmPresetRow.model_validate(dict(row))


async def update_llm_preset(
    conn: asyncpg.Connection,
    name: str,
    *,
    fields: dict[str, Any],
) -> LlmPresetRow | None:
    """Patch the columns named in `fields`. The set of allowed columns
    is enforced here — silently ignoring unknown keys would let admin
    API bugs corrupt data, so we raise instead."""
    bad = set(fields) - _PRESET_PATCHABLE_COLUMNS
    if bad:
        raise ValueError(f"unknown preset columns: {sorted(bad)}")
    if not fields:
        return await get_llm_preset(conn, name)

    set_clauses: list[str] = []
    values: list[Any] = []
    for col, val in fields.items():
        values.append(val)
        if col == "default_provider_options":
            # jsonb, nominally non-null. A PATCH carrying explicit JSON `null`
            # would otherwise write SQL NULL, after which the RETURNING row
            # (and every later list/get/load_presets) fails LlmPresetRow
            # validation — poisoning the whole preset read. COALESCE keeps it
            # an object.
            set_clauses.append(
                f"{col} = COALESCE(${len(values)}, '{{}}'::jsonb)"
            )
        else:
            set_clauses.append(f"{col} = ${len(values)}")
    set_clauses.append("updated_at = now()")
    values.append(name)
    sql = f"""
        UPDATE llm_presets
        SET {", ".join(set_clauses)}
        WHERE name = ${len(values)}
        RETURNING {_PRESET_COLUMNS}
    """
    row = await conn.fetchrow(sql, *values)
    return LlmPresetRow.model_validate(dict(row)) if row else None


async def delete_llm_preset(conn: asyncpg.Connection, name: str) -> bool:
    result = await conn.execute("DELETE FROM llm_presets WHERE name = $1", name)
    # asyncpg returns "DELETE n"; match on the leading-space form so we
    # consistently distinguish "1 row" from "11 rows" (the rest of this
    # file uses ` 1`). The PK uniqueness means rowcount is always 0 or
    # 1 here, so this is cosmetic — but cosmetic-as-discipline.
    return result.endswith(" 1")


# ---------------------------------------------------------------------------
# MCP servers (Phase 10a — admin-managed bridge configurations)
# ---------------------------------------------------------------------------


_MCP_SELECT_COLS = (
    "server_id, description, url, transport, auth_kind, "
    "auth_value_ref, auth_header_name, groups, expose_to_llm, "
    "tools_cache, refresh_requested_at, created_at, "
    "last_connected_at, created_by"
)


async def list_mcp_servers(conn: asyncpg.Connection) -> list[McpServerRow]:
    rows = await conn.fetch(
        f"SELECT {_MCP_SELECT_COLS} FROM mcp_servers ORDER BY server_id"
    )
    return [McpServerRow.model_validate(dict(r)) for r in rows]


async def get_mcp_server(
    conn: asyncpg.Connection, server_id: str
) -> McpServerRow | None:
    row = await conn.fetchrow(
        f"SELECT {_MCP_SELECT_COLS} FROM mcp_servers WHERE server_id = $1",
        server_id,
    )
    return McpServerRow.model_validate(dict(row)) if row else None


async def insert_mcp_server(
    conn: asyncpg.Connection,
    *,
    server_id: str,
    description: str,
    url: str,
    transport: str,
    auth_kind: str,
    auth_value_ref: str | None,
    auth_header_name: str | None,
    groups: list[str],
    expose_to_llm: bool,
    created_by: str | None,
) -> McpServerRow:
    row = await conn.fetchrow(
        f"""
        INSERT INTO mcp_servers
            (server_id, description, url, transport, auth_kind,
             auth_value_ref, auth_header_name, groups,
             expose_to_llm, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_MCP_SELECT_COLS}
        """,
        server_id, description, url, transport, auth_kind,
        auth_value_ref, auth_header_name, groups,
        expose_to_llm, created_by,
    )
    assert row is not None
    return McpServerRow.model_validate(dict(row))


async def update_mcp_server(
    conn: asyncpg.Connection,
    server_id: str,
    *,
    description: str | None = None,
    url: str | None = None,
    transport: str | None = None,
    auth_kind: str | None = None,
    auth_value_ref: str | None = None,
    auth_header_name: str | None = None,
    groups: list[str] | None = None,
    expose_to_llm: bool | None = None,
) -> McpServerRow | None:
    """PATCH semantics — only non-None fields are written. The
    `auth_kind = 'none'` case requires the caller to ALSO null out
    `auth_value_ref` and `auth_header_name`; otherwise the DB
    CHECK constraint refuses the row. The admin endpoint enforces
    this app-side."""
    sets: list[str] = []
    args: list[Any] = [server_id]
    if description is not None:
        args.append(description)
        sets.append(f"description = ${len(args)}")
    if url is not None:
        args.append(url)
        sets.append(f"url = ${len(args)}")
    if transport is not None:
        args.append(transport)
        sets.append(f"transport = ${len(args)}")
    if auth_kind is not None:
        args.append(auth_kind)
        sets.append(f"auth_kind = ${len(args)}")
        # When the caller sets auth_kind, ALSO write the
        # ref/header columns (callers pass through what they want;
        # `None` in Python becomes NULL in PostgreSQL).
        args.append(auth_value_ref)
        sets.append(f"auth_value_ref = ${len(args)}")
        args.append(auth_header_name)
        sets.append(f"auth_header_name = ${len(args)}")
    if groups is not None:
        args.append(groups)
        sets.append(f"groups = ${len(args)}")
    if expose_to_llm is not None:
        args.append(expose_to_llm)
        sets.append(f"expose_to_llm = ${len(args)}")
    if not sets:
        return await get_mcp_server(conn, server_id)
    row = await conn.fetchrow(
        f"""
        UPDATE mcp_servers
        SET {', '.join(sets)}
        WHERE server_id = $1
        RETURNING {_MCP_SELECT_COLS}
        """,
        *args,
    )
    return McpServerRow.model_validate(dict(row)) if row else None


async def delete_mcp_server(
    conn: asyncpg.Connection, server_id: str
) -> bool:
    result = await conn.execute(
        "DELETE FROM mcp_servers WHERE server_id = $1", server_id,
    )
    return result.endswith(" 1")


async def mark_mcp_server_refresh_requested(
    conn: asyncpg.Connection, server_id: str
) -> bool:
    """Set `refresh_requested_at = now()` so the bridge picks it up
    on its next poll and re-fetches `tools/list` from the upstream.
    Returns True if a row was updated, False if not found.

    Idempotent — repeated calls just overwrite the timestamp."""
    result = await conn.execute(
        "UPDATE mcp_servers SET refresh_requested_at = now() "
        "WHERE server_id = $1",
        server_id,
    )
    return result.endswith(" 1")


async def record_mcp_server_tools_refreshed(
    conn: asyncpg.Connection,
    server_id: str,
    *,
    tools_cache: dict[str, Any],
) -> bool:
    """Atomically record that the bridge has re-fetched the upstream
    `tools/list` for `server_id`:

      * `tools_cache` ← the new payload (whatever JSONB the bridge
        sends; typically `{"tools": [...]}`).
      * `last_connected_at` ← now() (the bridge had a working MCP
        session to fetch the list).
      * `refresh_requested_at` ← NULL (clears whatever admin click
        triggered the refresh, OR clears a stale signal if the
        bridge just refreshed on its own startup).

    Returns True if a row was updated, False when `server_id` is
    unknown (caller surfaces 404)."""
    result = await conn.execute(
        """
        UPDATE mcp_servers
        SET tools_cache          = $2,
            last_connected_at    = now(),
            refresh_requested_at = NULL
        WHERE server_id = $1
        """,
        server_id,
        tools_cache,
    )
    return result.endswith(" 1")
