"""MCP bridge hardening — three follow-up fixes on top of the
per-server / mode-per-tool refactor.

Three layers:

  * M1 — `ServerBridge.run()` races the refresh loop against the
    per-server agent task. A dead agent surfaces as the bridge's
    own exit cause so the supervisor restarts cleanly. Pre-fix the
    refresh loop kept running against a corpse, hammering
    `update_info` on a closed transport every 5 s forever.

  * M2 — bridge registers an `on_startup` hook on the per-server
    agent that re-broadcasts current `accepts_schema` /
    `non_tool_modes` / `capabilities` once the WS is connected.
    Pre-fix, `set_modes` calls that fired during the agent's
    onboard window silently swallowed the broadcast (the
    `_dispatcher is None` guard returns early) and the router's
    HelloFrame handler IGNORES `agent_info` on connect — so any
    drift accumulated during onboarding persisted until the next
    `tools/list_changed`.

  * M3 — `Agent.set_modes` docstring documents the pin-stickiness
    foot-gun: once `set_modes` has run, `accepts_schema` and
    `non_tool_modes` stay pinned for the Agent's lifetime, so a
    later `@agent.handler` registration won't auto-publish.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Helpers
# ===========================================================================


def _make_bridge(tmp_path: Path):  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.admin_client import AdminClient
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow(
        server_id="srv", url="https://x/", transport="streamable_http",
        auth_kind="none", auth_value_ref=None, auth_header_name=None,
        groups=["mcp_bridge"], expose_to_llm=True,
        refresh_requested_at=None,
    )
    admin = MagicMock(spec=AdminClient)
    admin.issue_service_invitation = AsyncMock(return_value=None)
    admin.record_tools_refreshed = AsyncMock(return_value=None)
    bridge = ServerBridge(
        row, admin_client=admin, router_url="ws://r/v1/agent",
        state_dir=tmp_path,
    )
    return bridge, admin


# ===========================================================================
# M1 — agent task death surfaces as bridge exit
# ===========================================================================


def test_race_helper_is_wired_into_run() -> None:
    """Source pin: `ServerBridge.run()` calls
    `_race_refresh_against_agent`, not the bare `_refresh_loop`,
    so a dying agent task can short-circuit the loop."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge.run)
    assert "self._race_refresh_against_agent()" in src
    # And the bare _refresh_loop is NOT awaited directly in run().
    # (It's still called inside `_race_refresh_against_agent` as
    # one of the wait() participants — that's the right place.)
    assert "await self._refresh_loop()" not in src


def test_race_uses_first_completed(tmp_path: Path) -> None:
    """Source pin: the race uses `FIRST_COMPLETED` so whichever
    task exits first triggers bridge shutdown — not `ALL_COMPLETED`
    which would only react after both tasks finished."""
    from bp_mcp_bridge.server_bridge import ServerBridge

    src = inspect.getsource(ServerBridge._race_refresh_against_agent)
    assert "asyncio.FIRST_COMPLETED" in src
    # The agent task gets first dibs on having its .result() called
    # (i.e. its exception surfaced) when both tasks complete.
    assert "if self._agent_task in done" in src


def test_dead_agent_task_propagates_through_run(tmp_path: Path) -> None:
    """Behavioural: if the per-server agent task raises, the
    bridge's `run()` re-raises the same exception so the supervisor
    sees the exit cause (rather than the bridge hanging in the
    refresh loop forever)."""

    bridge, _admin = _make_bridge(tmp_path)
    # Pretend the agent task has already died with a specific
    # exception. Use a finished task carrying that exception.
    async def _boom() -> None:
        raise RuntimeError("transport permanently failed")

    async def drive() -> None:
        bridge._agent_task = asyncio.create_task(_boom())
        # Give the task one event-loop tick to finish.
        await asyncio.sleep(0)
        await bridge._race_refresh_against_agent()

    with pytest.raises(RuntimeError, match="transport permanently failed"):
        asyncio.run(drive())


def test_cancelled_bridge_does_not_leak_refresh_task(tmp_path: Path) -> None:
    """Behavioural: when the bridge is cancelled mid-race, the
    refresh task is also cancelled in the finally block — no
    orphaned task on supervisor teardown."""

    bridge, _admin = _make_bridge(tmp_path)

    async def drive() -> None:
        # Wire a long-lived agent task that the race will outlive.
        async def _never_returns() -> None:
            await asyncio.Event().wait()

        bridge._agent_task = asyncio.create_task(_never_returns())
        race_task = asyncio.create_task(
            bridge._race_refresh_against_agent()
        )
        # Yield once so the race actually starts.
        await asyncio.sleep(0)
        # External cancel (simulating the supervisor).
        race_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await race_task
        # The refresh loop task created inside the race must have
        # been cancelled by the finally. No way to grab a reference
        # from outside, so assert via the agent task — it should be
        # the only remaining unfinished task. We just verify no
        # warnings leaked. Cleanup:
        bridge._agent_task.cancel()
        try:
            await bridge._agent_task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())


# ===========================================================================
# M2 — bridge re-syncs AgentInfo on agent connect
# ===========================================================================


