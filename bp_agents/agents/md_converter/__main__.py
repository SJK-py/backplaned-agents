"""Entrypoint: `python -m bp_agents.agents.md_converter`."""

from bp_agents.agents.md_converter.agent import agent

if __name__ == "__main__":
    agent.run()
