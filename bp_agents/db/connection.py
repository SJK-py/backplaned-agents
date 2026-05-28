"""bp_agents.db.connection — the suite's Postgres pool lifecycle.

Mirrors `bp_router.db.connection.open_pool`: a per-connection
statement timeout plus json/jsonb codecs so dict ↔ jsonb is automatic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bp_agents.settings import SuiteSettings

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


async def open_pool(settings: SuiteSettings) -> asyncpg.Pool:
    """Create the suite's asyncpg connection pool."""
    import asyncpg  # noqa: PLC0415

    async def _init(conn: asyncpg.Connection) -> None:
        await conn.execute(
            f"SET statement_timeout = {settings.db_statement_timeout_ms}"
        )
        import json  # noqa: PLC0415

        for type_name in ("jsonb", "json"):
            await conn.set_type_codec(
                type_name,
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )

    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        init=_init,
    )
    logger.info(
        "suite_db_pool_opened",
        extra={
            "event": "suite_db_pool_opened",
            "min_size": settings.db_pool_min_size,
            "max_size": settings.db_pool_max_size,
        },
    )
    return pool


async def open_redis(settings: SuiteSettings):  # type: ignore[no-untyped-def]
    """Open the suite's Redis client, or return None when `redis_url` is
    unset (the in-process single-instance default). `decode_responses=True`
    so the session-lock Lua scripts compare plain strings."""
    if not settings.redis_url:
        return None
    import redis.asyncio as aredis  # noqa: PLC0415

    client = aredis.Redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("suite_redis_opened", extra={"event": "suite_redis_opened"})
    return client
