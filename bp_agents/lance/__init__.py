"""bp_agents.lance — per-user LanceDB stores (knowledge + memory).

One logical store per user, resolved from the authoritative `user_id`
(derived from the task, never asserted) — same isolation guarantee as
the router file store. The sync LanceDB client is wrapped in
`asyncio.to_thread` so store calls don't block the event loop. Schema
reference: [agent-suite/data-model.md] §2.
"""

from bp_agents.lance.base import connect, user_db_path

__all__ = ["connect", "user_db_path"]
