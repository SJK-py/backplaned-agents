"""compose_subagent_system — the shared system-prompt builder for
stateless subagent calls.

A subagent's output returns to the CALLING agent, not the user, so the
composed prompt must (1) keep the agent's own role, (2) add the shared
"you're a subagent, output goes to the caller" framing, and (3) fold the
caller-supplied context + instruction in under explicit headers (skipped
when absent).
"""

from __future__ import annotations

from bp_agents.agents.l1_common import _SUBAGENT_ROLE, compose_subagent_system
from bp_protocol.types import LLMData


def test_role_framing_always_present_after_base() -> None:
    out = compose_subagent_system("You are a research specialist.", LLMData(prompt="go"))
    assert out == f"You are a research specialist.\n\n{_SUBAGENT_ROLE}"
    # The framing must make clear the output is not for the user.
    assert "NOT sent to the user" in out


def test_context_and_instruction_headed_and_ordered() -> None:
    out = compose_subagent_system(
        "Base role.",
        LLMData(
            prompt="ZZ_PROMPT_BODY_ZZ",
            context="some background",
            agent_instruction="do it tersely",
        ),
    )
    # Order: base, role framing, context, instruction.
    assert out == (
        "Base role.\n\n"
        f"{_SUBAGENT_ROLE}\n\n"
        "## Context from the calling agent\nsome background\n\n"
        "## Instruction from the calling agent\ndo it tersely"
    )
    # The prompt itself is NOT placed in the system text (it's the user turn).
    assert "ZZ_PROMPT_BODY_ZZ" not in out


def test_optionals_skipped_when_absent() -> None:
    only_ctx = compose_subagent_system("B", LLMData(prompt="p", context="c"))
    assert "## Context from the calling agent\nc" in only_ctx
    assert "## Instruction" not in only_ctx

    only_instr = compose_subagent_system("B", LLMData(prompt="p", agent_instruction="i"))
    assert "## Instruction from the calling agent\ni" in only_instr
    assert "## Context" not in only_instr
