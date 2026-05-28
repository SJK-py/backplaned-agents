"""Entrypoint: `python -m bp_agents.agents.webapp`."""

from bp_agents.agents.webapp.agent import agent

if __name__ == "__main__":
    agent.run()
