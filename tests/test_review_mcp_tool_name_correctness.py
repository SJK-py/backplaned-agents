"""MCP tool-name correctness.

The bridge derives three things from each MCP tool's `name` field:

1. The MODE name on the per-server backplane agent (verbatim).
2. The capability string `mcp.tool.<segment>` attached to AgentInfo
   (one per tool; aggregated on the per-server agent).
3. The handler that forwards `payload` to `mcp_client.call_tool`.

Real-world MCP servers use mixed-case tool names (`getUser`,
`fetchPullRequest`) and hyphens (`list-files`, `read-resource`).
Both shapes silently break the AgentInfo validator's
`CAPABILITY_PATTERN` (`^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)+$`), which
crashes the entire bridge on startup — no agent registers. Pin:

- `_capability_segment` lowercases + substitutes invalid chars so the
  composed capability passes the grammar.
- `agent_id_for_server` returns the simple `mcp_<server>` form — no
  hash-truncation needed since the agent_id is server-scoped (the
  tool name lives in the MODE portion which has no grammar limit).
- Per-tool handler closure (`make_tool_handler`) prefixes content
  with `[MCP tool error]` when `result.is_error` is True so the LLM
  reading the tool_result body sees the signal (the metadata flag
  alone is invisible to it).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bp_protocol.types import CAPABILITY_PATTERN

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_config(tmp_path: Path, server_id: str = "filesystem"):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.config import BridgeConfig

    return BridgeConfig(
        server_id=server_id,
        url="https://x/",
        transport="streamable_http",
        auth_kind="none",
        auth_value=None,
        auth_header_name=None,
        groups=["mcp_bridge", "llm_tools"],
        expose_to_llm=True,
        state_dir=tmp_path,
    )


def _stub_tool(name: str = "read_file"):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.mcp_client import ToolDefinition

    return ToolDefinition(
        name=name,
        description=f"MCP tool {name}.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


# ---------------------------------------------------------------------------
# (1) Capability grammar normalisation
# ---------------------------------------------------------------------------


def test_capability_segment_lowercases_camel_case() -> None:
    """A tool named `getUser` becomes the capability segment `getuser`
    so the composed `mcp.tool.getuser` matches CAPABILITY_PATTERN."""
    from bp_mcp_bridge.tool_agent import _capability_segment

    assert _capability_segment("getUser") == "getuser"
    assert _capability_segment("fetchPullRequest") == "fetchpullrequest"


def test_capability_segment_replaces_hyphens_with_underscores() -> None:
    """MCP servers commonly use hyphenated names (`list-files`).
    Hyphens are not in `[a-z0-9_]` so they get folded to underscores."""
    from bp_mcp_bridge.tool_agent import _capability_segment

    assert _capability_segment("list-files") == "list_files"
    assert _capability_segment("read-resource") == "read_resource"


def test_capability_segment_handles_leading_digit() -> None:
    """CAPABILITY_PATTERN requires the first char to be `[a-z]`. A
    name starting with a digit gets a `t_` prefix."""
    from bp_mcp_bridge.tool_agent import _capability_segment

    out = _capability_segment("2fa_verify")
    assert out.startswith("t_") or out[0].isalpha()
    assert CAPABILITY_PATTERN.match(f"mcp.tool.{out}") is not None


def test_capability_segment_handles_all_invalid_chars() -> None:
    """If the entire name is non-alpha-numeric (e.g. `///`),
    normalisation yields underscores; prepend `t_` so the segment
    still starts with a letter."""
    from bp_mcp_bridge.tool_agent import _capability_segment

    out = _capability_segment("///")
    assert out[0].isalpha()
    assert CAPABILITY_PATTERN.match(f"mcp.tool.{out}") is not None


def test_build_server_agent_capability_passes_grammar_for_camel_case(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Building a server agent for a tool named `getUser` must NOT
    raise pydantic ValidationError — `mcp.tool.getUser` (with capital
    `U`) doesn't match CAPABILITY_PATTERN. The normalised lowercase
    capability must land in `info.capabilities` instead."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool("getUser")
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert "mcp.tool.getuser" in agent.info.capabilities
    # Every capability must pass the grammar — this is the assertion
    # AgentInfo's `_cap_grammar` validator would have raised on.
    for cap in agent.info.capabilities:
        assert CAPABILITY_PATTERN.match(cap), f"{cap!r} fails CAPABILITY_PATTERN"


def test_build_server_agent_capability_passes_grammar_for_hyphenated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool("list-files")
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert "mcp.tool.list_files" in agent.info.capabilities
    for cap in agent.info.capabilities:
        assert CAPABILITY_PATTERN.match(cap), f"{cap!r} fails CAPABILITY_PATTERN"


def test_build_server_agent_capability_still_works_for_already_compliant(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Sanity: tool names that already match the grammar
    (`read_file`) keep their capability segment verbatim — the
    normaliser is a no-op on the common case."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool("read_file")
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert "mcp.tool.read_file" in agent.info.capabilities


def test_build_server_agent_aggregates_capabilities_for_multiple_tools(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The per-server agent's capability list is the union of
    `mcp.bridge` and one `mcp.tool.<seg>` per tool — order-stable
    (insertion-order of the tools list), de-duplicated, all valid
    against CAPABILITY_PATTERN."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tools = [_stub_tool("read_file"), _stub_tool("list-files"), _stub_tool("getUser")]
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), tools, "tok",
    )
    assert agent.info.capabilities[0] == "mcp.bridge"
    assert "mcp.tool.read_file" in agent.info.capabilities
    assert "mcp.tool.list_files" in agent.info.capabilities
    assert "mcp.tool.getuser" in agent.info.capabilities
    for cap in agent.info.capabilities:
        assert CAPABILITY_PATTERN.match(cap)


# ---------------------------------------------------------------------------
# (2) agent_id_for_server: simple per-server form
# ---------------------------------------------------------------------------


def test_agent_id_for_server_simple_form() -> None:
    """`agent_id_for_server` returns `mcp_<server_id>` verbatim —
    server_id grammar (`^[a-z][a-z0-9_]+$`) is a strict subset of
    the agent_id grammar so no normalisation is needed."""
    from bp_mcp_bridge.tool_agent import agent_id_for_server

    assert agent_id_for_server("filesystem") == "mcp_filesystem"
    assert agent_id_for_server("github_repo") == "mcp_github_repo"


def test_agent_id_for_server_distinct_per_server() -> None:
    """Two different servers produce two different agent_ids — no
    hash-truncation, no shared digest pool, no cross-server
    collision possible."""
    from bp_mcp_bridge.tool_agent import agent_id_for_server

    assert agent_id_for_server("fs") != agent_id_for_server("git")


def test_long_tool_names_do_not_force_truncation(tmp_path) -> None:
    """A 200-char tool name does NOT force any agent_id
    truncation: the tool name lives in the MODE portion (no
    grammar limit), and the agent_id stays the simple
    `mcp_<server>` form."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    long_name = "x" * 200
    config = _stub_config(tmp_path)
    tool = _stub_tool(long_name)
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert agent.info.agent_id == f"mcp_{config.server_id}"
    assert long_name in agent.info.accepts_schema  # mode = tool name


