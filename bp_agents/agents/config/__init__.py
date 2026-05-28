"""config (group l2) — conversational user self-service.

Two tool-visible modes (reachable only by the orchestrator + channel):
`message` (`call_config_message` / `/config`) reads-and-sets `user_config`
fields, and `cron` (`call_config_cron` / `/cron`) manages the user's
scheduled jobs. Each is an LLM loop with local tools. Not delegatable.
See [agents.md].
"""

from bp_agents.agents.config.agent import CONFIG_AGENT_ID, agent, run_config

__all__ = ["CONFIG_AGENT_ID", "agent", "run_config"]
