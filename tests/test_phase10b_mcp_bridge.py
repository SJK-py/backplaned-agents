"""Tests for Phase 10b: the bp_mcp_bridge package.

Three layers:

  * Config — env loading + validation invariants.
  * MCP client — JSON-RPC framing + auth header construction +
    initialize/list/call shapes. Uses respx to mock httpx.
  * Tool-agent projection — agent_id derivation, AgentInfo shape
    (operator-pinned accepts_schema), dict-input handler wiring.

Live end-to-end tests against a real MCP server live outside this
file (Phase 10b's `live walkthrough` PR-description checklist).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Config — env loading
# ===========================================================================


def test_config_from_env_minimal() -> None:
    """Required fields only: SERVER_ID + URL. Everything else
    falls back to sensible defaults."""
    from bp_mcp_bridge.config import BridgeConfig

    cfg = BridgeConfig.from_env({
        "BP_MCP_BRIDGE_SERVER_ID": "filesystem",
        "BP_MCP_BRIDGE_URL": "https://mcp.example.com/mcp",
    })
    assert cfg.server_id == "filesystem"
    assert cfg.url == "https://mcp.example.com/mcp"
    assert cfg.transport == "streamable_http"
    assert cfg.auth_kind == "none"
    assert cfg.expose_to_llm is True
    assert cfg.groups == []


def test_config_from_env_parses_groups_csv() -> None:
    from bp_mcp_bridge.config import BridgeConfig

    cfg = BridgeConfig.from_env({
        "BP_MCP_BRIDGE_SERVER_ID": "fs",
        "BP_MCP_BRIDGE_URL": "https://x/",
        "BP_MCP_BRIDGE_GROUPS": "mcp_bridge, llm_tools, ",
    })
    assert cfg.groups == ["mcp_bridge", "llm_tools"]


def test_config_from_env_expose_to_llm_truthy_parsing() -> None:
    from bp_mcp_bridge.config import BridgeConfig

    for false_val in ("false", "0", "no", "off", "FALSE"):
        cfg = BridgeConfig.from_env({
            "BP_MCP_BRIDGE_SERVER_ID": "fs",
            "BP_MCP_BRIDGE_URL": "https://x/",
            "BP_MCP_BRIDGE_EXPOSE_TO_LLM": false_val,
        })
        assert cfg.expose_to_llm is False
    for true_val in ("true", "1", "yes", "TRUE", ""):
        cfg = BridgeConfig.from_env({
            "BP_MCP_BRIDGE_SERVER_ID": "fs",
            "BP_MCP_BRIDGE_URL": "https://x/",
            "BP_MCP_BRIDGE_EXPOSE_TO_LLM": true_val,
        })
        assert cfg.expose_to_llm is True


def test_config_from_env_missing_required_raises() -> None:
    from bp_mcp_bridge.config import BridgeConfig

    with pytest.raises(RuntimeError, match="BP_MCP_BRIDGE_URL"):
        BridgeConfig.from_env({"BP_MCP_BRIDGE_SERVER_ID": "fs"})
    with pytest.raises(RuntimeError, match="BP_MCP_BRIDGE_SERVER_ID"):
        BridgeConfig.from_env({"BP_MCP_BRIDGE_URL": "https://x/"})


def test_config_rejects_unknown_transport() -> None:
    from bp_mcp_bridge.config import BridgeConfig

    with pytest.raises(ValueError, match="transport must be one of"):
        BridgeConfig(
            server_id="fs", url="https://x/", transport="websocket",
            auth_kind="none", auth_value=None, auth_header_name=None,
        )


def test_config_rejects_auth_kind_without_value() -> None:
    """Mirrors the DB CHECK constraint — auth_kind=bearer requires
    auth_value to be set."""
    from bp_mcp_bridge.config import BridgeConfig

    with pytest.raises(ValueError, match="auth_value required"):
        BridgeConfig(
            server_id="fs", url="https://x/", transport="streamable_http",
            auth_kind="bearer", auth_value=None, auth_header_name=None,
        )


def test_config_rejects_header_auth_without_header_name() -> None:
    from bp_mcp_bridge.config import BridgeConfig

    with pytest.raises(ValueError, match="auth_header_name required"):
        BridgeConfig(
            server_id="fs", url="https://x/", transport="streamable_http",
            auth_kind="header", auth_value="secret", auth_header_name=None,
        )


# ===========================================================================
# MCP client — auth header construction
# ===========================================================================


def test_mcp_client_no_auth_sends_no_credential_headers() -> None:
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    client = StreamableHttpMcpClient("https://x/")
    assert "Authorization" not in client._headers
    # Standard JSON-RPC headers still present.
    assert client._headers["Content-Type"] == "application/json"


def test_mcp_client_bearer_auth_sets_authorization_header() -> None:
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    client = StreamableHttpMcpClient(
        "https://x/", auth_kind="bearer", auth_value="sk-test123",
    )
    assert client._headers["Authorization"] == "Bearer sk-test123"


def test_mcp_client_header_auth_uses_custom_header_name() -> None:
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    client = StreamableHttpMcpClient(
        "https://x/",
        auth_kind="header",
        auth_value="key123",
        auth_header_name="X-API-Key",
    )
    assert client._headers["X-API-Key"] == "key123"
    assert "Authorization" not in client._headers


def test_mcp_client_bearer_without_value_raises() -> None:
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    with pytest.raises(ValueError, match="auth_value required"):
        StreamableHttpMcpClient("https://x/", auth_kind="bearer", auth_value=None)


def test_mcp_client_header_without_name_raises() -> None:
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    with pytest.raises(ValueError, match="auth_header_name required"):
        StreamableHttpMcpClient(
            "https://x/", auth_kind="header", auth_value="x", auth_header_name=None,
        )


# ===========================================================================
# MCP client — JSON-RPC framing and protocol flow
# ===========================================================================


def test_mcp_client_initialize_requires_call_before_list_tools() -> None:
    """The MCP spec mandates initialize before any other RPC. The
    client enforces this client-side too so misordered call sites
    surface a clear error instead of an opaque upstream rejection."""
    import asyncio

    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    client = StreamableHttpMcpClient("https://x/")
    with pytest.raises(RuntimeError, match="initialize"):
        asyncio.run(client.list_tools())


def test_mcp_client_call_tool_requires_initialize() -> None:
    import asyncio

    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    client = StreamableHttpMcpClient("https://x/")
    with pytest.raises(RuntimeError, match="initialize"):
        asyncio.run(client.call_tool("read_file", {"path": "/x"}))


def test_mcp_client_call_increments_jsonrpc_id() -> None:
    """Source pin: each _call increments the request id. JSON-RPC
    spec requires unique request ids per call."""
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    src = inspect.getsource(StreamableHttpMcpClient._call)
    assert "self._request_id += 1" in src
    assert '"id": self._request_id' in src


def test_mcp_client_notify_omits_id() -> None:
    """JSON-RPC notifications have no `id`. Pin the absence so a
    future refactor doesn't accidentally turn notifications into
    requests."""
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    src = inspect.getsource(StreamableHttpMcpClient._notify)
    assert '"id"' not in src


def test_mcp_client_initialize_sends_notifications_initialized() -> None:
    """Spec: after receiving initialize response, client MUST send
    a `notifications/initialized` notification."""
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    src = inspect.getsource(StreamableHttpMcpClient.initialize)
    assert '"notifications/initialized"' in src


def test_mcp_client_raises_mcp_error_on_jsonrpc_error_object() -> None:
    """JSON-RPC error responses have an `error` object; our client
    surfaces these as `McpError` exceptions, preserving code and
    message for the caller."""
    from bp_mcp_bridge.mcp_client import McpError, StreamableHttpMcpClient

    src = inspect.getsource(StreamableHttpMcpClient._call)
    assert 'if "error" in data:' in src
    assert "raise McpError(" in src

    err = McpError(-32602, "Invalid params", data={"detail": "missing"})
    assert err.code == -32602
    assert err.message == "Invalid params"
    assert err.data == {"detail": "missing"}


def test_mcp_client_rejects_sse_response_in_phase_10b() -> None:
    """Phase 10b is synchronous-JSON only; SSE responses raise a
    clear error pointing at Phase 10c."""
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    src = inspect.getsource(StreamableHttpMcpClient._call)
    assert "text/event-stream" in src
    assert "Phase 10c" in src


# ===========================================================================
# MCP client — end-to-end with httpx mock
# ===========================================================================


def test_mcp_client_initialize_round_trips(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Mock httpx to verify initialize() POSTs the right body and
    parses the response."""
    import asyncio
    from unittest.mock import MagicMock

    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    captured_calls: list[dict] = []

    def make_resp(payload: dict) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.headers = {"content-type": "application/json"}
        resp.json = MagicMock(return_value=payload)
        return resp

    async def fake_post(self, url, *, json=None, headers=None):  # noqa: ARG001
        captured_calls.append({"url": url, "body": json, "headers": dict(headers or {})})
        method = json.get("method")
        if method == "initialize":
            return make_resp({"jsonrpc": "2.0", "id": json["id"], "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "stub", "version": "0.0"},
            }})
        # `notifications/initialized` is a notification — no
        # response payload mattered, but our mock returns one.
        return make_resp({"jsonrpc": "2.0"})

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    async def run() -> None:
        client = StreamableHttpMcpClient("https://x/")
        result = await client.initialize()
        assert result["protocolVersion"] == "2024-11-05"
        assert client._initialized is True
        await client.aclose()

    asyncio.run(run())

    # Two calls: initialize + notifications/initialized.
    assert len(captured_calls) == 2
    assert captured_calls[0]["body"]["method"] == "initialize"
    assert captured_calls[0]["body"]["params"]["clientInfo"]["name"] == "bp_mcp_bridge"
    assert captured_calls[1]["body"]["method"] == "notifications/initialized"
    assert "id" not in captured_calls[1]["body"]


