"""Project one MCP server onto one backplane `Agent`.

The bridge builds ONE backplane `Agent` per MCP server, with one
MODE per MCP tool. Mode names = MCP tool names verbatim; the per-
mode `accepts_schema` is operator-pinned from each tool's
`inputSchema`. External LLM tool names land as
`call_mcp_<server>_<tool>` via the SDK's multi-mode rule
(`call_<agent_id>_<mode>`).

Why per-server instead of per-tool:

  * One catalog entry / WS socket / invitation / credential file per
    server — N-fold reduction in router-side bookkeeping for servers
    with many tools.
  * Mode adds / removes / schema-updates on `tools/list_changed`
    collapse to a single `Agent.set_modes(...)` call.
  * Long MCP tool names have no grammar limit — they live in the
    mode portion, not the agent_id (which is just `mcp_<server>`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from bp_mcp_bridge import metrics
from bp_mcp_bridge.config import BridgeConfig
from bp_mcp_bridge.mcp_client import (
    McpError,
    SseMcpClient,
    StreamableHttpMcpClient,
    ToolDefinition,
    ToolResult,
)
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext
from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)


# Retry policy for MCP tool calls.
#
# Transient: httpx network errors (the server is reachable in
# principle but a connect/read failed), and the JSON-RPC -32603
# "internal error" code (commonly used by MCP servers as a catch-
# all for transient infrastructure issues — DB hiccups, upstream
# API timeouts).
#
# Permanent: all other JSON-RPC error codes (-32600 invalid
# request, -32601 method not found, -32602 invalid params,
# application-defined error codes from the MCP server itself).
# These won't change shape on retry; surface immediately.
_MAX_ATTEMPTS = 3
_BACKOFF_INITIAL_S = 0.5
_BACKOFF_MAX_S = 4.0
_MCP_TRANSIENT_CODES = {-32603}
# HTTP status codes worth retrying. 429 (too many requests) and the
# 5xx server-error family are retried; 5xx covers transient upstream
# failures (502 bad gateway, 503 unavailable, 504 timeout) that
# typically clear within seconds. Other 4xx codes are client errors
# that retry won't fix.
_HTTP_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _is_transient(exc: Exception) -> bool:
    # `HTTPStatusError` is a SIBLING of `TransportError` under
    # `httpx.HTTPError`, NOT a subclass. The R2 PR #136 retry
    # initially missed it: a 502/503/504/429 from upstream raises
    # `HTTPStatusError` via `resp.raise_for_status()` inside
    # `mcp_client._call`, which surfaced immediately without retry
    # — exactly the case the retry was added for. Check this branch
    # FIRST so it's reachable even from a future refactor that
    # narrows the TransportError catch.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _HTTP_TRANSIENT_STATUS
    if isinstance(exc, httpx.TransportError):
        # Covers ConnectError, ReadTimeout, ReadError,
        # RemoteProtocolError, WriteError, ConnectTimeout, etc.
        # — anything where the request didn't get a clean HTTP
        # response from the server.
        return True
    if isinstance(exc, McpError):
        return exc.code in _MCP_TRANSIENT_CODES
    return False


async def _call_tool_with_retry(
    mcp_client: StreamableHttpMcpClient | SseMcpClient,
    tool_name: str,
    payload: dict,
    *,
    ctx: TaskContext,
    server_id: str,
) -> ToolResult:
    """Invoke `mcp_client.call_tool` with bounded retry on transient
    errors. Each attempt logs its outcome so operators can correlate
    retries with their upstream incidents.

    Cancellation flows through unchanged — `CancelledError` is NOT
    classified as transient."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await mcp_client.call_tool(tool_name, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_transient(exc) or attempt >= _MAX_ATTEMPTS:
                raise
            wait = min(
                _BACKOFF_INITIAL_S * (2 ** (attempt - 1)),
                _BACKOFF_MAX_S,
            )
            metrics.tool_call_retries_total.labels(
                server_id=server_id, tool=tool_name,
            ).inc()
            ctx.log.warning(
                "mcp_tool_call_retry",
                extra={
                    "event": "mcp_tool_call_retry",
                    "bp.mcp_server_id": server_id,
                    "bp.mcp_tool": tool_name,
                    "attempt": attempt,
                    "wait_s": wait,
                    "error": repr(exc),
                },
            )
            await asyncio.sleep(wait)
    # Unreachable — the loop either returns or re-raises.
    raise last_exc if last_exc is not None else RuntimeError("unreachable")


