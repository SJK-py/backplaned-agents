"""memory (group l3, hidden via tool flags) — per-user fact graph.

`retrieve` (tool-visible) recalls facts by relevance + recency with 1-hop
graph expansion; `add` (tool=false, channel fire-and-forget) runs the
4-phase extract→reconcile→relate→propagate pipeline under a per-user
lock. See [memory.md], [data-model.md] §2.2.
"""

from bp_agents.agents.memory.agent import (
    MEMORY_AGENT_ID,
    MemAdd,
    MemRetrieve,
    agent,
    run_memory_add,
    run_memory_retrieve,
)

__all__ = [
    "MEMORY_AGENT_ID",
    "MemAdd",
    "MemRetrieve",
    "agent",
    "run_memory_add",
    "run_memory_retrieve",
]