def test_mcp_client_list_tools_parses_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    from unittest.mock import MagicMock

    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    response_map = {
        "initialize": {"protocolVersion": "2024-11-05", "capabilities": {}},
        "notifications/initialized": {},
        "tools/list": {
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                {
                    "name": "write_file",
                    "description": "Write a file",
                    "inputSchema": {"type": "object"},
                },
            ],
        },
    }

    async def fake_post(self, url, *, json=None, headers=None):  # noqa: ARG001
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.headers = {"content-type": "application/json"}
        resp.json = MagicMock(return_value={
            "jsonrpc": "2.0", "id": json.get("id"),
            "result": response_map.get(json["method"], {}),
        })
        return resp

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    async def run() -> None:
        client = StreamableHttpMcpClient("https://x/")
        await client.initialize()
        tools = await client.list_tools()
        assert len(tools) == 2
        assert tools[0].name == "read_file"
        assert tools[0].description == "Read a file"
        assert tools[0].input_schema["required"] == ["path"]
        assert tools[1].name == "write_file"
        await client.aclose()

    asyncio.run(run())


# ===========================================================================
# Tool result flattening
# ===========================================================================


def test_tool_result_flattens_text_content() -> None:
    from bp_mcp_bridge.mcp_client import ToolResult

    r = ToolResult(content=[
        {"type": "text", "text": "Line one"},
        {"type": "text", "text": "Line two"},
    ])
    out = r.as_agent_output_dict()
    assert out["content"] == "Line one\nLine two"
    assert out["metadata"]["mcp_is_error"] is False