# Capability grammar is `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$` — strict
# lowercase + underscores only. MCP tool names commonly use camelCase
# (`getUser`) or hyphens (`list-files`); both shapes crash the
# AgentInfo validator if dropped into `mcp.tool.<name>` verbatim,
# preventing the entire bridge from registering. Lowercase and
# substitute any non-`[a-z0-9_]` for an underscore.
_CAPABILITY_INVALID_RE = re.compile(r"[^a-z0-9_]")


def _capability_segment(name: str) -> str:
    """Coerce an MCP tool name into a capability-grammar segment.

    Returns a non-empty string starting with `[a-z]` so the
    composed `mcp.tool.<segment>` matches `CAPABILITY_PATTERN`. If
    the name starts with a digit (or is all-non-alpha after
    normalisation), prefix `t_` so the first char is a letter.
    """
    s = _CAPABILITY_INVALID_RE.sub("_", name.lower())
    if not s or not s[0].isalpha():
        s = "t_" + s
    return s


def agent_id_for_server(server_id: str) -> str:
    """Compute the per-server backplane agent_id.

    server_id grammar (`^[a-z][a-z0-9_]+$`, lowercase letters /
    digits / underscores, no leading digit, ≥2 chars) is a STRICT
    subset of the agent_id grammar (`[A-Za-z_][A-Za-z0-9_-]{0,63}`),
    so `mcp_<server_id>` always validates without normalisation.
    The 64-char cap is generous (server_id stays well below it
    given the admin form's own length limits).
    """
    return f"mcp_{server_id}"


def _server_description(server_id: str, transport: str) -> str:
    """Description for the per-server `AgentInfo`. The router /
    admin UI surfaces this string when an operator inspects the
    catalog — keep it identifying."""
    return f"MCP server {server_id} ({transport})."


HandlerFactory = Callable[
    [TaskContext, dict],
    Awaitable[AgentOutput],
]


def make_tool_handler(
    mcp_client: StreamableHttpMcpClient | SseMcpClient,
    tool_name: str,
    server_id: str,
) -> HandlerFactory:
    """Build the per-tool handler closure.

    Each MCP tool becomes one MODE on the per-server backplane
    agent; the closure here is what `Agent.set_modes` registers for
    that mode. Closure captures `tool_name` so the dispatch is a
    plain dict lookup by mode — no shared mutable state crosses
    between modes.

    Behaviour: log the call, invoke `_call_tool_with_retry`, record
    duration + outcome metrics, prefix the result content with
    `[MCP tool error]` when MCP `isError=true` so the LLM parent
    sees the error class without parsing metadata.
    """

    async def handler(ctx: TaskContext, payload: dict) -> AgentOutput:
        """Forward payload to MCP server's `tools/call`. The router
        already admit-validated `payload` against the per-mode
        `accepts_schema[tool_name]` (= tool.input_schema), so the
        upstream call either succeeds with a result or raises an MCP
        error."""
        ctx.log.info(
            "mcp_tool_call",
            extra={
                "event": "mcp_tool_call",
                "bp.mcp_server_id": server_id,
                "bp.mcp_tool": tool_name,
            },
        )
        started = time.monotonic()
        try:
            result = await _call_tool_with_retry(
                mcp_client,
                tool_name,
                payload,
                ctx=ctx,
                server_id=server_id,
            )
        except BaseException:
            # Includes CancelledError (re-raised below) — we still
            # want the latency + a `failed` outcome recorded so a
            # tool that always cancels/raises is visible.
            metrics.tool_calls_total.labels(
                server_id=server_id, tool=tool_name,
                outcome="failed",
            ).inc()
            metrics.tool_call_duration_seconds.labels(
                server_id=server_id, tool=tool_name,
            ).observe(time.monotonic() - started)
            raise
        metrics.tool_call_duration_seconds.labels(
            server_id=server_id, tool=tool_name,
        ).observe(time.monotonic() - started)
        metrics.tool_calls_total.labels(
            server_id=server_id, tool=tool_name,
            outcome=("tool_error" if result.is_error else "success"),
        ).inc()
        rendered = result.as_agent_output_dict()
        content = rendered["content"]
        # MCP `isError=true` is the tool reporting "I refused / failed"
        # with a user-facing message in `content` — semantically a
        # successful RPC that returned an error message. The flag
        # already rides in `metadata.mcp_is_error` for programmatic
        # consumers, but an LLM parent reading the tool_result body
        # never sees metadata — only `content`. Prefix the content
        # with a recognisable marker so the LLM can distinguish "tool
        # errored" from "tool returned text" and adjust its strategy.
        if result.is_error:
            ctx.log.info(
                "mcp_tool_call_returned_error",
                extra={
                    "event": "mcp_tool_call_returned_error",
                    "bp.mcp_server_id": server_id,
                    "bp.mcp_tool": tool_name,
                },
            )
            marker = "[MCP tool error]"
            content = f"{marker}\n{content}" if content else marker
        return AgentOutput(
            content=content,
            metadata=rendered["metadata"],
        )

    return handler


