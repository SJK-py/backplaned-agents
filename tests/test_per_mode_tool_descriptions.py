"""Per-mode tool descriptions: `@agent.handler(description=...)` publishes
`AgentInfo.mode_descriptions`, and `build_tools` uses it per `call_<agent>_
<mode>` tool (falling back to the agent-level description)."""

from __future__ import annotations

import importlib

from pydantic import BaseModel

from bp_protocol.types import AgentInfo
from bp_sdk import Agent
from bp_sdk.tools import build_tools


class _P(BaseModel):
    x: str


def _agent_with_modes() -> Agent:
    a = Agent(info=AgentInfo(
        agent_id="kb", description="Agent-level desc.",
        groups=["l3"], capabilities=["database.retrieval"],
    ))

    @a.handler(mode="retrieve", description="Search the knowledge base.")
    async def _retrieve(ctx, p: _P): ...

    @a.handler(mode="remove", description="Delete a document by id.")
    async def _remove(ctx, p: _P): ...

    @a.handler(mode="plain")  # no per-mode description
    async def _plain(ctx, p: _P): ...

    return a


def test_handler_description_publishes_mode_descriptions() -> None:
    a = _agent_with_modes()
    assert a.info.mode_descriptions == {
        "retrieve": "Search the knowledge base.",
        "remove": "Delete a document by id.",
    }


def test_no_descriptions_leaves_field_none() -> None:
    a = Agent(info=AgentInfo(agent_id="x", description="d", groups=[], capabilities=[]))

    @a.handler(mode="only")
    async def _only(ctx, p: _P): ...

    assert a.info.mode_descriptions is None


def _descs(a: Agent) -> dict[str, str]:
    entry = {a.info.agent_id: a.info.model_dump()}
    return {
        f["function"]["name"]: f["function"]["description"]
        for f in build_tools(entry, provider="openai")
    }


def test_build_tools_uses_per_mode_description_with_fallback() -> None:
    d = _descs(_agent_with_modes())
    # Per-mode descriptions win, verbatim — no trailing metadata.
    assert d["call_kb_retrieve"] == "Search the knowledge base."
    assert d["call_kb_remove"] == "Delete a document by id."
    # …a mode without one falls back to the agent-level description.
    assert d["call_kb_plain"] == "Agent-level desc."
    # Capabilities are NOT appended to the tool description.
    assert all("[capabilities:" not in v for v in d.values())


def test_single_mode_agent_keeps_agent_level_description() -> None:
    a = Agent(info=AgentInfo(
        agent_id="solo", description="Do the one thing.",
        groups=["l2"], capabilities=["user.config"],
    ))

    @a.handler(mode="message")  # single tool mode, no per-mode desc
    async def _m(ctx, p: _P): ...

    d = _descs(a)
    # Single tool-visible mode keeps the back-compat `call_<agent>` name.
    assert d["call_solo"].startswith("Do the one thing.")


def test_config_agent_exposes_settings_and_cron_tools() -> None:
    """The config agent's `cron` mode is tool-visible (config is reachable
    only by the orchestrator + channel), so the orchestrator can set
    reminders via `call_config_cron` alongside `call_config_message`."""
    cfg = importlib.import_module("bp_agents.agents.config.agent").agent
    d = _descs(cfg)
    assert set(d) == {"call_config_message", "call_config_cron"}
    assert "settings" in d["call_config_message"].lower()
    assert "remind" in d["call_config_cron"].lower() or "scheduled" in d["call_config_cron"].lower()
