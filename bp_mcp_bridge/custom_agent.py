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
from bp_sdk import Agent, InputValidationError, Message, TaskContext
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

# Cap on the text inlined from a `file_ref` parameter. A reference to a
# huge (or accidentally binary) file would otherwise stuff the whole body
# into the prompt; reject past this with a clear caller-facing error.
_FILE_REF_MAX_BYTES = 1_000_000

# The hint appended to a `file_ref` param's tool-schema description so the
# calling model knows to pass a file NAME, not the literal content.
_FILE_REF_HINT = (
    "Provide a file reference (a name in the file store); its UTF-8 text "
    "content is read in and substituted."
)


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
    list. A `file_ref` param stays a `string` (the caller passes a file
    name) but gains a description hint so the model passes a reference,
    not the content. Object schema with `additionalProperties: False` so
    the router rejects anything the operator didn't declare."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in parameters:
        name = p["name"]
        desc = p.get("description") or ""
        if p.get("file_ref"):
            desc = f"{desc} {_FILE_REF_HINT}".strip() if desc else _FILE_REF_HINT
        prop: dict[str, Any] = {"type": "string"}
        if desc:
            prop["description"] = desc
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


async def _read_text_ref(ctx: TaskContext, name: str, param_name: str) -> str:
    """Read a `file_ref` parameter's referenced file as UTF-8 text.

    Raises `InputValidationError` (400 to the caller) when the reference
    is empty, the file is missing, too large, or not valid UTF-8 text —
    these are all "the caller gave a bad file ref", not server faults.
    `asyncio.CancelledError` is a BaseException and propagates."""
    if not name:
        raise InputValidationError(
            f"file_ref parameter {param_name!r} was given an empty reference"
        )
    try:
        stat = await ctx.files.stat(name)
        if stat.byte_size is not None and stat.byte_size > _FILE_REF_MAX_BYTES:
            raise InputValidationError(
                f"file_ref parameter {param_name!r}: file {name!r} is "
                f"{stat.byte_size} bytes, over the {_FILE_REF_MAX_BYTES}-byte "
                "text limit"
            )
        data = await ctx.files.read_bytes(name)
    except InputValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InputValidationError(
            f"file_ref parameter {param_name!r}: cannot read file "
            f"{name!r} ({exc})"
        ) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputValidationError(
            f"file_ref parameter {param_name!r}: file {name!r} is not "
            "UTF-8 text"
        ) from exc


async def _resolve_values(
    ctx: TaskContext,
    parameters: list[dict[str, Any]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Map the validated payload to substitution values: `file_ref`
    params are dereferenced to their file's text content; the rest pass
    through verbatim. Params declared but absent from the payload (an
    optional the caller omitted) are left out, so `$name` survives as a
    literal — matching the no-file-ref behaviour."""
    file_ref_names = {p["name"] for p in parameters if p.get("file_ref")}
    resolved: dict[str, Any] = {}
    for name, raw in payload.items():
        if name in file_ref_names:
            resolved[name] = await _read_text_ref(ctx, str(raw), name)
        else:
            resolved[name] = raw
    return resolved


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
        values = await _resolve_values(ctx, spec.parameters, payload)
        sys_text = _render(spec.system_prompt, values)
        user_text = _render(spec.user_prompt, values)
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