def _build_capabilities(tools: list[ToolDefinition]) -> list[str]:
    """The per-server agent's capability list. `mcp.bridge` is the
    coarse marker (every bridged agent has it); per-tool
    `mcp.tool.<seg>` capabilities surface each tool individually so
    capability-pattern ACL rules (`mcp.tool.*`,
    `mcp.tool.search_*`) keep working unchanged from the per-tool
    agent era. De-duplicated and order-stable (insertion order of
    `tools`)."""
    caps: list[str] = ["mcp.bridge"]
    seen: set[str] = {"mcp.bridge"}
    for t in tools:
        cap = f"mcp.tool.{_capability_segment(t.name)}"
        if cap not in seen:
            caps.append(cap)
            seen.add(cap)
    return caps


def _accepts_schema_for(tools: list[ToolDefinition]) -> dict[str, Any]:
    """Per-server `accepts_schema = {mode: schema, ...}` — mode is
    the MCP tool name verbatim, schema is the tool's
    `inputSchema` operator-pinned (not derived; preserves the full
    upstream JSON schema so the router validates `payload` at admit
    exactly against what the MCP server documents)."""
    return {t.name: t.input_schema for t in tools}


def build_server_agent(
    config: BridgeConfig,
    mcp_client: StreamableHttpMcpClient | SseMcpClient,
    tools: list[ToolDefinition],
    invitation_token: str,
) -> Agent:
    """Construct the single backplane `Agent` for one MCP server.

    The agent has one mode per MCP tool; each mode's handler
    forwards `NewTaskFrame.payload` to the upstream MCP server's
    `tools/call`. `accepts_schema = {tool.name: tool.input_schema}`
    is operator-pinned at construction (full per-tool schemas; ALSO
    updated by `Agent.set_modes` on `tools/list_changed`).

    `invitation_token` is issued by the bridge ahead of time (one
    per server); on resume from a persisted credentials file the
    SDK ignores it.

    A server with zero tools at startup is still given a valid Agent
    — handlers are registered when tools appear via the first
    `tools/list_changed`. This lets the bridge stay connected
    through a temporarily-empty upstream rather than crashing the
    supervisor.
    """
    agent_id = agent_id_for_server(config.server_id)
    info = AgentInfo(
        agent_id=agent_id,
        description=_server_description(config.server_id, config.transport),
        groups=list(config.groups),
        capabilities=_build_capabilities(tools),
        # Operator-pinned: explicit `{mode: schema}` map. Pinning at
        # construction protects against `_republish_schemas` (the
        # decorator's auto-derive) from wiping the upstream schemas
        # into `{mode: None}` for dict-input handlers.
        accepts_schema=_accepts_schema_for(tools),
        # Per-server visibility. There is no per-mode (per-tool)
        # `hidden` today; that's a future operator-config hook noted
        # in the design doc (§12 Open Questions).
        hidden=not config.expose_to_llm,
    )
    agent_config = AgentConfig(
        router_url=config.router_url,
        # One credential dir per server (`state_dir/mcp_<server>/`).
        state_dir=config.state_dir / agent_id,
        invitation_token=invitation_token,
    )
    agent = Agent(info=info, config=agent_config)

    # Register one handler per tool. The decorator path is used at
    # construction time (pre-connect); subsequent reconciles call
    # `agent.set_modes(...)` for atomic add / remove / update.
    for t in tools:
        agent.handler(mode=t.name)(
            make_tool_handler(mcp_client, t.name, config.server_id)
        )

    return agent
