"""drop user_config.preset_* — model choice moved to router preset slots

The suite stored four preset NAMES per user (`preset_pro`, `preset_balanced`,
`preset_lite`, `preset_embedding`) and every agent read them at turn start to
put an explicit preset on each LLM call. That made the *choice* suite state
and the *entitlement* router state, with neither aware of the other: a menu
built from a static operator allow-list could offer a model the user's tier
forbade, and the refusal then landed mid-conversation on the next turn.

Model choice is now a router-resolved preset **slot**
(`docs/design/router-resolved-preset-slots.md`): the agent sends an opaque
slot key, and the router picks the preset from the user's own preference
(`user_llm_preferences`, router-side, written only under the user's session
JWT) intersected with their tier gate.

`preset_embedding` is **dropped, not migrated**. It was never user-selectable
and must not become so — changing an embedding model silently invalidates
every vector already written to that user's LanceDB (design §12). The
embedding model is operator configuration
(`SUITE_DEFAULT_PRESET_EMBEDDING`) on the explicit `preset=` path.

The three chat presets are likewise dropped rather than copied into
`user_llm_preferences`: their stored values are overwhelmingly the seeded
operator default, which the slot resolution already produces for a user with
no preference. Copying them would manufacture an explicit preference nobody
made — and pin every user to today's model forever.

`downgrade` restores the columns with their original defaults; the values are
not recoverable, which is the honest outcome for a column whose contents were
deliberately not carried forward.

Revision ID: 0004_drop_user_config_presets
Revises: 0003_per_chat_session
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0004_drop_user_config_presets"
down_revision = "0003_per_chat_session"
branch_labels = None
depends_on = None

_COLUMNS = ("preset_pro", "preset_balanced", "preset_lite", "preset_embedding")

_DEFAULTS = {
    "preset_pro": "default",
    "preset_balanced": "default",
    "preset_lite": "default",
    "preset_embedding": "default_embedding",
}


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE user_config DROP COLUMN IF EXISTS {column}")


def downgrade() -> None:
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE user_config ADD COLUMN IF NOT EXISTS {column} "
            f"text NOT NULL DEFAULT '{_DEFAULTS[column]}'"
        )
