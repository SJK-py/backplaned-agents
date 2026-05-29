"""evicted agent_id reuse: ON UPDATE CASCADE on agent + user FKs

Eviction now *renames* the agent's primary key to a tombstone
(`deleted_<id>_<epoch>`) instead of leaving a `removed` row squatting on
the original `agent_id` forever — freeing the id for a brand-new agent to
onboard. The same applies to the agent's co-located service principal
(`usr_service_<id>` in `users`) so a CHANNEL agent's id is reusable too.

A PK rename only propagates to dependent rows when the foreign keys carry
`ON UPDATE CASCADE`; all FKs to `agents(agent_id)` and `users(user_id)`
were created `NO ACTION` (the v1 baseline), so a bare rename would error.
This migration recreates each as `ON UPDATE CASCADE` (delete behaviour
unchanged — only `password_reset_tokens.user_id` keeps its `ON DELETE
CASCADE`). No data change; constraint redefinition only.

Revision ID: 0002_fk_on_update_cascade
Revises: 0001_initial_schema
Create Date: 2026-05-29
"""

from __future__ import annotations

from alembic import op

revision = "0002_fk_on_update_cascade"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


# (constraint, table, column, ref_table, ref_col, on_delete) — every FK that
# must cascade a primary-key rename. Delete behaviour is preserved verbatim.
_FKS = [
    # → agents(agent_id)
    ("tasks_agent_id_fkey", "tasks", "agent_id", "agents", "agent_id", None),
    ("tasks_caller_agent_id_fkey", "tasks", "caller_agent_id", "agents", "agent_id", None),
    ("tasks_active_agent_id_fkey", "tasks", "active_agent_id", "agents", "agent_id", None),
    # → users(user_id)
    ("sessions_user_id_fkey", "sessions", "user_id", "users", "user_id", None),
    ("tasks_user_id_fkey", "tasks", "user_id", "users", "user_id", None),
    ("files_user_id_fkey", "files", "user_id", "users", "user_id", None),
    ("file_names_user_id_fkey", "file_names", "user_id", "users", "user_id", None),
    ("acl_rules_created_by_fkey", "acl_rules", "created_by", "users", "user_id", None),
    ("invitations_created_by_fkey", "invitations", "created_by", "users", "user_id", None),
    ("auth_refresh_tokens_user_id_fkey", "auth_refresh_tokens", "user_id", "users", "user_id", None),
    ("password_reset_tokens_user_id_fkey", "password_reset_tokens", "user_id", "users", "user_id", "CASCADE"),
    ("password_reset_tokens_created_by_fkey", "password_reset_tokens", "created_by", "users", "user_id", None),
    ("llm_presets_created_by_fkey", "llm_presets", "created_by", "users", "user_id", None),
    ("pending_user_registrations_submitted_by_service_user_id_fkey",
     "pending_user_registrations", "submitted_by_service_user_id", "users", "user_id", None),
    ("mcp_servers_created_by_fkey", "mcp_servers", "created_by", "users", "user_id", None),
]


def _add(con: str, tbl: str, col: str, rtbl: str, rcol: str,
         on_delete: str | None, on_update: str | None) -> None:
    clause = ""
    if on_update:
        clause += f" ON UPDATE {on_update}"
    if on_delete:
        clause += f" ON DELETE {on_delete}"
    op.execute(
        f"ALTER TABLE {tbl} ADD CONSTRAINT {con} "
        f"FOREIGN KEY ({col}) REFERENCES {rtbl}({rcol}){clause}"
    )


def upgrade() -> None:
    for con, tbl, col, rtbl, rcol, on_delete in _FKS:
        op.execute(f"ALTER TABLE {tbl} DROP CONSTRAINT {con}")
        _add(con, tbl, col, rtbl, rcol, on_delete, on_update="CASCADE")


def downgrade() -> None:
    for con, tbl, col, rtbl, rcol, on_delete in _FKS:
        op.execute(f"ALTER TABLE {tbl} DROP CONSTRAINT {con}")
        _add(con, tbl, col, rtbl, rcol, on_delete, on_update=None)
