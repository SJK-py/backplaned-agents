"""knowledge_base (group l3) — per-user document store + retrieval (LanceDB).

All modes are tool-visible (research's LLM calls them). Phase 2 ships
`store` / `retrieve` / `list` / `remove`; `modify` and the LLM
metadata-generation refinement land later. See [agents.md], [data-model.md] §2.1.
"""

from bp_agents.agents.knowledge_base.agent import (
    KNOWLEDGE_BASE_AGENT_ID,
    agent,
    run_kb_list,
    run_kb_remove,
    run_kb_retrieve,
    run_kb_store,
)

__all__ = [
    "KNOWLEDGE_BASE_AGENT_ID",
    "agent",
    "run_kb_list",
    "run_kb_remove",
    "run_kb_retrieve",
    "run_kb_store",
]
