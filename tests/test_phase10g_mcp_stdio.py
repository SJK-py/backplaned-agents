"""Phase 10g — stdio MCP transport (subprocess + hardening).

Exercises the StdioMcpClient against a real fake MCP server subprocess, plus the
spawn-config / policy plumbing. No router, no network.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_FAKE_SERVER = '''
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    if mid is None:  # notification (e.g. notifications/initialized)
        continue
    m = msg.get("method")
    if m == "initialize":
        r = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}}
    elif m == "tools/list":
        r = {"tools": [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]}
    elif m == "tools/call":
        r = {"content": [{"type": "text", "text": "hi " + msg["params"]["arguments"].get("who", "")}], "isError": False}
    else:
        r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": r}) + "\\n")
    sys.stdout.flush()
'''


def test_stdio_client_initialize_list_call(tmp_path: Path) -> None:
    """End-to-end JSON-RPC over a subprocess's stdin/stdout."""
    from bp_mcp_bridge.mcp_client import StdioMcpClient, StdioSpawnConfig

    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER)

    async def _drive() -> None:
        spawn = StdioSpawnConfig(env={"PATH": os.environ.get("PATH", "/usr/bin")})
        c = StdioMcpClient("python", [str(script)], spawn=spawn, server_id="fake")
        info = await c.initialize()
        assert info["serverInfo"]["name"] == "fake"
        tools = await c.list_tools()
        assert [t.name for t in tools] == ["echo"]
        res = await c.call_tool("echo", {"who": "world"})
        assert res.content[0]["text"] == "hi world"
        assert res.is_error is False
        await c.aclose()

    asyncio.run(_drive())


def test_stdio_client_surfaces_subprocess_exit(tmp_path: Path) -> None:
    """A subprocess that exits before responding fails the in-flight call
    rather than hanging."""
    from bp_mcp_bridge.mcp_client import (
        McpError,
        StdioMcpClient,
        StdioSpawnConfig,
    )

    script = tmp_path / "dies.py"
    script.write_text("import sys; sys.exit(0)\n")

    async def _drive() -> None:
        spawn = StdioSpawnConfig(env={"PATH": os.environ.get("PATH", "/usr/bin")})
        c = StdioMcpClient("python", [str(script)], spawn=spawn, server_id="dies")
        with pytest.raises(McpError):
            await c.initialize()
        await c.aclose()

    asyncio.run(_drive())


def test_build_mcp_client_dispatches_stdio() -> None:
    from bp_mcp_bridge.mcp_client import StdioMcpClient, build_mcp_client

    c = build_mcp_client("stdio", None, command="uvx", args=["x"])
    assert isinstance(c, StdioMcpClient)


def test_build_mcp_client_stdio_requires_command() -> None:
    from bp_mcp_bridge.mcp_client import build_mcp_client

    with pytest.raises(ValueError, match="stdio transport requires a command"):
        build_mcp_client("stdio", None)


def test_stdio_policy_from_env() -> None:
    from bp_mcp_bridge.config import StdioPolicy

    pol = StdioPolicy.from_env({
        "BP_MCP_BRIDGE_UID_BASE": "30000",
        "BP_MCP_BRIDGE_UID_MAX": "39999",
        "BP_MCP_BRIDGE_ALLOWED_LAUNCHERS": "uvx, npx",
    })
    assert pol.uid_base == 30000
    assert pol.uid_max == 39999
    assert pol.allowed_launchers == ("uvx", "npx")
    # Defaults: no uid drop, uvx only.
    d = StdioPolicy.from_env({})
    assert d.uid_base == 0 and d.allowed_launchers == ("uvx",)


def test_stdio_spawn_scopes_env_and_rejects_disallowed_launcher(tmp_path: Path) -> None:
    """The bridge builds the child's env from a minimal base + resolved
    env_refs only — never the bridge's own secrets — and refuses a command
    outside the launcher allowlist."""
    from bp_mcp_bridge.config import StdioPolicy
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    os.environ["BP_TEST_MCP_SECRET_VALUE"] = "leak-me"
    row = ServerBridgeRow.from_admin_dict({
        "server_id": "minimax", "transport": "stdio", "auth_kind": "none",
        "command": "uvx", "args": ["minimax-mcp"],
        "env_refs": {"MINIMAX_API_KEY": "env://BP_TEST_MCP_SECRET_VALUE"},
    })
    pol = StdioPolicy(
        uid_base=0, uid_max=0, allowed_launchers=("uvx",), work_root=tmp_path,
    )
    bridge = ServerBridge(
        row, admin_client=object(), router_url="ws://r/",
        state_dir=tmp_path, stdio_policy=pol,
    )
    spawn = bridge._build_stdio_spawn()
    # Only the scoped env: PATH/HOME/LANG + the resolved ref. NOT the bridge's
    # other env (e.g. an unrelated secret var).
    assert spawn.env["MINIMAX_API_KEY"] == "leak-me"
    assert set(spawn.env) == {"PATH", "HOME", "LANG", "MINIMAX_API_KEY"}
    assert "BP_MCP_BRIDGE_SERVICE_SECRET" not in spawn.env

    bad = ServerBridgeRow.from_admin_dict({
        "server_id": "x", "transport": "stdio", "auth_kind": "none",
        "command": "bash",
    })
    bad_bridge = ServerBridge(
        bad, admin_client=object(), router_url="ws://r/",
        state_dir=tmp_path, stdio_policy=pol,
    )
    with pytest.raises(RuntimeError, match="not in the bridge allowlist"):
        bad_bridge._build_stdio_spawn()


def test_stdio_uid_is_deterministic_in_range(tmp_path: Path) -> None:
    from bp_mcp_bridge.config import StdioPolicy
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow.from_admin_dict({
        "server_id": "minimax", "transport": "stdio", "auth_kind": "none",
        "command": "uvx",
    })
    pol = StdioPolicy(uid_base=2000, uid_max=2099)
    b = ServerBridge(
        row, admin_client=object(), router_url="w", state_dir=tmp_path,
        stdio_policy=pol,
    )
    uid = b._stdio_uid()
    assert uid is not None and 2000 <= uid <= 2099
    assert b._stdio_uid() == uid  # stable

    # Disabled range → no drop.
    b2 = ServerBridge(
        row, admin_client=object(), router_url="w", state_dir=tmp_path,
        stdio_policy=StdioPolicy(),
    )
    assert b2._stdio_uid() is None


def test_stdio_preexec_builds_no_new_privs_and_drop():
    """Source pin: the preexec sets PR_SET_NO_NEW_PRIVS and drops uid (only
    when running as root) — the hardening can't silently regress."""
    import inspect

    from bp_mcp_bridge import mcp_client

    src = inspect.getsource(mcp_client._stdio_preexec)
    assert "_PR_SET_NO_NEW_PRIVS" in src
    assert "os.setgroups([])" in src
    assert "os.setuid" in src
    assert "geteuid() == 0" in src  # only drops as root
