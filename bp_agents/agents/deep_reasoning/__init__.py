"""deep_reasoning (group l1) — planning / multi-step reasoning.

Phase 3 ships the standard l1 modes (subagent / on_delegation /
delegated_message) via l1_common. The bespoke in-process `plan_mode`
(fresh-loop step planner with execute_step → orchestrator(subagent)) is
a later refinement — see [agents.md] and the deferred-work ledger.
"""

from bp_agents.agents.deep_reasoning.agent import DEEP_REASONING_AGENT_ID, agent

__all__ = ["DEEP_REASONING_AGENT_ID", "agent"]
