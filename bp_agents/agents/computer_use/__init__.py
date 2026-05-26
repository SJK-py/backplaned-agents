"""computer_use (group l1) — coding / computer tasks via the sandbox.

Standard l1 modes (subagent / on_delegation / delegated_message) via
l1_common; it drives the sandbox through peer tools (`call_sandbox`,
allowed by the ACL `*/computer.* → infra/computer.*` rule). The SDK
file-tool bundle is a noted refinement. See [agents.md].
"""

from bp_agents.agents.computer_use.agent import COMPUTER_USE_AGENT_ID, agent

__all__ = ["COMPUTER_USE_AGENT_ID", "agent"]