def test_tool_result_handles_image_with_placeholder() -> None:
    """Phase 10b doesn't render images into AgentOutput content yet
    (that's the multimodal-document path from PR #108 to wire up).
    Surface a placeholder so the calling LLM knows there's an image
    in the result."""
    from bp_mcp_bridge.mcp_client import ToolResult

    r = ToolResult(content=[
        {"type": "image", "mimeType": "image/png", "data": "base64..."},
    ])
    out = r.as_agent_output_dict()
    assert "[image:image/png]" in out["content"]


def test_tool_result_carries_is_error_flag() -> None:
    """MCP tools can return content with isError=true to signal
    a tool-level failure. The bridge surfaces this on metadata so
    callers can branch."""
    from bp_mcp_bridge.mcp_client import ToolResult

    r = ToolResult(content=[{"type": "text", "text": "denied"}], is_error=True)
    out = r.as_agent_output_dict()
    assert out["metadata"]["mcp_is_error"] is True


# ===========================================================================
# agent_id derivation — per-server now (one Agent per MCP server)
# ===========================================================================


def test_agent_id_for_server_simple_case() -> None:
    from bp_mcp_bridge.tool_agent import agent_id_for_server

    assert agent_id_for_server("filesystem") == "mcp_filesystem"


def test_agent_id_for_server_grammar_compliant() -> None:
    """server_id grammar (`^[a-z][a-z0-9_]+$`) is a strict subset of
    the agent_id grammar (`[A-Za-z_][A-Za-z0-9_-]{0,63}`) — no
    normalisation needed and the result fits the 64-char cap for
    any valid server_id."""
    from bp_mcp_bridge.tool_agent import agent_id_for_server  # noqa: PLC0415
    from bp_protocol.types import AGENT_ID_PATTERN  # noqa: PLC0415

    for sid in ("fs", "filesystem", "github_repo", "x_y_z"):
        aid = agent_id_for_server(sid)
        assert len(aid) <= 64
        assert AGENT_ID_PATTERN.match(aid)


