"""bp_agents.channel — transport-agnostic channel core.

The logic every channel shares ([channel.md], [delegation.md],
[sessions.md]) — per-session routing, the FIFO session lock, `delegated_to`
maintenance, user-driven `/delegate`·`/undelegate`, rolling summarization,
and the fire-and-forget `memory.add` — with **no transport coupling**. A
frontend (the Telegram `ChatbotGateway`, the future webapp) supplies
identity + send/receive and orchestrates a turn around these primitives so
delegation/summarization/locking have a single source of truth.
"""

from bp_agents.channel.core import (
    MEMORY_AGENT_ID,
    ORCHESTRATOR_AGENT_ID,
    ChannelCore,
    pretty_agent,
)

__all__ = [
    "MEMORY_AGENT_ID",
    "ORCHESTRATOR_AGENT_ID",
    "ChannelCore",
    "pretty_agent",
]
