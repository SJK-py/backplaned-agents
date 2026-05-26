"""bp_agents.common.payloads — shared inter-agent payload models.

`message` / `cron_message` modes take a bare `{prompt}` — the agent
builds its own system prompt from config + history ([agents.md]). The
channel dispatches it; the orchestrator receives it.
"""

from __future__ import annotations

from pydantic import BaseModel


class MessagePayload(BaseModel):
    """Bare user input for the orchestrator's `message` mode (and the
    delegated `delegated_message` mode). The agent reconstructs context
    from session history; this carries only the turn's text."""

    prompt: str