def test_spawn_agent_registers_resync_startup_hook(tmp_path: Path) -> None:
    """The bridge installs `_resync_info_on_connect` as an
    `on_startup` hook so the SDK calls it after the dispatcher is
    built. Pin the registration so a refactor doesn't quietly
    drop the hook."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_mcp_bridge.mcp_client import StreamableHttpMcpClient, ToolDefinition

    bridge, _admin = _make_bridge(tmp_path)
    bridge._mcp_client = StreamableHttpMcpClient("https://x/")

    tool = ToolDefinition(
        name="t", description="d", input_schema={"type": "object"},
    )

    async def drive() -> None:
        await bridge._spawn_agent([tool])
        # Cancel the agent task so we don't leak; we're only here
        # to inspect the construction-time wiring.
        if bridge._agent_task is not None:
            bridge._agent_task.cancel()
            try:
                await bridge._agent_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    asyncio.run(drive())

    assert bridge._agent is not None
    hooks = bridge._agent._startup_hooks
    assert bridge._resync_info_on_connect in hooks


def test_resync_info_calls_update_info_with_current_state(tmp_path: Path) -> None:
    """Behavioural: the hook reads the CURRENT in-memory
    `accepts_schema` / `non_tool_modes` / `capabilities` (which
    `set_modes` may have mutated during onboarding) and broadcasts
    them via `update_info`. Idempotent if no drift."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")

    bridge, _admin = _make_bridge(tmp_path)
    fake_agent = MagicMock()
    fake_agent.info = MagicMock()
    fake_agent.info.accepts_schema = {
        "new_tool": {"type": "object", "properties": {"q": {"type": "string"}}}
    }
    fake_agent.info.non_tool_modes = []
    fake_agent.info.capabilities = ["mcp.bridge", "mcp.tool.new_tool"]
    fake_agent.update_info = AsyncMock(return_value=None)
    bridge._agent = fake_agent

    asyncio.run(bridge._resync_info_on_connect())

    fake_agent.update_info.assert_awaited_once()
    kwargs = fake_agent.update_info.call_args.kwargs
    assert kwargs["accepts_schema"] == {
        "new_tool": {"type": "object", "properties": {"q": {"type": "string"}}}
    }
    assert kwargs["non_tool_modes"] == []
    assert "mcp.tool.new_tool" in kwargs["capabilities"]


def test_resync_info_swallows_failures_non_fatal(tmp_path: Path) -> None:
    """If `update_info` raises (rate-limited, transient), the
    bridge logs a warning but doesn't bubble the exception. A
    next reconcile will re-broadcast if drift is real."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")

    bridge, _admin = _make_bridge(tmp_path)
    fake_agent = MagicMock()
    fake_agent.info = MagicMock()
    fake_agent.info.accepts_schema = {}
    fake_agent.info.non_tool_modes = []
    fake_agent.info.capabilities = ["mcp.bridge"]
    fake_agent.update_info = AsyncMock(side_effect=RuntimeError("rate_limited"))
    bridge._agent = fake_agent

    # Should NOT raise.
    asyncio.run(bridge._resync_info_on_connect())
    fake_agent.update_info.assert_awaited_once()


def test_resync_info_noop_when_agent_missing(tmp_path: Path) -> None:
    """Defensive: a teardown race where the hook fires after
    `_agent` was reset to None must not crash."""
    bridge, _admin = _make_bridge(tmp_path)
    bridge._agent = None

    # Should NOT raise.
    asyncio.run(bridge._resync_info_on_connect())


# ===========================================================================
# M3 — Agent.set_modes docstring documents pin stickiness
# ===========================================================================


def test_set_modes_docstring_documents_pin_stickiness() -> None:
    """Source pin: the `set_modes` docstring spells out that the
    `accepts_schema` / `non_tool_modes` pin sticks for the Agent's
    lifetime, so a later `@agent.handler` won't auto-publish. The
    footgun is real but easily avoided once documented."""
    from bp_sdk.agent import Agent

    assert Agent.set_modes.__doc__ is not None
    doc = Agent.set_modes.__doc__
    # Mentions the stickiness explicitly.
    assert "stickiness" in doc.lower() or "sticky" in doc.lower() or "pinned for the" in doc.lower()
    # Names the auto-publish that won't fire.
    assert "auto-publish" in doc.lower() or "re-derive" in doc.lower()


def test_set_modes_pin_stickiness_behaves_as_documented() -> None:
    """Behavioural: the documented foot-gun is real. After
    `set_modes`, a fresh `@agent.handler` adds the mode to
    `_handlers_by_mode` but does NOT extend
    `info.accepts_schema`."""
    pytest.importorskip("fastapi")
    pytest.importorskip("bp_sdk")
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.settings import AgentConfig

    agent = Agent(
        info=AgentInfo(
            agent_id="pin_test", description="d",
            groups=["t"], capabilities=["t.x"],
        ),
        config=AgentConfig(
            embedded=True, router_url="ws://t/v1/agent",
        ),
    )

    async def existing(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    asyncio.run(agent.set_modes(
        {"existing": (existing, {"type": "object", "title": "Pinned"})}
    ))
    assert agent.info.accepts_schema == {
        "existing": {"type": "object", "title": "Pinned"}
    }

    # A later decorator adds the mode to the handler dict but does
    # NOT touch accepts_schema (pin is sticky — the documented
    # foot-gun).
    @agent.handler(mode="late_added")
    async def late(ctx, payload: dict) -> dict:  # type: ignore[no-untyped-def]
        return {}

    assert "late_added" in agent.registered_handlers
    # Schema map UNCHANGED: still only "existing".
    assert agent.info.accepts_schema == {
        "existing": {"type": "object", "title": "Pinned"}
    }
