"""WS per-socket outbox honours `settings.per_socket_outbox_max`.

R6 third-pass review (HIGH): `SocketEntry`'s `outbox` field had
a hardcoded `default_factory=lambda: asyncio.Queue(256)`. The
`settings.per_socket_outbox_max` field existed (`settings.py:154`)
but no caller read it — operators raising the cap for bursty
deployments saw no effect on the actual queue size.

R6 fix: `_handshake` builds the outbox via `_new_outbox(settings)`
which reads the field. Resume-path preserves the existing outbox
(carries whatever cap was in effect at the original handshake).
The dataclass default factory keeps `Queue(256)` so test /
tooling code that builds `SocketEntry` without settings still
gets a sensible cap.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest


def test_new_outbox_reads_settings_field() -> None:
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import _new_outbox

    settings = MagicMock()
    settings.per_socket_outbox_max = 4096
    out = _new_outbox(settings)
    assert isinstance(out, asyncio.Queue)
    assert out.maxsize == 4096


def test_new_outbox_falls_back_to_256_default() -> None:
    """A settings object missing the field (older config / partial
    test fixture) doesn't crash — falls back to the same default
    the dataclass field uses."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import _new_outbox

    settings = object()  # no per_socket_outbox_max attribute
    out = _new_outbox(settings)
    assert isinstance(out, asyncio.Queue)
    assert out.maxsize == 256


def test_handshake_uses_new_outbox_for_fresh_session() -> None:
    """Source pin: the fresh-session branch (no resume) constructs
    the SocketEntry with `outbox=_new_outbox(settings)`, not the
    dataclass default."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    # The fresh-session SocketEntry passes outbox= explicitly.
    assert "outbox=_new_outbox(settings)" in src


def test_resume_path_preserves_existing_outbox() -> None:
    """The resume path inherits the parked entry's outbox (which
    already holds frames queued during the disconnect window).
    Source pin: still threads `outbox=resumed.outbox`."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    assert "outbox=resumed.outbox" in src


def test_socket_entry_default_factory_still_works_standalone() -> None:
    """Defensive: building a SocketEntry without going through
    `_handshake` still produces a usable outbox (size 256, the
    pre-R6 hardcoded value). Tests + tooling rely on this."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketEntry

    ws = MagicMock()
    entry = SocketEntry(
        agent_id="agt_x",
        websocket=ws,
        session_token="tok",
    )
    assert entry.outbox.maxsize == 256


def test_settings_field_still_present_in_model() -> None:
    """The Settings field must remain — operators reading docs
    expect to be able to tune it via env var."""
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "per_socket_outbox_max" in fields
