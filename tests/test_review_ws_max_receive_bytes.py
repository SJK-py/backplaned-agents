"""Verify `AGENT_WS_MAX_RECEIVE_BYTES` plumbs through to the WS
client's `max_size` and the size-coupling story (`AgentConfig` ↔
`websockets.connect` ↔ router's `max_payload_bytes`) is intact.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from bp_sdk.settings import AgentConfig
from bp_sdk.transport import ws  # safe: websockets is lazy-imported inside ws._run_one_connection

# ---------------------------------------------------------------------------
# AgentConfig field
# ---------------------------------------------------------------------------


def test_default_is_2_mib() -> None:
    assert AgentConfig().ws_max_receive_bytes == 2 * 1024 * 1024


def test_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WS_MAX_RECEIVE_BYTES", str(4 * 1024 * 1024))
    assert AgentConfig().ws_max_receive_bytes == 4 * 1024 * 1024


def test_lower_bound_enforced() -> None:
    # `Field(..., ge=1024)`. Below the bound is a hard validation error.
    with pytest.raises(ValidationError):
        AgentConfig(ws_max_receive_bytes=128)


# ---------------------------------------------------------------------------
# Source pin: ws.py threads the config, not the old magic constant
# ---------------------------------------------------------------------------


def test_ws_transport_uses_config_not_magic_constant() -> None:
    """Regression guard: `bp_sdk/transport/ws.py` must pass
    `self.config.ws_max_receive_bytes` to `websockets.connect(...)`
    — NOT a hardcoded `2 * 1024 * 1024`. The magic constant
    decoupled the SDK buffer from the router's negotiated
    `max_payload_bytes`, so an operator who raised the router cap
    would silently 1009 on outbound frames near the new cap."""
    src = inspect.getsource(ws.WebSocketTransport)
    # The connect call binds max_size to the configurable field.
    assert "max_size=self.config.ws_max_receive_bytes" in src
    # And no stray hardcoded 2 MiB literal sits in the connect line.
    connect_block = src[src.index("websockets.connect"):
                        src.index("ping_interval=None") + 32]
    assert "2 * 1024 * 1024" not in connect_block
