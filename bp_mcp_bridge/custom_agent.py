"""Project one operator-defined custom-agent row onto one backplane `Agent`.

The bridge builds ONE single-mode backplane `Agent` per `custom_agents`
row. Unlike the MCP path (`tool_agent.py`), the mode handler does not
forward to an upstream — it runs an LLM completion via `ctx.llm.generate`
using the operator's system/user prompts (with the row's string
parameters substituted in) against the row's model preset.

The operator's parameter list becomes the single mode's `accepts_schema`
(an object of `string` properties); the calling LLM fills it, the router
admit-validates it, and the handler substitutes the values into the
prompt templates with `string.Template.safe_substitute` ($name).

Why `bp_sdk` primitives only (no `bp_agents.run_llm_loop`): the bridge
must stay free of the agent suite's dependency weight. v1 is a single
completion, which needs nothing beyond `ctx.llm` /
`ctx.files`. See `docs/design/mcp-bridge-custom-llm-agents.md`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, Message, TaskContext
from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)

# Single mode. Never appears in the external LLM tool name: a one-mode
# agent surfaces as `call_<agent_id>` (the mode label is dropped by the
# SDK's `_tool_specs`), so the model calls `call_custom_<id>`.
MODE = "main"

# Output filename when `output_as_file` is set. Markdown is the common
# shape for a generated artifact; the parent reads it on demand via the
# file ref rather than receiving the whole body inline.
_OUTPUT_FILENAME = "output.md"


@dataclass(frozen=True)
class CustomAgentSpec:
    """The fields `build_custom_agent` consumes — the runtime-relevant
    subset of a `custom_agents` row plus the bridge's connection config.
    Analogous to `BridgeConfig` for the MCP path."""

    agent_id: str  # full backplane id, custom_<slug>
    description: str
    preset_name: str
    system_prompt: str
    user_prompt: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    expose_to_llm: bool = True
    output_as_file: bool = False
    router_url: str = "ws://localhost:8000/v1/agent"
    state_dir: Path = field(default_factory=lambda: Path("/var/lib/bp_mcp_bridge"))


def _render(template: str, values: dict[str, Any]) -> str:
    """Substitute `$name` placeholders from `values`. `safe_substitute`
    leaves any stray `$` sequence untouched (never raises) and only
    substitutes declared keys — the router already validated that the
    payload carries the declared params, and the admin API rejected
    prompts referencing undeclared ones."""
    return Template(template).safe_substitute(values)


def _accepts_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """The single mode's parameter schema: every operator param is one
    `string` property; `required` params land in the schema's required
    list. Object schema with `additionalProperties: False` so the router
    rejects anything the operator didn't declare."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in parameters:
        name = p["name"]
        prop: dict[str, Any] = {"type": "string"}
        if p.get("description"):
            prop["description"] = p["description"]
        props[name] = prop
        if p.get("required", True):
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return {MODE: schema}


def make_custom_handler(spec: CustomAgentSpec):  # type: ignore[no-untyped-def]
    """Build the single-mode handler closure: render the prompts, run one
    LLM completion against the preset, return the text (or a file ref when
    `output_as_file` is set)."""

    async def handler(ctx: TaskContext, payload: dict) -> AgentOutput:
        ctx.log.info(
            "custom_agent_call",
            extra={
                "event": "custom_agent_call",
                "bp.custom_agent_id": spec.agent_id,
                "preset": spec.preset_name,
            },
        )
        sys_text = _render(spec.system_prompt, payload)
        user_text = _render(spec.user_prompt, payload)
        messages: list[Message] = []
        if sys_text.strip():
            messages.append(Message(role="system", content=sys_text))
        messages.append(Message(role="user", content=user_text))
        resp = await ctx.llm.generate(messages, preset=spec.preset_name)
        text = resp.text or ""
        if spec.output_as_file:
            saved = await ctx.files.write(_OUTPUT_FILENAME, text)
            return AgentOutput(
                content=f"Output written to file: {saved}",
                files=[saved],
            )
        return AgentOutput(content=text)

    return handler


def build_custom_agent(
    spec: CustomAgentSpec,
    invitation_token: str,
) -> Agent:
    """Construct the single backplane `Agent` for one custom-agent row.

    The agent has one mode (`MODE`); its handler runs an LLM completion.
    `accepts_schema` is operator-pinned from the row's parameter list.
    `invitation_token` is the admin-minted onboarding token; on resume
    from a persisted credentials file the SDK ignores it."""
    info = AgentInfo(
        agent_id=spec.agent_id,
        description=spec.description,
        groups=list(spec.groups),
        # `custom.agent` is the coarse marker (every custom agent has it),
        # paralleling `mcp.bridge`; operator caps append for ACL targeting.
        capabilities=["custom.agent", *spec.capabilities],
        accepts_schema=_accepts_schema(spec.parameters),
        produces_files=spec.output_as_file,
        hidden=not spec.expose_to_llm,
    )
    agent_config = AgentConfig(
        router_url=spec.router_url,
        state_dir=spec.state_dir / spec.agent_id,
        invitation_token=invitation_token,
    )
    agent = Agent(info=info, config=agent_config)
    agent.handler(mode=MODE)(make_custom_handler(spec))
    return agent
