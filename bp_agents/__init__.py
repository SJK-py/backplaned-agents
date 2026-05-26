"""bp_agents — the first-party agent suite built on the Backplaned
router/SDK.

See `docs/agent-suite/` for the design and `docs/agent-suite/build-plan.md`
for the phased build. This package layers conversation, orchestration,
memory, knowledge, scheduling, and channels on top of the platform
(`bp_protocol` / `bp_sdk` / `bp_router`).

The suite keeps its OWN Postgres (sessions / config / cron) and
per-user LanceDB (knowledge / memory), joined to the platform only by
`user_id` / `session_id`. The DB layer lives in `bp_agents.db`.
"""
