"""Project one operator-authored code-agent row onto one backplane `Agent`.

The bridge builds ONE single-mode backplane `Agent` per `code_agents` row.
Unlike the MCP path (`tool_agent.py`) it forwards to no upstream, and
unlike the custom LLM path (`custom_agent.py`) it calls no model: the mode
handler runs the operator's Python function in a hardened subprocess
(`code_runner.py`) and returns what it returned.

The operator's parameter list becomes the single mode's `accepts_schema`.
Unlike the LLM kind those parameters are TYPED — that kind pins everything
to `string` because its values are `$`-substituted into a prompt as inert
text, whereas this handler passes the payload to the function as a dict,
so a type is both meaningful and free (the router already validates the
payload against the schema at admit).

v1 gives the function NO backplane handles: no `ctx.llm`, no `ctx.files`,
no `ctx.peers`. It gets its parameters, an informational context dict, its
declared secrets, and the network. Everything else would need a child→parent
RPC channel, which is v2. See `docs/design/bridge-python-code-agents.md`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bp_mcp_bridge.agent_common import KIND_CODE, object_schema, observed_call
from bp_mcp_bridge.code_runner import CodeRunError, CodeRunSpec, run_code
from bp_mcp_bridge.config import StdioPolicy
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, InputValidationError, TaskContext
from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)

# Single mode. Never appears in the external LLM tool name: a one-mode agent
# surfaces as `call_<agent_id>`, so the model calls `call_code_<slug>`.
MODE = "main"

# Output filename when `output_as_file` is set.
_OUTPUT_FILENAME = "output.txt"


@dataclass(frozen=True)
class CodeAgentSpec:
    """The fields `build_code_agent` consumes — the runtime-relevant subset
    of a `code_agents` row plus the bridge's connection config."""

    agent_id: str  # full backplane id, code_<slug>
    description: str
    code: str
    entrypoint: str = "run"
    parameters: list[dict[str, Any]] = field(default_factory=list)
    returns: dict[str, Any] | None = None
    # Already-resolved secret VALUES (the bridge resolves `env://` refs before
    # constructing this). Never logged, never echoed into an error.
    secrets: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 30
    memory_mb: int = 512
    groups: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    expose_to_llm: bool = True
    output_as_file: bool = False
    policy: StdioPolicy = field(default_factory=StdioPolicy)
    router_url: str = "ws://localhost:8000/v1/agent"
    state_dir: Path = field(default_factory=lambda: Path("/var/lib/bp_mcp_bridge"))


def _accepts_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """The single mode's parameter schema, from the shared builder so this
    kind and the LLM kind cannot drift on `required` or on rejecting
    undeclared properties."""
    return {MODE: object_schema(parameters)}


def _run_spec(spec: CodeAgentSpec) -> CodeRunSpec:
    return CodeRunSpec(
        agent_id=spec.agent_id,
        code=spec.code,
        entrypoint=spec.entrypoint,
        timeout_s=spec.timeout_s,
        memory_mb=spec.memory_mb,
        secrets=spec.secrets,
        policy=spec.policy,
    )


def _as_text(value: Any) -> str:
    """The function's return value as `AgentOutput.content`.

    A `str` passes through verbatim — the common case, and JSON-quoting it
    would make every caller unwrap a string. Anything else is JSON, which is
    what `returns`/`produces_schema` describes when the operator declared
    one."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def make_code_handler(spec: CodeAgentSpec):  # type: ignore[no-untyped-def]
    """Build the single-mode handler closure: run the operator's function in
    a subprocess and return its value (or a file ref when `output_as_file`)."""

    async def handler(ctx: TaskContext, payload: dict) -> AgentOutput:
        ctx.log.info(
            "code_agent_call",
            extra={
                "event": "code_agent_call",
                "bp.code_agent_id": spec.agent_id,
                "entrypoint": spec.entrypoint,
            },
        )
        # Informational only in v1. The function cannot act on the backplane
        # with these; they are here so it can log, key a cache, or namespace
        # an outbound request per user.
        context = {
            "task_id": ctx.task_id,
            "user_id": ctx.user_id,
            "session_id": ctx.session_id,
            "agent_id": spec.agent_id,
        }
        async with observed_call(KIND_CODE, spec.agent_id):
            try:
                value = await run_code(_run_spec(spec), dict(payload), context)
            except CodeRunError as exc:
                if exc.kind == "internal":
                    # The BRIDGE is at fault (spawn refused, chown failed).
                    # Reporting it as a caller error would send the calling
                    # model off rewriting a payload that was fine.
                    ctx.log.error(
                        "code_agent_internal_error",
                        extra={
                            "event": "code_agent_internal_error",
                            "bp.code_agent_id": spec.agent_id,
                            "error": str(exc),
                        },
                    )
                    raise RuntimeError(str(exc)) from exc
                raise InputValidationError(str(exc)) from exc

            text = _as_text(value)
            if spec.output_as_file:
                saved = await ctx.files.write(_OUTPUT_FILENAME, text)
                return AgentOutput(
                    content=f"Output written to file: {saved}",
                    files=[saved],
                )
            return AgentOutput(content=text)

    return handler


def build_code_agent(spec: CodeAgentSpec, invitation_token: str) -> Agent:
    """Construct the single backplane `Agent` for one code-agent row.

    `accepts_schema` is operator-pinned from the row's parameter list;
    `produces_schema` from its `returns`, when declared. `invitation_token`
    is the admin-minted onboarding token; on resume from a persisted
    credentials file the SDK ignores it."""
    info = AgentInfo(
        agent_id=spec.agent_id,
        description=spec.description,
        groups=list(spec.groups),
        # `code.agent` is the coarse marker (every code agent has it),
        # paralleling `mcp.bridge` / `custom.agent`; operator caps append
        # for ACL targeting.
        capabilities=["code.agent", *spec.capabilities],
        accepts_schema=_accepts_schema(spec.parameters),
        produces_schema=spec.returns or None,
        produces_files=spec.output_as_file,
        hidden=not spec.expose_to_llm,
    )
    agent_config = AgentConfig(
        router_url=spec.router_url,
        state_dir=spec.state_dir / spec.agent_id,
        invitation_token=invitation_token,
    )
    agent = Agent(info=info, config=agent_config)
    agent.handler(mode=MODE)(make_code_handler(spec))
    return agent
