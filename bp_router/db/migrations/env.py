"""Alembic env — async-aware migration runner for bp_router.

The DB URL is read from `ROUTER_DB_URL` so deployments don't have to
keep alembic.ini and the runtime config in sync.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from env so we don't keep secrets in alembic.ini
db_url = os.environ.get("ROUTER_DB_URL")
if db_url:
    # Alembic uses sqlalchemy URLs; coerce postgresql:// → postgresql+asyncpg://
    if db_url.startswith("postgresql://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgresql://"):]
    config.set_main_option("sqlalchemy.url", db_url)

# No SQLAlchemy MetaData in this project (we use raw SQL via asyncpg).
# Alembic still needs target_metadata; pass None and rely on op.execute().
target_metadata = None

# Project-fixed key for the migration advisory lock. The documented
# deploy model is multi-replica (k8s / gunicorn workers); a parallel
# first deploy or upgrade runs `alembic upgrade head` from several
# runners at once. Without serialisation they all see an empty
# `alembic_version`, race `CREATE TABLE` / `ALTER TABLE`, and
# corrupt the schema or wedge `alembic_version` — migrations
# 0007/0008 use `CREATE INDEX CONCURRENTLY` in an autocommit block,
# especially unsafe to double-run.
#
# `pg_advisory_lock` is SESSION-scoped (deliberately NOT
# `pg_advisory_xact_lock`): it must span the per-migration
# autocommit blocks the CONCURRENTLY migrations open, so an
# xact-level lock — released at the first COMMIT — would not
# protect them. Explicitly released in a `finally`; Postgres also
# auto-releases session advisory locks if the connection drops, so
# a crashed runner can't wedge it. The loser blocks until the
# winner finishes, then runs against an already-at-head
# `alembic_version` → a safe no-op.
#
# Value is b"bpmigr" big-endian (0x62706d696772 ≈ 1.08e14) — a
# stable, collision-unlikely, in-range bigint.
_MIGRATION_ADVISORY_LOCK_KEY = 0x62706D696772


def run_migrations_offline() -> None:
    """Generate SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    # Serialise concurrent runners (see _MIGRATION_ADVISORY_LOCK_KEY).
    # Acquire BEFORE configure/begin_transaction so the lock spans
    # the entire run including the CONCURRENTLY autocommit blocks;
    # release in `finally` so a failed migration frees it for a
    # retry/another pod. The key is our own int literal — no
    # injection surface.
    connection.exec_driver_sql(
        f"SELECT pg_advisory_lock({_MIGRATION_ADVISORY_LOCK_KEY})"
    )
    try:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.exec_driver_sql(
            f"SELECT pg_advisory_unlock({_MIGRATION_ADVISORY_LOCK_KEY})"
        )


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
