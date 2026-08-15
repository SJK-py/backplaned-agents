"""drop the user_config columns that moved into the router's user scope

Six settings — `full_name`, `timezone`, `language`, `verbose_default`,
`custom_note`, `max_context_token_limit` — are now keys in the router's
user-scoped state, the `(user_id, key)` namespace the session store exposes
under `scope="user"` (`docs/design/router-managed-session-store.md` §3.1).
Reading them no longer needs a database credential, which is what let the l1
specialists drop their suite pools entirely.

The split is by READER, not by kind, and this migration is where that shows:

  * these six are read INSIDE a task (at turn start, to build a system
    prompt) or by a steward that already holds a carrier session, so both
    paths into the user scope are available;
  * `sandbox_uid` and `default_session_id` STAY. They are read outside any
    task — by the sandbox host and the cron scheduler, neither of which has a
    `ctx.history` or a session to ride — and `sandbox_uid` additionally needs
    cross-user uniqueness that a per-key namespace does not give.

No data is migrated, and it could not be: the destination is a different
database, reachable only over the router's authenticated surfaces, so there
is no transaction that could span both. Existing users fall back to the
operator defaults (`SUITE_DEFAULT_TIMEZONE`, `SUITE_DEFAULT_LANGUAGE`,
`SUITE_DEFAULT_MAX_CONTEXT_TOKEN_LIMIT`) — an absent key IS the default in
the new shape — and re-set anything they had customised. That is the
accepted cost recorded in the design; the alternative is a one-off backfill
script, which belongs outside a schema migration if it is ever wanted.

`downgrade` restores the columns with their original defaults. The values
are not recoverable, which is the honest outcome for columns whose contents
were deliberately not carried forward.

Revision ID: 0006_user_config_to_user_scope
Revises: 0005_drop_session_tables
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0006_user_config_to_user_scope"
down_revision = "0005_drop_session_tables"
branch_labels = None
depends_on = None

# column -> its original DDL fragment, so `downgrade` restores the exact
# shape `0001_suite_initial` declared rather than an approximation.
_COLUMNS = {
    "full_name": "text NOT NULL DEFAULT ''",
    "timezone": "text NOT NULL DEFAULT 'UTC'",
    "max_context_token_limit": "integer NOT NULL DEFAULT 120000",
    "verbose_default": "boolean NOT NULL DEFAULT false",
    "language": "text NOT NULL DEFAULT 'en'",
    "custom_note": "text NOT NULL DEFAULT ''",
}


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE user_config DROP COLUMN IF EXISTS {column}")


def downgrade() -> None:
    for column, ddl in _COLUMNS.items():
        op.execute(f"ALTER TABLE user_config ADD COLUMN IF NOT EXISTS {column} {ddl}")
