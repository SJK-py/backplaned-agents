"""bp_agents.db — the suite's Postgres layer.

`connection.open_pool` builds the asyncpg pool; `models` holds the row
dataclasses; `queries` holds the async query functions. Schema is owned
by Alembic migrations under `bp_agents/migrations/` (the suite's own
Alembic config, `alembic_suite.ini`, separate from the router's).
"""
