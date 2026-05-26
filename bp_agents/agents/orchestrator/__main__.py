"""Entrypoint: `python -m bp_agents.agents.orchestrator`."""

from bp_agents.agents.orchestrator.agent import agent

if __name__ == "__main__":
    agent.run()
