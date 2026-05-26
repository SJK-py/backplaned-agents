"""config (group l2) — conversational user-config management.

`message` mode (tool-visible: the orchestrator's LLM calls `call_config`;
also reached via the channel's `/config`). An LLM loop with local tools
that read/set `user_config` fields. Not delegatable. See [agents.md].
"""

from bp_agents.agents.config.agent import CONFIG_AGENT_ID, agent, run_config

__all__ = ["CONFIG_AGENT_ID", "agent", "run_config"]
