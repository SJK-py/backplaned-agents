"""Entrypoint: `python -m bp_agents.agents.chatbot`."""

from bp_agents.agents.chatbot.agent import agent

if __name__ == "__main__":
    agent.run()
