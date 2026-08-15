"""suite initial schema (consolidated — pre-release baseline)

The agent suite's ENTIRE Postgres schema, as one migration. Four tables:
`user_config`, `suite_platform_mappings`, `cron_jobs`, `cron_executions`.

**This baseline is not upgradable-to.** It was consolidated a second time,
absorbing what had been migrations 0002–0006, so a database created by the
previous chain has an `alembic_version` naming a revision that no longer
exists and `alembic upgrade head` fails against it rather than doing
something subtle. That is deliberate: the codebase is pre-release, no
deployment carries data worth a rewrite path, and a fabricated "upgrade"
from an unknown intermediate is a worse promise than a clear stop.
**Existing installations must be recreated from empty.** Post-release
schema changes get fresh sequence numbers (0002+) chaining off this file.

Two whole tables the old chain created and then dropped are simply ABSENT
here, and the reasons are worth keeping because they explain why the suite's
schema is this small:

  * `session_history` / `session_info` — the conversation and its
    descriptors are the ROUTER's now, in the store
    `docs/design/router-managed-session-store.md` specifies. Turns became
    `session_messages`, written only by the agent that owns the thread;
    the rolling summaries became that thread's own state; `delegated_to`
    became session-scoped state; and `channel` / `chat_id` /
    `session_name` became the router session's `metadata`, which its
    serviced-session discovery already read — so the suite copy was always
    a shadow.
  * Six `user_config` settings — `full_name`, `timezone`, `language`,
    `verbose_default`, `custom_note`, `max_context_token_limit` — became
    keys in the router's user-scoped state (§3.1 of the same design).
    Reading them no longer needs a database credential, which is what let
    the l1 specialists drop their suite pools entirely. The four
    `preset_*` columns went further and are gone with no replacement here:
    model choice is a router-resolved preset slot
    (`docs/design/router-resolved-preset-slots.md`), so the *choice* and
    the *tier entitlement* finally live in the same place.

What remains in `user_config` remains for one reason: it is read OUTSIDE
any task, by a caller with neither a `ctx.history` nor a session to ride.
See the table's own comment.

Revision ID: 0001_suite_initial
Revises:
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op

revision = "0001_suite_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # user_config — one row per user.
    # ------------------------------------------------------------------
    # Deliberately two settings wide. Everything a turn reads moved to the
    # router's user scope; what is left is what a NON-task reader needs:
    #
    #   * `sandbox_uid` — read by the sandbox host, which is network-isolated
    #     from this database, and which needs cross-user UNIQUENESS that a
    #     per-key namespace does not give. Currently DEAD in this table: the
    #     sandbox owns per-user uids in its own local JSON store. Kept as the
    #     column of record for the identifier.
    #   * `default_session_id` — the cron fallback pointer ([cron.md] §2),
    #     read by the scheduler, which fires outside any session. Inbound
    #     chat routing does NOT use it; that reads
    #     `suite_platform_mappings.session_id` below.
    op.execute("""
        CREATE TABLE user_config (
            user_id                  text PRIMARY KEY,
            sandbox_uid              integer,
            default_session_id       text,
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ------------------------------------------------------------------
    # suite_platform_mappings — inbound identity (chat_id → user_id).
    # ------------------------------------------------------------------
    # `session_id` is the chat's OWN live session, so a user who links two
    # channels (Telegram + KakaoTalk via `/link`) keeps a separate
    # conversation on each instead of both interleaving into one. Nullable:
    # a chat that has not started one yet falls back to
    # `user_config.default_session_id`.
    op.execute("""
        CREATE TABLE suite_platform_mappings (
            platform    text NOT NULL
                        CHECK (platform IN ('telegram', 'web', 'kakao')),
            chat_id     text NOT NULL,
            user_id     text NOT NULL,
            session_id  text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (platform, chat_id)
        )
    """)
    op.execute(
        "CREATE INDEX suite_platform_mappings_user_idx "
        "ON suite_platform_mappings(user_id)"
    )

    # ------------------------------------------------------------------
    # cron_jobs — scheduled per-session prompts ([data-model.md] §1.4).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE cron_jobs (
            cron_id           text PRIMARY KEY,
            user_id           text NOT NULL,
            session_id        text NOT NULL,
            cron_expression   text NOT NULL,
            timezone          text NOT NULL DEFAULT 'UTC',
            report            text NOT NULL DEFAULT 'case_by_case'
                              CHECK (report IN ('always', 'never', 'case_by_case')),
            cron_message      text NOT NULL,
            status            text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'inactive')),
            execute_until     timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            last_executed_at  timestamptz
        )
    """)
    op.execute("CREATE INDEX cron_jobs_user_idx ON cron_jobs(user_id)")
    # The scheduler scans active jobs; partial index keeps that cheap.
    op.execute(
        "CREATE INDEX cron_jobs_active_idx ON cron_jobs(status) "
        "WHERE status = 'active'"
    )

    # ------------------------------------------------------------------
    # cron_executions — one row per firing ([data-model.md] §1.5).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE cron_executions (
            id          bigserial PRIMARY KEY,
            cron_id     text NOT NULL,
            user_id     text NOT NULL,
            session_id  text NOT NULL,
            fired_at    timestamptz NOT NULL DEFAULT now(),
            reported    boolean NOT NULL,
            reason      text,
            message     text,
            error       text
        )
    """)
    op.execute("CREATE INDEX cron_executions_cron_idx ON cron_executions(cron_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cron_executions")
    op.execute("DROP TABLE IF EXISTS cron_jobs")
    op.execute("DROP TABLE IF EXISTS suite_platform_mappings")
    op.execute("DROP TABLE IF EXISTS user_config")
