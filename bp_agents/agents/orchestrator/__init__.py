"""orchestrator (group l0, hidden) — the personal assistant + session hub.

Runs the main agent loop and (Phase 3+) delegates to l1 specialists.
Reached only by handcrafted paths (channel dispatch, l1 hand-back) —
never as an LLM tool — so it's `hidden=true`.

Phase 1 ships the `message` mode only; `cron_message`, `subagent`, and
`end_delegation` land in later phases.
"""

from bp_agents.agents.orchestrator.agent import (
    ORCHESTRATOR_AGENT_ID,
    agent,
    run_orchestrator_message,
)

__all__ = ["ORCHESTRATOR_AGENT_ID", "agent", "run_orchestrator_message"]