# ---------------------------------------------------------------------------
# (3) isError surfacing in the per-tool handler closure
# ---------------------------------------------------------------------------


class _StubLogger:
    def info(self, *args, **kwargs) -> None: pass
    def warning(self, *args, **kwargs) -> None: pass
    def error(self, *args, **kwargs) -> None: pass


class _StubCtx:
    log = _StubLogger()


def test_call_handler_prefixes_content_when_is_error(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When the MCP tool returns `isError=true`, the bridge prefixes
    the AgentOutput content with `[MCP tool error]` so an LLM parent
    reading the tool_result body can distinguish a tool-reported error
    from a successful text result.

    Without this prefix the LLM only sees the raw error text and may
    misinterpret it as a normal response (the `mcp_is_error` metadata
    flag is invisible to it)."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient, ToolResult
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")
    agent = build_server_agent(config, mcp, [tool], "tok")

    # Locate the registered dict handler — mode == tool name now.
    handler = agent._handlers_by_mode[tool.name]

    error_result = ToolResult(
        content=[{"type": "text", "text": "permission denied: /etc/shadow"}],
        is_error=True,
    )

    async def fake_call_tool(name: str, payload: dict):  # type: ignore[no-untyped-def]
        assert name == "read_file"
        return error_result

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    out = asyncio.run(handler.fn(_StubCtx(), {"path": "/etc/shadow"}))

    assert out.content is not None
    assert out.content.startswith("[MCP tool error]")
    assert "permission denied" in out.content
    # Metadata flag is still surfaced for programmatic consumers.
    assert out.metadata.get("mcp_is_error") is True


def test_call_handler_does_not_prefix_when_success(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The marker prefix MUST NOT be added on success — otherwise
    every successful tool call would look like an error to the LLM."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient, ToolResult
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")
    agent = build_server_agent(config, mcp, [tool], "tok")

    handler = agent._handlers_by_mode[tool.name]

    success_result = ToolResult(
        content=[{"type": "text", "text": "file contents go here"}],
        is_error=False,
    )

    async def fake_call_tool(name: str, payload: dict):  # type: ignore[no-untyped-def]
        return success_result

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    out = asyncio.run(handler.fn(_StubCtx(), {"path": "/etc/hosts"}))

    assert out.content == "file contents go here"
    assert "[MCP tool error]" not in out.content
    assert out.metadata.get("mcp_is_error") is False


def test_call_handler_handles_empty_error_content(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Edge case: `isError=true` with empty content. The bridge must
    still emit the marker so the LLM sees the signal — content==''
    is technically valid AgentOutput but loses the error signal
    entirely without the prefix."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient, ToolResult
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")
    agent = build_server_agent(config, mcp, [tool], "tok")

    handler = agent._handlers_by_mode[tool.name]

    empty_error = ToolResult(content=[], is_error=True)

    async def fake_call_tool(name: str, payload: dict):  # type: ignore[no-untyped-def]
        return empty_error

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    out = asyncio.run(handler.fn(_StubCtx(), {"path": "/tmp"}))

    assert out.content is not None
    assert "[MCP tool error]" in out.content
