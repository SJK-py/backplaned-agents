"""research agent: prompts instruct deliberate (not reflexive) web search.

Guards the fix for the "research over-uses web search" regression — a
behavioral nudge in the system prompts, source-pinned so it isn't silently
dropped.
"""

from __future__ import annotations

from bp_agents.agents.research.agent import _DELEGATION_SYSTEM, _SUBAGENT_SYSTEM


def test_research_prompts_carry_search_discipline() -> None:
    for system in (_SUBAGENT_SYSTEM, _DELEGATION_SYSTEM):
        low = system.lower()
        # Don't search when the answer is already available.
        assert "without searching" in low
        # Bias toward few focused searches and stopping early.
        assert "search only" in low
        assert "stop as soon as you can answer" in low
