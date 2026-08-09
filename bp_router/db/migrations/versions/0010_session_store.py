"""router-managed session store: messages, threads, state, hand-overs, turn queue

Adds the conversation log + session state service specified in
`docs/design/router-managed-session-store.md`. Five tables, all scoped by
`(user_id, session_id)` with `ON DELETE CASCADE` on the session so purge
and the closed-session retention sweep reap conversation data with no
cross-database reconcile (§10.1).

Shape notes that are load-bearing, not incidental:

  * `session_messages.owner_agent_id` is the THREAD key and is stamped by
    the router from the task's active executor — never from the wire. There
    is deliberately no `author_agent_id`: with the owner derived, the author
    IS the owner (§4).
  * `role` carries NO check constraint. The router never interprets a role;
    every read names the roles it wants (§3.3).
  * There is no `incumbent` flag. Retirement is `session_threads.floor_id`,
    a monotonic cursor covering both prefix folding and whole-thread
    retirement (§3.4).
  * `session_id` is nullable: NULL rows are the `user` scope — cross-session
    agent context, the conversational analogue of the file store's
    `persist/` (§3.1). They survive session purge and are reaped by
    `purge_user`.
  * `session_turn_queue` holds at most one `active` row per session, enforced
    by a partial unique index rather than by application logic (§6.4).
"""

from __future__ import annotations

from alembic import op

revision = "0010_session_store"
down_revision = "0009_custom_agent_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE session_messages (
            id              bigserial PRIMARY KEY,
            user_id         text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            session_id      text
                REFERENCES sessions (session_id) ON UPDATE CASCADE ON DELETE CASCADE,
            owner_agent_id  text NOT NULL,
            thread_key      text NOT NULL DEFAULT '',
            role            text NOT NULL,
            content         text NOT NULL,
            hidden          boolean NOT NULL DEFAULT false,
            metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
            task_id         text,
            idempotency_key text,
            redacted_at     timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Read path: a range scan over one thread's partition. `roles` /
    # `redacted_at` / `hidden` are filters applied to the scanned rows.
    op.execute(
        """
        CREATE INDEX ix_session_messages_thread
            ON session_messages (session_id, owner_agent_id, thread_key, id)
        """
    )
    # `user`-scope read path (session_id IS NULL).
    op.execute(
        """
        CREATE INDEX ix_session_messages_user_thread
            ON session_messages (user_id, owner_agent_id, thread_key, id)
            WHERE session_id IS NULL
        """
    )
    # Idempotent append. Keyed on the CALLER'S key, not on (task_id, role):
    # one task legitimately appends several rows of the same role (a
    # tool_call and its tool_result), which a role-keyed constraint would
    # reject.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_messages_idempotency
            ON session_messages (task_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE session_threads (
            user_id          text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            session_id       text
                REFERENCES sessions (session_id) ON UPDATE CASCADE ON DELETE CASCADE,
            owner_agent_id   text NOT NULL,
            thread_key       text NOT NULL DEFAULT '',
            floor_id         bigint NOT NULL DEFAULT 0,
            message_count    bigint NOT NULL DEFAULT 0,
            content_bytes    bigint NOT NULL DEFAULT 0,
            last_message_id  bigint NOT NULL DEFAULT 0,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Session and user scopes need separate uniqueness: NULL session_id
    # never equals itself, so one index cannot cover both.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_threads_session
            ON session_threads (session_id, owner_agent_id, thread_key)
            WHERE session_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_threads_user
            ON session_threads (user_id, owner_agent_id, thread_key)
            WHERE session_id IS NULL
        """
    )
    # Per-user quota: SUM(content_bytes) over a user's threads.
    op.execute(
        "CREATE INDEX ix_session_threads_user ON session_threads (user_id)"
    )

    op.execute(
        """
        CREATE TABLE session_state (
            user_id         text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            session_id      text
                REFERENCES sessions (session_id) ON UPDATE CASCADE ON DELETE CASCADE,
            owner_agent_id  text,
            thread_key      text NOT NULL DEFAULT '',
            key             text NOT NULL,
            value           text,
            version         bigint NOT NULL DEFAULT 1,
            metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Thread state (owner set) and session state (owner NULL) are distinct
    # namespaces; NULL-safe uniqueness needs the partial-index pair again.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_state_thread
            ON session_state (session_id, owner_agent_id, thread_key, key)
            WHERE owner_agent_id IS NOT NULL AND session_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_state_session
            ON session_state (session_id, key)
            WHERE owner_agent_id IS NULL AND session_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_state_user_thread
            ON session_state (user_id, owner_agent_id, thread_key, key)
            WHERE owner_agent_id IS NOT NULL AND session_id IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_state_user
            ON session_state (user_id, key)
            WHERE owner_agent_id IS NULL AND session_id IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE session_handovers (
            id                  bigserial PRIMARY KEY,
            user_id             text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            session_id          text
                REFERENCES sessions (session_id) ON UPDATE CASCADE ON DELETE CASCADE,
            target_agent_id     text NOT NULL,
            thread_key          text NOT NULL DEFAULT '',
            from_agent_id       text,
            item_kind           text NOT NULL,
            payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            consumed_at         timestamptz,
            consumed_by_task_id text
        )
        """
    )
    # Partial, so a drain never scans consumed rows.
    op.execute(
        """
        CREATE INDEX ix_session_handovers_pending
            ON session_handovers (session_id, target_agent_id, thread_key, id)
            WHERE consumed_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_session_handovers_pending_user
            ON session_handovers (user_id, target_agent_id, thread_key, id)
            WHERE consumed_at IS NULL AND session_id IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE session_turn_queue (
            ticket      bigserial PRIMARY KEY,
            session_id  text NOT NULL
                REFERENCES sessions (session_id) ON UPDATE CASCADE ON DELETE CASCADE,
            user_id     text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            holder_id   text NOT NULL,
            agent_id    text NOT NULL,
            state       text NOT NULL CHECK (state IN ('waiting', 'active')),
            expires_at  timestamptz NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # At most one holder per session — a database guarantee, not a code path.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_turn_queue_active
            ON session_turn_queue (session_id)
            WHERE state = 'active'
        """
    )
    # One row per (session, holder): re-acquiring is idempotent.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_session_turn_queue_holder
            ON session_turn_queue (session_id, holder_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_session_turn_queue_waiting
            ON session_turn_queue (session_id, ticket)
            WHERE state = 'waiting'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_turn_queue")
    op.execute("DROP TABLE IF EXISTS session_handovers")
    op.execute("DROP TABLE IF EXISTS session_state")
    op.execute("DROP TABLE IF EXISTS session_threads")
    op.execute("DROP TABLE IF EXISTS session_messages")
