"""Regression: the bridge's post-connect info resync must not block the
dispatch loop from starting.

`_resync_info_on_connect` runs as an `on_startup` hook, which `Agent.run_async`
executes BEFORE entering `run_until` (the loop that reads the socket and
delivers acks). Awaiting `update_info` inline there deadlocked on its own ack
for the full `pending_acks_timeout_s` (30s) and stalled the agent from
servicing tool calls that whole time — the live
`mcp_server_bridge_resync_failed` / AckTimeout. The hook must instead fire the
resync as a background task that completes once the loop is live.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest


def test_resync_info_on_connect_is_non_blocking(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow.from_admin_dict({
        "server_id": "minimax", "transport": "stdio", "auth_kind": "none",
        "command": "uvx",
    })
    bridge = ServerBridge(
        row, admin_client=object(), router_url="w", state_dir=tmp_path,
    )

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_update_info(**_kwargs: object) -> None:
        # Stand-in for the real round-trip that can't ack until the dispatch
        # loop runs — block until explicitly released.
        started.set()
        await release.wait()

    bridge._agent = SimpleNamespace(  # type: ignore[assignment]
        info=SimpleNamespace(accepts_schema={}, non_tool_modes=[], capabilities=[]),
        update_info=_blocking_update_info,
    )

    async def _drive() -> None:
        # The hook must return promptly even though update_info blocks forever.
        await asyncio.wait_for(bridge._resync_info_on_connect(), timeout=1.0)
        assert bridge._resync_task is not None
        # The resync runs in the background, not inline.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert not bridge._resync_task.done()
        # Teardown cancels the outstanding resync task.
        await bridge._tear_down_agent()
        assert bridge._resync_task is None

    asyncio.run(_drive())


def test_resync_info_on_connect_noop_without_agent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bp_mcp_bridge.server_bridge import ServerBridge, ServerBridgeRow

    row = ServerBridgeRow.from_admin_dict({
        "server_id": "x", "transport": "stdio", "auth_kind": "none",
        "command": "uvx",
    })
    bridge = ServerBridge(
        row, admin_client=object(), router_url="w", state_dir=tmp_path,
    )

    async def _drive() -> None:
        await bridge._resync_info_on_connect()  # _agent is None → no task
        assert bridge._resync_task is None

    asyncio.run(_drive())


def test_ack_timeout_takes_no_kwargs() -> None:
    """`AckTimeout` has no custom __init__; `update_info` used to construct it
    with `task_id=None`, raising `TypeError: AckTimeout() takes no keyword
    arguments` and masking the real ack timeout."""
    from bp_sdk.peers import AckTimeout

    assert isinstance(AckTimeout("ack timed out"), Exception)
    with pytest.raises(TypeError):
        AckTimeout("ack timed out", task_id=None)  # type: ignore[call-arg]


def test_update_info_acktimeout_drops_task_id_kwarg() -> None:
    """Source pin: the ack-timeout branch must construct `AckTimeout` with a
    message only — no `task_id` kwarg (regression for the TypeError above)."""
    import re

    from bp_sdk.agent import Agent

    src = inspect.getsource(Agent.update_info)
    m = re.search(r"raise AckTimeout\((.*?)\)", src, re.DOTALL)
    assert m is not None, "update_info no longer raises AckTimeout"
    assert "task_id" not in m.group(1)
