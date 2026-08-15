"""initial schema (consolidated — pre-release baseline)

The router's ENTIRE schema, as one migration. A fresh deployment runs
this file and nothing else and lands on the final shape.

Tables: users, sessions, agents, tasks, task_events, files, file_names,
acl_rules, audit_log, invitations, auth_refresh_tokens,
password_reset_tokens, llm_presets, pending_user_registrations,
registration_attempts, user_oidc_identities, mcp_servers, custom_agents,
code_agents, the five session-store tables (session_messages,
session_threads, session_state, session_handovers, session_turn_queue)
and user_llm_preferences.

**This baseline is not upgradable-to.** It was consolidated a second
time, absorbing what had been migrations 0002–0013, so a database
created by the previous chain has an `alembic_version` naming a
revision that no longer exists — `alembic upgrade head` against it
fails rather than doing anything subtle. That is deliberate: the
codebase is pre-release, no deployment carries data worth a rewrite
path, and a fabricated "upgrade" from an unknown intermediate is a
worse promise than a clear stop. **Existing installations must be
recreated from empty.** Post-release schema changes get fresh sequence
numbers (0002+) chaining linearly off this file.

What the fold changed, versus replaying the old chain (the schema is
identical; only the route to it differs):

  * Columns added by a later ALTER are declared inline in their
    table's CREATE — `users.purged_at`, `mcp_servers`'
    invitation/capability/stdio columns,
    `pending_user_registrations.requested_password_hash`,
    `invitations.agent_ids`/`consumed`, and the four
    `custom_agents` agent-loop columns.
  * `mcp_servers.url` is nullable from the start and the transport
    CHECK carries `stdio` from the start, rather than being widened
    later. The `mcp_servers_transport_fields` CHECK that keeps the
    URL and stdio shapes disjoint is declared with the table.
  * `tasks.caller_agent_id` / `active_agent_id` are NOT NULL + FK
    inline. The historical migration added them nullable, backfilled,
    then enforced — a dance only a populated table needs.
  * The `audit_log(actor_id, ts DESC)` partial index is a plain
    CREATE INDEX. The historical migration used CONCURRENTLY (and an
    autocommit block) purely to avoid an AccessExclusiveLock on a
    populated table; on an empty schema that buys nothing and costs
    the single-transaction property a baseline should have.
  * Only final constraint shapes appear. The pre-Phase-10 strict
    `acl_rules` pattern regex, for instance, is not reproduced — a
    consolidated baseline has no history to be faithful to.
  * Every FK to `users(user_id)` / `agents(agent_id)` is declared
    `ON UPDATE CASCADE` inline, so a service-principal rename on
    eviction propagates. Delete behaviour is per-table and unchanged.

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
    # `serviced_by`: list of service-principal user_ids authorised to
    # mint credentials for this user. Default-deny — an empty array
    # means no principal can mint.
    # `deleted_at` is the terminal admin soft-delete (distinct from the
    # reversible `suspended_at`); the row stays so the many
    # `REFERENCES users(user_id)` FKs — audit_log attribution in
    # particular — survive the delete.
    # `purged_at` goes further: a permanent `purge_user` hard-deletes
    # the user's content (sessions/tasks/files), scrubs PII (`email` /
    # `auth_secret_hash` → NULL) and stamps this column. The row is kept
    # as a tombstone (the FKs above, plus an append-only audit chain that
    # must stay intact), so `purged_at` is the durable signal the suite's
    # reconcile loop keys off to erase its own per-user rows and LanceDB.
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
            deleted_at         timestamptz,
            purged_at          timestamptz
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

    # Bootstrap rules — see docs/backplaned/acl.md §13. Three rows in evaluation
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
            provisions_service_user boolean NOT NULL DEFAULT false,
            -- Optional agent-name ROSTER on a single token (see
            -- docs/design/deployment-agent-host.md §3). Without it every
            -- invitation is an unbound bearer credential: `POST /v1/onboard`
            -- takes the name from the agent's own `agent_info`, so any token
            -- can onboard as any name. `agent_ids` fixes the names one token
            -- may produce and `consumed` records which have been taken, so a
            -- host process can onboard the whole group it runs and a
            -- partially-provisioned group heals on restart. NULL `agent_ids`
            -- keeps the unbound single-use behaviour exactly.
            agent_ids        text[],
            consumed         text[] NOT NULL DEFAULT '{}'
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
            -- TRUE for presets owned by the JSONC catalogue: re-synced
            -- (upserted) from the catalogue on every boot and pruned when
            -- dropped from it, so edits to them via the admin UI are
            -- transient. FALSE for admin-created presets, which the boot
            -- sync never touches. created_by is NULL exactly when managed.
            managed                    boolean NOT NULL DEFAULT false,
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
    # pending_user_registrations
    # ------------------------------------------------------------------
    # Queue for channel-side registration requests. Channel agents
    # submit on behalf of an unauthenticated chat; admin approves to
    # convert into a real user row. `submitted_by_service_user_id`
    # is the F8 hook — approve auto-grants that principal servicing
    # rights on the new user.
    #
    # `requested_password_hash` serves the self-service WEB signup path
    # only. A chat-channel registration is submitted by a service
    # principal that controls the chat, so the password is set later
    # out-of-band (the bot mints a reset token via `serviced_by`); those
    # rows leave this NULL and keep the random-initial-password approval
    # behaviour. The webapp has no such principal and no delivery
    # channel, so the user chooses a password on the public form and its
    # argon2 hash rides here until an admin approves.
    op.execute("""
        CREATE TABLE pending_user_registrations (
            registration_id              uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
            channel                      text           NOT NULL,
            external_id                  text           NOT NULL,
            display_name                 text,
            requested_email              text,
            requested_password_hash      text,
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
    # user_oidc_identities — external OIDC subjects linked to a user
    # ------------------------------------------------------------------
    # SSO login resolves a validated `(issuer, sub)` to a local
    # `user_id`. A child table rather than columns on `users`, so one
    # account can carry a password AND any number of linked OPs — and so
    # the OIDC subject (PII) is scrubbed on purge by deleting rows.
    #
    # `PRIMARY KEY (issuer, sub)` enforces "one OP identity ↔ exactly one
    # account" (`sub` is only unique per issuer); the `user_id` index
    # powers the reverse "list / unlink my logins" lookup. Structurally
    # the same as the suite's `(platform, external_id) → user_id` map.
    op.execute("""
        CREATE TABLE user_oidc_identities (
            issuer        text NOT NULL,
            sub           text NOT NULL,
            user_id       text NOT NULL
                          REFERENCES users(user_id)
                          ON UPDATE CASCADE ON DELETE CASCADE,
            email_at_link text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz,
            PRIMARY KEY (issuer, sub)
        )
    """)
    op.execute(
        "CREATE INDEX user_oidc_identities_user_id_idx "
        "ON user_oidc_identities (user_id)"
    )

    # ------------------------------------------------------------------
    # mcp_servers
    # ------------------------------------------------------------------
    # Admin-managed MCP bridge configurations. PK is `server_id`
    # (one row → N runtime agents, one per MCP tool). `auth_value_ref`
    # indirects through env/secret store — raw secrets never live here.
    #
    # Three transports in two disjoint shapes, kept apart by
    # `mcp_servers_transport_fields`: `stdio` spawns a local subprocess
    # (`command` + `args`, no URL) and speaks MCP over its pipes;
    # `sse` / `streamable_http` connect to a `url` and have no command.
    # `url` is therefore nullable. `env_refs` is a JSON map
    # `{ENV_NAME: "env://VAR" | "secret://..."}` the bridge resolves from
    # its own environment — never raw secrets in the table. The launcher
    # allowlist (uvx/…) is enforced app-side, not here.
    #
    # `pending_invitation_token` is how a bridge onboards without admin
    # rights: an admin action (create / reconnect) mints a short-TTL
    # service invitation onto the row, the bridge consumes it on its next
    # poll to onboard the `mcp_<server>` agent, and it is cleared once
    # that agent connects. `capabilities` merges into the agent's
    # auto-derived `mcp.bridge` / `mcp.tool.<tool>` set for ACL
    # targeting; `disabled_tools` names tools the bridge must not expose
    # as modes, while the full list is still reported for the UI.
    op.execute("""
        CREATE TABLE mcp_servers (
            server_id            text         PRIMARY KEY,
            description          text         NOT NULL DEFAULT '',
            url                  text,
            transport            text         NOT NULL,
            auth_kind            text         NOT NULL DEFAULT 'none'
                                              CHECK (auth_kind IN ('none', 'bearer', 'header')),
            auth_value_ref       text,
            auth_header_name     text,
            command              text,
            args                 text[]       NOT NULL DEFAULT '{}',
            env_refs             jsonb        NOT NULL DEFAULT '{}',
            groups               text[]       NOT NULL DEFAULT '{}',
            capabilities         text[]       NOT NULL DEFAULT '{}',
            disabled_tools       text[]       NOT NULL DEFAULT '{}',
            expose_to_llm        boolean      NOT NULL DEFAULT true,
            tools_cache          jsonb,
            refresh_requested_at timestamptz,
            created_at           timestamptz  NOT NULL DEFAULT now(),
            last_connected_at    timestamptz,
            created_by           text         REFERENCES users(user_id) ON UPDATE CASCADE,
            pending_invitation_token      text,
            pending_invitation_expires_at timestamptz,
            CONSTRAINT mcp_servers_server_id_check
                CHECK (server_id ~ '^[a-z][a-z0-9_]+$'),
            CONSTRAINT mcp_servers_transport_check
                CHECK (transport IN ('sse', 'streamable_http', 'stdio')),
            CONSTRAINT mcp_servers_transport_fields CHECK (
                (transport = 'stdio' AND command IS NOT NULL AND url IS NULL)
                OR (transport IN ('sse', 'streamable_http')
                    AND url IS NOT NULL AND command IS NULL)
            ),
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

    # ------------------------------------------------------------------
    # custom_agents — operator-defined LLM-backed agents
    # ------------------------------------------------------------------
    # The second bridge-provisioned kind. An operator authors a system
    # prompt, a user-prompt template, a list of parameters and a model
    # preset in the admin UI; the bridge stands up one single-mode
    # backplane `Agent` (`custom_<slug>`) whose handler runs an LLM
    # completion instead of forwarding to an MCP `tools/call`.
    #
    #   * `preset_name` FKs `llm_presets.name` — a referenced preset
    #     cannot be dropped.
    #   * `parameters` is a JSON list of `{name, description, required}`.
    #     Every param is type "string": the names are both the mode's
    #     `accepts_schema` keys and the `$name` substitution keys in the
    #     prompts, and non-string values have no safe `$`-templating.
    #   * The agent-loop columns turn one completion into a bounded
    #     tool-use loop. All default off, so a row that never touches
    #     them behaves as a single completion.
    #   * Provisioning mirrors `mcp_servers` exactly.
    #
    # See `docs/design/mcp-bridge-custom-llm-agents.md`.
    op.execute("""
        CREATE TABLE custom_agents (
            agent_id      text PRIMARY KEY
                          CHECK (agent_id ~ '^custom_[a-z][a-z0-9_]*$'),
            description   text NOT NULL DEFAULT '',
            preset_name   text NOT NULL REFERENCES llm_presets(name),
            system_prompt text NOT NULL DEFAULT '',
            user_prompt   text NOT NULL DEFAULT '',
            parameters    jsonb NOT NULL DEFAULT '[]'::jsonb,
            groups        jsonb NOT NULL DEFAULT '[]'::jsonb,
            capabilities  jsonb NOT NULL DEFAULT '[]'::jsonb,
            expose_to_llm boolean NOT NULL DEFAULT true,
            output_as_file boolean NOT NULL DEFAULT false,
            enabled       boolean NOT NULL DEFAULT true,
            agent_loop_enabled boolean NOT NULL DEFAULT false,
            max_rounds    integer NOT NULL DEFAULT 4
                          CHECK (max_rounds BETWEEN 1 AND 16),
            file_access   text NOT NULL DEFAULT 'none'
                          CHECK (file_access IN ('none', 'read_only', 'full')),
            peer_tools_enabled boolean NOT NULL DEFAULT false,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            created_by    text REFERENCES users(user_id),
            pending_invitation_token      text,
            pending_invitation_expires_at timestamptz
        )
    """)

    # ------------------------------------------------------------------
    # code_agents — operator-authored Python functions
    # ------------------------------------------------------------------
    # The third bridge-provisioned kind. An operator authors a Python
    # function in the admin UI; the bridge stands up one single-mode
    # `Agent` (`code_<slug>`) whose handler runs it in a uid-dropped
    # subprocess.
    #
    # A separate table rather than a `kind` column on `custom_agents`:
    # `custom_agents.preset_name` is `NOT NULL REFERENCES
    # llm_presets(name)`, and a discriminator would force it nullable —
    # dropping a real constraint on every existing LLM row for a kind
    # that will never pick a preset. What the two kinds share is CODE
    # (`bp_mcp_bridge/agent_common.py`), not schema.
    #
    #   * `parameters` entries carry a real JSON-Schema `type`. This
    #     handler receives a dict, so the string-only rule above — which
    #     exists for `$`-templating safety — does not transfer.
    #   * `secret_refs` holds `env://VAR` REFERENCES, never literals, the
    #     same posture as `mcp_servers.auth_value_ref`.
    #   * `timeout_s` / `memory_mb` bound one call inside the bridge; the
    #     router's task deadline is the outer bound.
    #   * There is deliberately NO `network` column. Per-agent egress
    #     control needs CAP_NET_ADMIN, which the bridge does not have and
    #     should not get, so the column would read as a guarantee it
    #     cannot make. See `docs/design/bridge-python-code-agents.md`
    #     §3.4.
    op.execute("""
        CREATE TABLE code_agents (
            agent_id      text PRIMARY KEY
                          CHECK (agent_id ~ '^code_[a-z][a-z0-9_]*$'),
            description   text NOT NULL DEFAULT '',
            code          text NOT NULL DEFAULT '',
            entrypoint    text NOT NULL DEFAULT 'run'
                          CHECK (entrypoint ~ '^[a-z_][a-z0-9_]*$'),
            parameters    jsonb NOT NULL DEFAULT '[]'::jsonb,
            returns       jsonb,
            secret_refs   jsonb NOT NULL DEFAULT '{}'::jsonb,
            timeout_s     integer NOT NULL DEFAULT 30
                          CHECK (timeout_s BETWEEN 1 AND 300),
            memory_mb     integer NOT NULL DEFAULT 512
                          CHECK (memory_mb BETWEEN 64 AND 4096),
            groups        jsonb NOT NULL DEFAULT '[]'::jsonb,
            capabilities  jsonb NOT NULL DEFAULT '[]'::jsonb,
            expose_to_llm boolean NOT NULL DEFAULT true,
            output_as_file boolean NOT NULL DEFAULT false,
            enabled       boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            created_by    text REFERENCES users(user_id),
            pending_invitation_token      text,
            pending_invitation_expires_at timestamptz
        )
    """)

    _create_session_store()

    # ------------------------------------------------------------------
    # user_llm_preferences — the user's per-slot model choice
    # ------------------------------------------------------------------
    # A **slot** is an opaque key ("balanced", "pro", …) naming a
    # preference the router resolves to a preset. This table holds the
    # user's half of that decision; the operator's half is
    # `Settings.llm_default_presets`, and the ceiling is each preset's
    # `min_user_level`. See `docs/design/router-resolved-preset-slots.md`
    # §4.
    #
    # Why its own table rather than the session store's user-scoped state
    # below, which already provides an opaque `(user_id, key)` namespace:
    # that namespace is writable by any agent acting in the user's
    # session, and the router *acts* on this value — it selects a model,
    # at a cost, under a tier gate. A value the router enforces policy on
    # must not be one any agent can overwrite. Writes come only from the
    # session-JWT endpoints (`/v1/llm/preferences`).
    #
    # `preset_embedding` is deliberately NOT a slot: changing an
    # embedding model invalidates every vector already written, silently,
    # with no migration path (design §12).
    op.execute("""
        CREATE TABLE user_llm_preferences (
            user_id     text NOT NULL
                REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
            slot        text NOT NULL,
            preset_name text NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, slot)
        )
    """)


def _create_session_store() -> None:
    """The five router-managed session-store tables.

    Adds the conversation log + session state service specified in
    `docs/design/router-managed-session-store.md`. All scoped by
    `(user_id, session_id)` with `ON DELETE CASCADE` on the session, so purge
    and the closed-session retention sweep reap conversation data with no
    cross-database reconcile (§10.1).

    Shape notes that are load-bearing, not incidental:

      * `session_messages.owner_agent_id` is the THREAD key and is stamped by
        the router from the task's active executor — never from the wire.
        There is deliberately no `author_agent_id`: with the owner derived,
        the author IS the owner (§4).
      * `role` carries NO check constraint. The router never interprets a
        role; every read names the roles it wants (§3.3).
      * There is no `incumbent` flag. Retirement is
        `session_threads.floor_id`, a monotonic cursor covering both prefix
        folding and whole-thread retirement (§3.4).
      * `session_id` is nullable: NULL rows are the `user` scope —
        cross-session agent context, the conversational analogue of the file
        store's `persist/` (§3.1). They survive session purge and are reaped
        by `purge_user`.
      * `session_turn_queue` holds at most one `active` row per session,
        enforced by a partial unique index rather than by application logic
        (§6.4).
    """
    op.execute("""
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
    """)
    # Read path: a range scan over one thread's partition. `roles` /
    # `redacted_at` / `hidden` are filters applied to the scanned rows.
    op.execute("""
        CREATE INDEX ix_session_messages_thread
            ON session_messages (session_id, owner_agent_id, thread_key, id)
    """)
    # `user`-scope read path (session_id IS NULL).
    op.execute("""
        CREATE INDEX ix_session_messages_user_thread
            ON session_messages (user_id, owner_agent_id, thread_key, id)
            WHERE session_id IS NULL
    """)
    # Idempotent append. Keyed on the CALLER'S key, not on (task_id, role):
    # one task legitimately appends several rows of the same role (a
    # tool_call and its tool_result), which a role-keyed constraint would
    # reject.
    op.execute("""
        CREATE UNIQUE INDEX uq_session_messages_idempotency
            ON session_messages (task_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)

    op.execute("""
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
    """)
    # Session and user scopes need separate uniqueness: NULL session_id
    # never equals itself, so one index cannot cover both.
    op.execute("""
        CREATE UNIQUE INDEX uq_session_threads_session
            ON session_threads (session_id, owner_agent_id, thread_key)
            WHERE session_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_session_threads_user
            ON session_threads (user_id, owner_agent_id, thread_key)
            WHERE session_id IS NULL
    """)
    # Per-user quota: SUM(content_bytes) over a user's threads.
    op.execute(
        "CREATE INDEX ix_session_threads_user ON session_threads (user_id)"
    )

    op.execute("""
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
    """)
    # Thread state (owner set) and session state (owner NULL) are distinct
    # namespaces; NULL-safe uniqueness needs the partial-index pair again.
    op.execute("""
        CREATE UNIQUE INDEX uq_session_state_thread
            ON session_state (session_id, owner_agent_id, thread_key, key)
            WHERE owner_agent_id IS NOT NULL AND session_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_session_state_session
            ON session_state (session_id, key)
            WHERE owner_agent_id IS NULL AND session_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_session_state_user_thread
            ON session_state (user_id, owner_agent_id, thread_key, key)
            WHERE owner_agent_id IS NOT NULL AND session_id IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_session_state_user
            ON session_state (user_id, key)
            WHERE owner_agent_id IS NULL AND session_id IS NULL
    """)

    op.execute("""
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
    """)
    # Partial, so a drain never scans consumed rows.
    op.execute("""
        CREATE INDEX ix_session_handovers_pending
            ON session_handovers (session_id, target_agent_id, thread_key, id)
            WHERE consumed_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_session_handovers_pending_user
            ON session_handovers (user_id, target_agent_id, thread_key, id)
            WHERE consumed_at IS NULL AND session_id IS NULL
    """)

    op.execute("""
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
    """)
    # At most one holder per session — a database guarantee, not a code path.
    op.execute("""
        CREATE UNIQUE INDEX uq_session_turn_queue_active
            ON session_turn_queue (session_id)
            WHERE state = 'active'
    """)
    # One row per (session, holder): re-acquiring is idempotent.
    op.execute("""
        CREATE UNIQUE INDEX uq_session_turn_queue_holder
            ON session_turn_queue (session_id, holder_id)
    """)
    op.execute("""
        CREATE INDEX ix_session_turn_queue_waiting
            ON session_turn_queue (session_id, ticket)
            WHERE state = 'waiting'
    """)


def downgrade() -> None:
    for table in (
        "user_llm_preferences",
        "session_turn_queue",
        "session_handovers",
        "session_state",
        "session_threads",
        "session_messages",
        "code_agents",
        "custom_agents",
        "mcp_servers",
        "user_oidc_identities",
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
