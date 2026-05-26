"""Entrypoint: `python -m bp_agents.agents.history_summarizer`."""

from bp_agents.agents.history_summarizer.agent import agent

if __name__ == "__main__":
    agent.run()
