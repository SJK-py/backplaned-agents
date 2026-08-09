"""user_llm_preferences — the user's per-slot model choice

Implements `docs/design/router-resolved-preset-slots.md` §4.

A **slot** is an opaque key ("balanced", "pro", …) naming a preference the
router resolves to a preset. This table holds the user's half of that
decision; the operator's half is `Settings.llm_default_presets`, and the
ceiling is each preset's `min_user_level`.

Why its own table rather than the session store's user-scoped state (which
already provides an opaque `(user_id, key)` namespace): that namespace is
writable by any agent acting in the user's session, and the router *acts* on
this value — it selects a model, at a cost, under a tier gate. A value the
router enforces policy on must not be one any agent can overwrite. Writes
come only from the session-JWT endpoints (`/v1/llm/preferences`); there is
no agent-facing write path.

`preset_embedding` is deliberately NOT migrated here and embedding is not a
slot: changing an embedding model invalidates every vector already written,
silently, with no migration path (design §12).
"""

from __future__ import annotations

from alembic import op

revision = "0011_user_llm_preferences"
down_revision = "0010_session_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE user_llm_preferences (
            user_id     text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            slot        text NOT NULL,
            preset_name text NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, slot)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_llm_preferences")