def test_long_mcp_tool_name_does_not_hash_agent_id() -> None:
    """A 200-char MCP tool name doesn't force any agent_id
    truncation. The tool name lives in the MODE portion which has
    no grammar limit; agent_id stays `mcp_<server>`. The per-tool
    `agent_id_for(server, tool)` API does not exist."""
    from bp_mcp_bridge import tool_agent  # noqa: PLC0415

    assert not hasattr(tool_agent, "agent_id_for")
    assert hasattr(tool_agent, "agent_id_for_server")


# ===========================================================================
# Tool-agent projection — one Agent per server, mode per tool
# ===========================================================================


def _stub_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.config import BridgeConfig

    return BridgeConfig(
        server_id="filesystem",
        url="https://x/",
        transport="streamable_http",
        auth_kind="none",
        auth_value=None,
        auth_header_name=None,
        groups=["mcp_bridge", "llm_tools"],
        expose_to_llm=True,
        state_dir=tmp_path,
    )


def _stub_tool() -> object:
    from bp_mcp_bridge.mcp_client import ToolDefinition

    return ToolDefinition(
        name="read_file",
        description="Read a file from the filesystem.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


def test_build_server_agent_publishes_input_schemas_keyed_by_tool_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Each MCP tool's inputSchema is published under its TOOL NAME
    as the mode key (`{tool.name: tool.input_schema}`). The operator-
    pin path preserves the schemas across handler registration that
    would otherwise null them out (`{mode: None}` for dict input)."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    mcp = StreamableHttpMcpClient("https://x/")

    agent = build_server_agent(config, mcp, [tool], invitation_token="tok")
    assert agent.info.accepts_schema == {tool.name: tool.input_schema}


def test_build_server_agent_sets_agent_id_per_server(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One Agent per server — `agent_id = mcp_<server_id>` regardless
    of how many tools the server exposes."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert agent.info.agent_id == "mcp_filesystem"


def test_build_server_agent_inherits_groups_from_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert agent.info.groups == ["mcp_bridge", "llm_tools"]


def test_build_server_agent_tags_capabilities_for_acl_targeting(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Per-tool capability `mcp.tool.<seg>` + the generic
    `mcp.bridge` capability — both pin-able via ACL. The capabilities
    list aggregates all tools' segments on the single per-server
    agent. Phase #115's prefix-glob lets admin write `*/mcp.tool.*`
    to target all bridged tools at once."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert "mcp.tool.read_file" in agent.info.capabilities
    assert "mcp.bridge" in agent.info.capabilities


def test_build_server_agent_uses_dict_input_handler_per_tool(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Each per-tool mode's handler takes `dict` input (the SDK seam
    that lets the bridge avoid generating Pydantic models from
    arbitrary MCP inputSchemas at runtime). Pin both the handler's
    input_model and the make_tool_handler closure annotation."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge import tool_agent
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    agent = tool_agent.build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    # The mode keyed by the tool name is registered with dict input.
    assert agent._handlers_by_mode[tool.name].input_model is dict

    # And the source pin on the handler factory's annotation, to
    # defend against a future refactor that silently re-types it.
    src = inspect.getsource(tool_agent.make_tool_handler)
    assert "payload: dict" in src


def test_build_server_agent_state_dir_is_per_server(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One credentials dir per server — `state_dir/mcp_<server>/`."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    tool = _stub_tool()
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [tool], "tok",
    )
    assert agent.config.state_dir == tmp_path / "mcp_filesystem"


def test_build_server_agent_hidden_when_expose_to_llm_false(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`expose_to_llm=False` hides the WHOLE per-server agent
    (every tool together). Per-tool visibility is a future feature
    via `non_tool_modes`; see the design doc §12 open questions."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.config import BridgeConfig
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = BridgeConfig(
        server_id="fs", url="https://x/", transport="streamable_http",
        auth_kind="none", auth_value=None, auth_header_name=None,
        groups=[], expose_to_llm=False,
        state_dir=tmp_path,
    )
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [_stub_tool()], "tok",
    )
    assert agent.info.hidden is True


def test_build_server_agent_handles_empty_tools(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A server with zero tools still gets a valid Agent — onboards
    and waits for the first `tools/list_changed` to populate modes.
    Capabilities are just `["mcp.bridge"]`; accepts_schema is empty."""
    pytest.importorskip("fastapi")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient
    from bp_mcp_bridge.tool_agent import build_server_agent

    config = _stub_config(tmp_path)
    agent = build_server_agent(
        config, StreamableHttpMcpClient("https://x/"), [], "tok",
    )
    assert agent.info.agent_id == "mcp_filesystem"
    assert agent.info.capabilities == ["mcp.bridge"]
    assert agent.info.accepts_schema == {}
    assert agent._handlers_by_mode == {}


# ===========================================================================
# Bridge orchestration
# ===========================================================================


def test_server_bridge_rejects_unknown_transport(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Phase 10d added SSE so 'sse' and 'streamable_http' both
    work. Genuinely unknown transports raise ValueError from the
    factory in `mcp_client.build_mcp_client`. Pin so future
    transports can be added without silent fallthrough."""
    import asyncio

    from bp_mcp_bridge.admin_client import AdminClient
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow(
        server_id="fs", url="https://x/", transport="grpc",  # invalid
        auth_kind="none", auth_value_ref=None, auth_header_name=None,
        groups=[], expose_to_llm=True, refresh_requested_at=None,
        pending_invitation_token="inv-tok",  # lets run() get past onboarding gate
    )
    admin_client = AdminClient(
        "http://router/", refresh_token="tok", state_dir=tmp_path
    )
    bridge = ServerBridge(
        row,
        admin_client=admin_client,
        router_url="ws://router/v1/agent",
        state_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="unknown MCP transport"):
        asyncio.run(bridge.run())


def test_server_bridge_invitation_skips_when_credentials_already_persisted() -> None:
    """Source pin: bridge resumes from existing credentials.json instead of
    consuming the onboarding invitation (the SDK ignores it when auth_token
    loads)."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._onboarding_invitation)
    assert "_creds_path().exists()" in src
    assert 'return ""' in src


def test_server_bridge_onboards_from_pending_invitation() -> None:
    """Source pin: the bridge onboards using the admin-stashed pending
    invitation token on the row — it no longer self-mints."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._onboarding_invitation)
    assert "self._row.pending_invitation_token" in src
