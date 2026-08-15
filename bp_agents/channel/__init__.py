"""bp_agents.channel — transport-agnostic channel core.

The logic every channel shares ([channel.md], [delegation.md],
[sessions.md]) — per-session routing, the router's FIFO turn lease,
`delegated_to` maintenance, user-driven `/delegate`·`/undelegate`, and the
fire-and-forget `memory.add` — with **no transport coupling**. A frontend
(the Telegram `ChatbotGateway`, the webapp) supplies identity + send/receive
and orchestrates a turn around these primitives so delegation and ordering
have a single source of truth.

Conversation itself lives in the router's session store, which the channel
sees through `store.SessionStore`: it may read any thread and drive session
state, and it may not write history. The two hosts hold the user's authority
differently — the chatbot mints per-user tokens as a service principal, the
webapp already has the logged-in user's — so the store is a seam.
"""

from bp_agents.channel.core import (
    DELEGATED_TO,
    MEMORY_AGENT_ID,
    ORCHESTRATOR_AGENT_ID,
    RECAP_ITEM,
    RETIRE_ITEM,
    SEED_ITEM,
    ChannelCore,
    SessionBusy,
    pretty_agent,
)
from bp_agents.channel.render import (
    UNTAGGED_AGENTS,
    VERBOSE_PREFIX,
    agent_tag,
    progress_producer,
    render_progress_line,
)
from bp_agents.channel.store import (
    HttpSessionStore,
    SessionStore,
    StoreError,
    TokenRegistry,
)

__all__ = [
    "DELEGATED_TO",
    "MEMORY_AGENT_ID",
    "ORCHESTRATOR_AGENT_ID",
    "RECAP_ITEM",
    "RETIRE_ITEM",
    "SEED_ITEM",
    "UNTAGGED_AGENTS",
    "VERBOSE_PREFIX",
    "ChannelCore",
    "HttpSessionStore",
    "SessionBusy",
    "SessionStore",
    "StoreError",
    "TokenRegistry",
    "agent_tag",
    "pretty_agent",
    "progress_producer",
    "render_progress_line",
]
