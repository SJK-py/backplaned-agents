"""drop session_history + session_info — conversation moved to the router

The suite kept the conversation in `session_history` (one row per turn, per
agent thread) and its descriptors in `session_info` (channel, chat id, title,
rolling summaries, `delegated_to`). Both are now the router's, in the session
store `docs/design/router-managed-session-store.md` specifies:

  * turns → `session_messages`, written only by the agent that owns the
    thread. There is no way to represent writing another agent's thread, which
    is the property the whole move exists for.
  * `history_summary` / `delegate_summary` → that thread's own state, applied
    with its floor in one batch.
  * `delegated_to` → session-scoped state.
  * `channel` / `chat_id` / `session_name` → the router session's `metadata`
    (`kind` / `external_id` / `title`) — the same field its serviced-session
    discovery already read, so the suite copy was always a shadow.

No data is migrated. Conversations are live state, not records to preserve
across an architecture change, and there is no honest mapping for the ones
mid-delegation: a `session_info.delegated_to` pointing at a thread whose rows
would have to be re-attributed to an owner that never wrote them. Existing
sessions therefore start empty in the store and fill from their next turn.

`downgrade` recreates both tables, empty, with their original shape.

Revision ID: 0005_drop_session_tables
Revises: 0004_drop_user_config_presets
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0005_drop_session_tables"
down_revision = "0004_drop_user_config_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_history")
    op.execute("DROP TABLE IF EXISTS session_info")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_info (
            session_id       text PRIMARY KEY,
            user_id          text NOT NULL,
            channel          text,
            chat_id          text,
            session_name     text,
            history_summary  text,
            delegate_summary text,
            delegated_to     text,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_info_user_idx "
        "ON session_info (user_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_history (
            id          bigserial PRIMARY KEY,
            session_id  text NOT NULL,
            agent_id    text NOT NULL,
            role        text NOT NULL,
            message     text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            incumbent   boolean NOT NULL DEFAULT true,
            hidden      boolean NOT NULL DEFAULT false
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_history_thread_idx "
        "ON session_history (session_id, agent_id, id)"
    )
