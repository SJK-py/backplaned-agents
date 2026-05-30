"""initial schema (consolidated v1 — pre-release)

Creates the full set of tables defined in
docs/router/storage.md §1.1: users, sessions, agents, tasks,
task_events, files, acl_rules, audit_log, invitations,
auth_refresh_tokens, llm_presets, plus pending_user_registrations,
registration_attempts, password_reset_tokens and mcp_servers.

This is a CONSOLIDATED v1 baseline. The codebase is pre-release;
no production (or derivative) deployment carries an intermediate
schema, so the historical incremental migrations have been folded
into this single file — a fresh deployment runs ONE migration and
lands directly on the final schema. Post-release schema changes
will get fresh sequence numbers (0002+).

Folded in (previously standalone migrations 0002–0008):
  * 0002 — tasks.caller_agent_id / active_agent_id (NOT NULL, FK
    agents, indexed). Declared inline here (fresh schema → no
    nullable-then-backfill dance, no FK on a transient state).
  * 0003 — users.serviced_by; pending_user_registrations;
    registration_attempts.
  * 0004 — password_reset_tokens.
  * 0005 — users.deleted_at + active-rows partial index.
  * 0006 — mcp_servers.
  * 0007 — audit_log(actor_id, ts DESC) partial index. Created
    here as a plain CREATE INDEX: the standalone migration used
    CREATE INDEX CONCURRENTLY (+ autocommit_block) only to avoid
    AccessExclusiveLock on a populated audit_log during an online
    migration. On the initial empty schema that concern does not
    apply and a plain index keeps the whole migration in one
    transaction (the correct shape for a baseline).
  * 0008 — acl_rules caller/callee pattern CHECKs use the relaxed
    Phase-10 prefix-glob regex, with explicit constraint names so
    a future relaxation has a stable handle. The pre-0008 (strict)
    regex is not reproduced — a consolidated baseline has no
    history to be faithful to, only the final shape.
  * fk-cascade — every FK to agents(agent_id) / users(user_id) is
    declared `ON UPDATE CASCADE` inline (15 constraints) so an
    agent/service-principal PK rename on eviction propagates to
    dependent rows. Delete behaviour is unchanged — only
    password_reset_tokens.user_id keeps ON DELETE CASCADE and
    pending_user_registrations.submitted_by_service_user_id keeps
    ON DELETE SET NULL. The standalone migration recreated the FKs
    with ALTER; on a fresh schema they're declared cascading from
    the start.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


# acl_rules caller/callee pattern regex (Phase-10 prefix-glob aware).
# Raw string so the single backslashes land verbatim in the SQL
# string literal; applied via an explicitly-named ADD CONSTRAINT
# (not an inline column CHECK) so the constraint has a stable
# handle for any future relaxation. Three alternatives for the
# slash-form capability half:
#   * the whole-token `*`
#   * a full dotted capability (one or more `.segment`s)
#   * a prefix-glob: zero or more `.segment`s, then `.*`
_ACL_PATTERN_REGEX = (
    r"^(@[A-Za-z_][A-Za-z0-9_-]{0,63}"
    r"|(\*|[a-z][a-z0-9_:.-]{0,63})"
    r"/(\*"
    r"|[a-z][a-z0-9_]*(\.[a-z0-9_]+)+"
    r"|[a-z][a-z0-9_]*(\.[a-z0-9_]+)*\.\*))$"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    # `serviced_by` (folded from 0003): list of service-principal
    # user_ids authorised to mint credentials for this user.
    # Default-deny — an empty array means no principal can mint.
    # `deleted_at` (folded from 0005): terminal admin soft-delete
    # (distinct from the reversible `suspended_at`); the row stays
    # so the nine `REFERENCES users(user_id)` FKs — audit_log
    # attribution in particular — survive the delete.
    op.execute("""
        CREATE TABLE users (
            user_id            text PRIMARY KEY,
            level              text NOT NULL CHECK (level ~ '^(admin|service|tier[0-9]+)$'),
            auth_kind          text NOT NULL CHECK (auth_kind IN ('password','oidc','api_key')),
            auth_secret_hash   text,
            email              text UNIQUE,
            created_at         timestamptz NOT NULL DEFAULT now(),
            suspended_at       timestamptz,
            serviced_by        text[] NOT NULL DEFAULT '{}',
            deleted_at         timestamptz
        )
    """)
    op.execute("CREATE INDEX users_level_idx ON users(level)")
    # GIN makes `WHERE $1 = ANY(serviced_by)` / `serviced_by @> ARRAY[$1]`
    # index-backed (reverse + admin-listing lookups).
    op.execute("CREATE INDEX users_serviced_by_idx ON users USING gin (serviced_by)")
    # Partial index for the common admin-list filter
    # `WHERE deleted_at IS NULL ORDER BY created_at DESC`.
    op.execute(
        "CREATE INDEX users_active_idx "
        "ON users (created_at DESC) WHERE deleted_at IS NULL"
    )

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE sessions (
            session_id   text PRIMARY KEY,
            user_id      text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
            opened_at    timestamptz NOT NULL DEFAULT now(),
            closed_at    timestamptz,
            metadata     jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX sessions_user_idx ON sessions(user_id, opened_at DESC)")

    # ------------------------------------------------------------------
    # agents
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE agents (
            agent_id              text PRIMARY KEY
                                  CHECK (agent_id ~ '^[A-Za-z_][A-Za-z0-9_-]{0,63}$'),
            kind                  text NOT NULL CHECK (kind IN ('external','embedded')),
            status                text NOT NULL CHECK (status IN ('active','suspended','pending','removed')),
            capabilities          jsonb NOT NULL DEFAULT '[]'::jsonb,
            groups                jsonb NOT NULL DEFAULT '[]'::jsonb,
            agent_info            jsonb NOT NULL DEFAULT '{}'::jsonb,
            auth_token_hash       text,
            public_key            text,
            registered_at         timestamptz NOT NULL DEFAULT now(),
            last_seen_at          timestamptz
        )
    """)
    op.execute("CREATE INDEX agents_status_idx ON agents(status)")
    op.execute("CREATE INDEX agents_capabilities_idx ON agents USING gin (capabilities)")
    op.execute("CREATE INDEX agents_groups_idx ON agents USING gin (groups)")

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    # `caller_agent_id` / `active_agent_id` (folded from 0002):
    # declared inline NOT NULL + FK here. The standalone migration
    # added them nullable, backfilled from `agent_id`, then enforced
    # NOT NULL/FK — a dance only needed for a populated table. On a
    # fresh schema the final shape is declared directly.
    op.execute("""
        CREATE TABLE tasks (
            task_id          text PRIMARY KEY,
            parent_task_id   text REFERENCES tasks(task_id),
            root_task_id     text NOT NULL,
            user_id          text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
            session_id       text NOT NULL REFERENCES sessions(session_id),
            agent_id         text NOT NULL REFERENCES agents(agent_id) ON UPDATE CASCADE,
            caller_agent_id  text NOT NULL REFERENCES agents(agent_id) ON UPDATE CASCADE,
            active_agent_id  text NOT NULL REFERENCES agents(agent_id) ON UPDATE CASCADE,
            state            text NOT NULL CHECK (state IN (
                'QUEUED','RUNNING','WAITING_CHILDREN',
                'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'
            )),
            status_code      int,
            idempotency_key  text,
            priority         text NOT NULL DEFAULT 'normal',
            deadline         timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            input            jsonb NOT NULL DEFAULT '{}'::jsonb,
            output           jsonb,
            error            jsonb,
            CONSTRAINT tasks_idempotency_unique UNIQUE (caller_agent_id, user_id, idempotency_key),
            CONSTRAINT tasks_deadline_after_create CHECK (deadline IS NULL OR deadline > created_at)
        )
    """)
    op.execute("CREATE INDEX tasks_user_state_idx ON tasks(user_id, state)")
    op.execute("CREATE INDEX tasks_session_idx ON tasks(session_id, created_at DESC)")
    op.execute("CREATE INDEX tasks_parent_idx ON tasks(parent_task_id)")
    op.execute("CREATE INDEX tasks_caller_idx ON tasks(caller_agent_id)")
    op.execute("CREATE INDEX tasks_active_agent_idx ON tasks(active_agent_id)")
    op.execute("""
        CREATE INDEX tasks_active_idx ON tasks(state)
        WHERE state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
    """)
    # Backs the deadline sweep's `find_expired_tasks` hot query:
    #   WHERE deadline IS NOT NULL
    #     AND deadline < $1
    #     AND state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
    #   ORDER BY deadline ASC LIMIT $2
    # Keyed on `deadline` (not `state`) so the range predicate +
    # ORDER BY + LIMIT resolves as one bounded, already-sorted index
    # scan instead of scan-then-sort. Partial on the same
    # non-terminal + has-deadline slice the sweep ever touches —
    # `tasks_active_idx` above only covers the state filter and
    # leaves the deadline range/sort unindexed (a full sort of every
    # active task on every 5 s tick at scale).
    op.execute("""
        CREATE INDEX tasks_deadline_sweep_idx ON tasks(deadline)
        WHERE deadline IS NOT NULL
          AND state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
    """)

    # ------------------------------------------------------------------
    # task_events
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE task_events (
            event_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id          text NOT NULL REFERENCES tasks(task_id),
            ts               timestamptz NOT NULL DEFAULT now(),
            kind             text NOT NULL,
            actor_agent_id   text,
            from_state       text,
            to_state         text,
            payload          jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX task_events_task_ts_idx ON task_events(task_id, ts)")

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE files (
            file_id            text PRIMARY KEY,
            sha256             text NOT NULL,
            user_id            text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
            session_id         text REFERENCES sessions(session_id),
            task_id            text REFERENCES tasks(task_id),
            byte_size          bigint NOT NULL,
            mime_type          text,
            storage_url        text NOT NULL,
            original_filename  text,
            created_at         timestamptz NOT NULL DEFAULT now(),
            expires_at         timestamptz
        )
    """)
    op.execute("CREATE UNIQUE INDEX files_user_sha_idx ON files(user_id, sha256)")
    op.execute("CREATE INDEX files_expires_idx ON files(expires_at) WHERE expires_at IS NOT NULL")

    # ------------------------------------------------------------------
    # file_names — named directory over the content-addressed `files`
    # blob registry (router-managed file store; see
    # docs/design/router-managed-file-store.md).
    #
    # A row maps a (user_id, scope, filename) NAME to a `files`
    # blob row. `scope` is 'session:{session_id}' (the ephemeral
    # baseline, GC'd on session close) or 'persist' (user-wide,
    # survives every session). The PRIMARY KEY is the atomic
    # name-allocation guard: two concurrent stores of the same name
    # can't both land — one gets the unique-violation and bumps the
    # dedup counter. `byte_size` is denormalised from `files` so the
    # per-user storage-quota SUM is a single-table scan (no join);
    # it's immutable for a blob, updated only when a name is
    # repointed (overwrite). `file_id` FK is the blob pointer; a
    # blob is GC-collectable when no file_names row references it
    # (refcount via `count_names_for_file`).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE file_names (
            user_id     text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
            scope       text NOT NULL,
            filename    text NOT NULL,
            file_id     text NOT NULL REFERENCES files(file_id),
            byte_size   bigint NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, scope, filename)
        )
    """)
    # The PK index already covers the `user_id` (quota SUM) and
    # `(user_id, scope)` (list / session-GC) leftmost prefixes; only
    # the file_id refcount lookup needs its own index.
    op.execute("CREATE INDEX file_names_file_idx ON file_names(file_id)")

    # ------------------------------------------------------------------
    # acl_rules
    # ------------------------------------------------------------------
    # caller_pattern / callee_pattern CHECKs are added AFTER the
    # CREATE (folded from 0008): explicitly-named constraints
    # carrying the Phase-10 prefix-glob-aware regex. Inline,
    # column-level CHECKs would be Postgres-auto-named and the
    # baseline would have no stable handle for a future relaxation.
    op.execute("""
        CREATE TABLE acl_rules (
            rule_id         text PRIMARY KEY,
            ord             int  NOT NULL UNIQUE,
            name            text,
            description     text,
            effect          text NOT NULL CHECK (effect IN ('allow','deny')),
            user_level      text NOT NULL
                            CHECK (user_level ~ '^(\\*|admin|service|tier[0-9]+)$'),
            caller_pattern  text NOT NULL,
            callee_pattern  text NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            created_by      text REFERENCES users(user_id) ON UPDATE CASCADE
        )
    """)
    op.execute(
        f"ALTER TABLE acl_rules ADD CONSTRAINT acl_rules_caller_pattern_check "
        f"CHECK (caller_pattern ~ '{_ACL_PATTERN_REGEX}')"
    )
    op.execute(
        f"ALTER TABLE acl_rules ADD CONSTRAINT acl_rules_callee_pattern_check "
        f"CHECK (callee_pattern ~ '{_ACL_PATTERN_REGEX}')"
    )
    op.execute("CREATE INDEX acl_rules_ord_idx ON acl_rules(ord)")

    # Bootstrap rules — see docs/acl.md §13. Three rows in evaluation
    # order:
    #   ord 0  allow * admin/* -> admin/*    (admin agents may call admin agents)
    #   ord 1  deny  * */*     -> admin/*    (only admin agents may call admin agents)
    #   ord 2  allow * */*     -> */*        (default permissive — replace with real policy)
    # The first two protect the synthetic `admin_console` caller used by
    # POST /v1/admin/tasks/test and any future admin-only embedded
    # agents. Admin should remove or tighten the permissive rule at
    # ord 2 before going to production.
    op.execute("""
        INSERT INTO acl_rules
            (rule_id, ord, name, description,
             effect, user_level, caller_pattern, callee_pattern)
        VALUES
            ('rule_bootstrap_admin_loop', 0, 'admin-loop',
             'Admin-group agents may call other admin-group agents.',
             'allow', '*', 'admin/*', 'admin/*'),
            ('rule_bootstrap_admin_protect', 1, 'admin-protect',
             'Non-admin callers may not reach admin-group agents.',
             'deny',  '*', '*/*',     'admin/*'),
            ('rule_bootstrap_default', 2, 'bootstrap',
             'Default install rule — admin should replace with real policy.',
             'allow', '*', '*/*',     '*/*')
    """)

    # ------------------------------------------------------------------
    # audit_log (hash-chained, append-only)
    # ------------------------------------------------------------------
    # `seq bigserial` is the chain-order key. The hash chain links
    # rows by sha256(prev_hash + body); `append_audit_event` must
    # pick the genuinely last-appended row as `prev`. Neither
    # `event_id` (random gen_random_uuid()) nor `ts` (wall clock —
    # non-monotonic under NTP step / equal to microsecond resolution
    # on a burst) is insertion-ordered, so an `ORDER BY ts, event_id`
    # head pick could select the wrong predecessor and FORK the
    # chain. `bigserial` is assigned at INSERT under the append's
    # advisory lock, so it is strictly monotonic in chain order.
    op.execute("""
        CREATE TABLE audit_log (
            event_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq          bigserial NOT NULL,
            ts           timestamptz NOT NULL DEFAULT now(),
            actor_kind   text NOT NULL,
            actor_id     text,
            event        text NOT NULL,
            target_kind  text,
            target_id    text,
            payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
            prev_hash    text,
            self_hash    text NOT NULL
        )
    """)
    # UNIQUE both enforces the no-duplicate-position invariant and
    # serves `append_audit_event`'s `ORDER BY seq DESC LIMIT 1` head
    # pick as a backward index scan (hot path: every audit append).
    op.execute("CREATE UNIQUE INDEX audit_log_seq_idx ON audit_log(seq)")
    op.execute("CREATE INDEX audit_log_ts_idx ON audit_log(ts DESC)")
    op.execute("CREATE INDEX audit_log_event_idx ON audit_log(event, ts DESC)")
    # (actor_id, ts DESC) partial index (folded from 0007): backs the
    # admin `/admin/audit?actor_id=...` filter + user/agent detail
    # views. Partial because actor_id is NULL for system events
    # (queried by actor_kind instead). Plain CREATE INDEX — the
    # CONCURRENTLY form in the standalone migration was only to avoid
    # locking a populated table; irrelevant on the initial schema.
    op.execute(
        "CREATE INDEX audit_log_actor_ts_idx "
        "ON audit_log (actor_id, ts DESC) WHERE actor_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # invitations
    # ------------------------------------------------------------------
    # `used_by` is plain text — NOT a FK to users(user_id) — because
    # invitations are consumed by AGENTS during `/v1/onboard`, and
    # agents live in `agents(agent_id)`, not `users(user_id)`. A FK
    # to users would (and historically did) reject every legitimate
    # agent onboard with `ForeignKeyViolationError on
    # invitations_used_by_fkey` (upstream-bug #11, surfaced by the
    # examples test drive). The column is audit-only — operators
    # see "which identifier consumed this invitation" without the
    # row needing to be referentially valid as a user.
    op.execute("""
        CREATE TABLE invitations (
            token_hash       text PRIMARY KEY,
            level            text NOT NULL CHECK (level ~ '^(admin|service|tier[0-9]+)$'),
            expires_at       timestamptz NOT NULL,
            used_at          timestamptz,
            used_by          text,
            created_by       text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
            created_at       timestamptz NOT NULL DEFAULT now(),
            idempotency_key  text,
            provisions_service_user boolean NOT NULL DEFAULT false
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX invitations_created_by_idempotency_key_uniq
            ON invitations (created_by, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)

    # ------------------------------------------------------------------
    # auth_refresh_tokens
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE auth_refresh_tokens (
            token_hash    text PRIMARY KEY,
            user_id       text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
            issued_at     timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL,
            used_at       timestamptz,
            replaced_by   text
        )
    """)
    op.execute("CREATE INDEX auth_refresh_user_idx ON auth_refresh_tokens(user_id)")

    # ------------------------------------------------------------------
    # password_reset_tokens (folded from 0004)
    # ------------------------------------------------------------------
    # Separate single-use, time-limited token type for "forgot
    # password" — distinct TTL / authz / audit story from refresh
    # tokens. `created_by` records the minting principal; nullable
    # so a defensive user-delete needn't scrub reset history first.
    op.execute("""
        CREATE TABLE password_reset_tokens (
            token_hash   text PRIMARY KEY,
            user_id      text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            issued_at    timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL,
            used_at      timestamptz,
            created_by   text REFERENCES users(user_id) ON UPDATE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX password_reset_tokens_user_idx "
        "ON password_reset_tokens (user_id)"
    )

    # ------------------------------------------------------------------
    # llm_presets
    # ------------------------------------------------------------------
    # Bundled (provider, model, sampling defaults, tier gate)
    # configurations agents reference by name instead of repeating
    # provider config on every call.
    op.execute("""
        CREATE TABLE llm_presets (
            name                       text PRIMARY KEY
                CHECK (name ~ '^[a-z][a-z0-9_-]{0,63}$'),
            description                text,
            provider                   text NOT NULL
                CHECK (provider IN (
                    'gemini', 'anthropic', 'openai', 'openai-embeddings',
                    'openai-compatible', 'openai-compatible-embeddings'
                )),
            concrete_model             text NOT NULL,
            api_key_ref                text NOT NULL,
            api_key                    text,
            base_url                   text,
            -- min_user_level mirrors the ACL `user_level` grammar:
            -- '*' (any), 'admin', 'service', or 'tierN'. Caller must
            -- satisfy this gate (see acl._user_level_satisfies).
            min_user_level             text NOT NULL DEFAULT '*'
                CHECK (min_user_level ~ '^(\\*|admin|service|tier[0-9]+)$'),
            default_temperature        double precision
                CHECK (default_temperature IS NULL
                       OR (default_temperature >= 0
                           AND default_temperature <= 2)),
            default_max_tokens         integer
                CHECK (default_max_tokens IS NULL OR default_max_tokens > 0),
            default_provider_options   jsonb,
            fallback_preset            text
                REFERENCES llm_presets(name) ON DELETE SET NULL,
            max_retries                integer NOT NULL DEFAULT 0
                CHECK (max_retries >= 0 AND max_retries <= 10),
            created_at                 timestamptz NOT NULL DEFAULT now(),
            updated_at                 timestamptz NOT NULL DEFAULT now(),
            -- created_by may be NULL for default-seeded rows.
            created_by                 text REFERENCES users(user_id) ON UPDATE CASCADE,
            CONSTRAINT llm_presets_base_url_check CHECK (
                provider NOT IN ('openai-compatible',
                                 'openai-compatible-embeddings')
                OR (base_url IS NOT NULL AND base_url <> '')
            )
        )
    """)
    op.execute("CREATE INDEX llm_presets_provider_idx ON llm_presets(provider)")
    op.execute("CREATE INDEX llm_presets_min_user_level_idx ON llm_presets(min_user_level)")
    op.execute(
        "CREATE INDEX llm_presets_fallback_preset_idx "
        "ON llm_presets(fallback_preset)"
    )

    # ------------------------------------------------------------------
    # pending_user_registrations (folded from 0003)
    # ------------------------------------------------------------------
    # Queue for channel-side registration requests. Channel agents
    # submit on behalf of an unauthenticated chat; admin approves to
    # convert into a real user row. `submitted_by_service_user_id`
    # is the F8 hook — approve auto-grants that principal servicing
    # rights on the new user.
    op.execute("""
        CREATE TABLE pending_user_registrations (
            registration_id              uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
            channel                      text           NOT NULL,
            external_id                  text           NOT NULL,
            display_name                 text,
            requested_email              text,
            metadata                     jsonb          NOT NULL DEFAULT '{}'::jsonb,
            requested_at                 timestamptz    NOT NULL DEFAULT now(),
            attempts                     integer        NOT NULL DEFAULT 1,
            last_attempt_at              timestamptz    NOT NULL DEFAULT now(),
            submitted_by_service_user_id text           REFERENCES users(user_id)
                                                        ON UPDATE CASCADE ON DELETE SET NULL,
            CONSTRAINT pending_user_registrations_channel_check
                CHECK (channel ~ '^[a-z][a-z0-9_-]{0,31}$'),
            UNIQUE (channel, external_id)
        )
    """)
    op.execute(
        "CREATE INDEX pending_user_registrations_requested_at_idx "
        "ON pending_user_registrations (requested_at)"
    )

    # ------------------------------------------------------------------
    # registration_attempts (folded from 0003)
    # ------------------------------------------------------------------
    # Rolling-window log; one row per submit attempt. Durable
    # history for the per-(channel, external_id) rate-limit bucket
    # (the bucket itself lives in Redis / per-process). Grows
    # unbounded; operators should plan a periodic
    # `DELETE WHERE attempted_at < now() - interval '30 days'`.
    op.execute("""
        CREATE TABLE registration_attempts (
            id             bigserial      PRIMARY KEY,
            channel        text           NOT NULL,
            external_id    text           NOT NULL,
            attempted_at   timestamptz    NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX registration_attempts_window_idx "
        "ON registration_attempts (channel, external_id, attempted_at DESC)"
    )
    # Dedicated single-column index for the hourly GC delete
    # (`DELETE ... WHERE attempted_at < cutoff`). `window_idx` can't serve it
    # — `attempted_at` is that index's 3rd column, not a leftmost prefix — so
    # without this the GC is a full table scan every hour.
    op.execute(
        "CREATE INDEX registration_attempts_gc_idx "
        "ON registration_attempts (attempted_at)"
    )

    # ------------------------------------------------------------------
    # mcp_servers (folded from 0006)
    # ------------------------------------------------------------------
    # Admin-managed MCP bridge configurations. PK is `server_id`
    # (one row → N runtime agents, one per MCP tool). `auth_value_ref`
    # indirects through env/secret store — raw secrets never live
    # here. Inert until the bridge package reads from it.
    op.execute("""
        CREATE TABLE mcp_servers (
            server_id            text         PRIMARY KEY,
            description          text         NOT NULL DEFAULT '',
            url                  text         NOT NULL,
            transport            text         NOT NULL
                                              CHECK (transport IN ('sse', 'streamable_http')),
            auth_kind            text         NOT NULL DEFAULT 'none'
                                              CHECK (auth_kind IN ('none', 'bearer', 'header')),
            auth_value_ref       text,
            auth_header_name     text,
            groups               text[]       NOT NULL DEFAULT '{}',
            expose_to_llm        boolean      NOT NULL DEFAULT true,
            tools_cache          jsonb,
            refresh_requested_at timestamptz,
            created_at           timestamptz  NOT NULL DEFAULT now(),
            last_connected_at    timestamptz,
            created_by           text         REFERENCES users(user_id) ON UPDATE CASCADE,
            CONSTRAINT mcp_servers_server_id_check
                CHECK (server_id ~ '^[a-z][a-z0-9_]+$'),
            CONSTRAINT mcp_servers_auth_consistent CHECK (
                -- auth_value_ref required when auth_kind != 'none'
                (auth_kind = 'none' AND auth_value_ref IS NULL
                                    AND auth_header_name IS NULL)
                OR (auth_kind = 'bearer' AND auth_value_ref IS NOT NULL
                                         AND auth_header_name IS NULL)
                OR (auth_kind = 'header' AND auth_value_ref IS NOT NULL
                                         AND auth_header_name IS NOT NULL)
            )
        )
    """)
    op.execute("CREATE INDEX mcp_servers_groups_idx ON mcp_servers USING gin (groups)")


def downgrade() -> None:
    for table in (
        "mcp_servers",
        "registration_attempts",
        "pending_user_registrations",
        "password_reset_tokens",
        "llm_presets",
        "auth_refresh_tokens",
        "invitations",
        "audit_log",
        "acl_rules",
        "file_names",
        "files",
        "task_events",
        "tasks",
        "agents",
        "sessions",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
