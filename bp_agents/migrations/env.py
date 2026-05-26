"""Alembic env — async migration runner for the agent suite's Postgres.

The DB URL is read from `SUITE_DATABASE_URL` so deployments don't keep
secrets in `alembic_suite.ini`. The suite uses raw SQL via asyncpg at
runtime, so there is no SQLAlchemy MetaData — `target_metadata = None`
and migrations use `op.execute()`.
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

db_url = os.environ.get("SUITE_DATABASE_URL")
if db_url:
    if db_url.startswith("postgresql://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgresql://") :]
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (`--sql`)."""
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
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        # Explicit commit: under SQLAlchemy 2.0 + asyncpg, an
        # `AsyncConnection` is commit-as-you-go and the `async with`
        # block ROLLS BACK on exit unless we commit. Alembic's
        # `begin_transaction()` runs on the sync facade and, with this
        # driver/version combo, does not surface a commit to the outer
        # async connection — so without this the migration "succeeds"
        # (exit 0) but no DDL lands. (The router's own env.py omits this
        # and exhibits the no-commit symptom on alembic 1.18 /
        # SQLAlchemy 2.0.50.)
        await connection.commit()
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
