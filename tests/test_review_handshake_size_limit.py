"""WS handshake refuses oversized Hello frames before parse.

The per-frame receive loop (`_run_socket`) already enforces
`max_payload_bytes` against incoming raw text. The handshake
path missed the same check — an unauthenticated client could
send a giant Hello payload and burn Pydantic CPU before any
auth gate ran.

Fix: same byte-accurate `len(raw.encode("utf-8")) >
max_payload_bytes` guard BEFORE `parse_frame`, raising
`_HandshakeFailed("hello_too_large")` with close code 1009.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_handshake_refuses_oversized_hello() -> None:
    """Functional pin: a Hello frame whose UTF-8 byte size exceeds
    `max_payload_bytes` raises `_HandshakeFailed("hello_too_large")`
    with close_code 1009, BEFORE parse_frame is touched."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    # Craft an oversized raw frame (raw is consumed pre-parse, so
    # any string > cap triggers the guard regardless of JSON shape).
    state = MagicMock()
    state.settings.max_payload_bytes = 1024  # 1 KiB cap for the test
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value="x" * 2048)

    with pytest.raises(ws_hub._HandshakeFailed) as exc_info:
        asyncio.run(ws_hub._handshake(ws, state))

    assert exc_info.value.reason == "hello_too_large"
    assert exc_info.value.close_code == 1009


def test_handshake_byte_accurate_not_char_accurate() -> None:
    """A multibyte UTF-8 character is up to 4 bytes — using
    `len(raw)` (char count) would let a payload 4× over the byte
    cap slip past. Pin the BYTE measurement."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    # 300 characters, each 4 bytes (a non-BMP emoji) = 1200 bytes.
    # Cap is 1024.
    state = MagicMock()
    state.settings.max_payload_bytes = 1024
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value="🔐" * 300)

    with pytest.raises(ws_hub._HandshakeFailed) as exc_info:
        asyncio.run(ws_hub._handshake(ws, state))

    assert exc_info.value.reason == "hello_too_large"


def test_handshake_under_cap_proceeds_to_parse() -> None:
    """Companion check: a Hello UNDER the cap proceeds to the
    parse-frame stage. We feed garbage so parse_frame raises a
    different `_HandshakeFailed`, confirming the size guard
    passed through cleanly."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    state = MagicMock()
    state.settings.max_payload_bytes = 1_048_576
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value="{not even json")

    with pytest.raises(ws_hub._HandshakeFailed) as exc_info:
        asyncio.run(ws_hub._handshake(ws, state))

    # NOT "hello_too_large" — the parse path is what failed.
    assert exc_info.value.reason != "hello_too_large"


def test_handshake_size_check_runs_before_parse_frame() -> None:
    """Source pin: the size check appears in `_handshake` BEFORE
    the `parse_frame(raw)` call so an oversized payload never
    pays the Pydantic parse cost."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    lines = src.splitlines()
    size_idx = next(
        (i for i, line in enumerate(lines)
         if "raw.encode(" in line and "max_payload_bytes" in line),
        -1,
    )
    parse_idx = next(
        (i for i, line in enumerate(lines)
         if "parse_frame(raw)" in line),
        -1,
    )
    assert size_idx >= 0 and parse_idx >= 0
    assert size_idx < parse_idx, (
        "Size check must precede parse_frame so oversized payloads "
        "never pay the Pydantic cost."
    )


def test_handshake_close_code_1009_payload_too_large() -> None:
    """Source pin on close_code=1009: matches WebSocket RFC 6455
    code 1009 ("Message Too Big") and the existing per-frame
    receive loop's shape."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    # The size guard raises with close_code=1009.
    assert 'close_code=1009' in src
    # And uses the same reason string the per-frame loop uses.
    assert 'hello_too_large' in src or 'payload_too_large' in src


def test_handshake_does_not_duplicate_settings_load() -> None:
    """Defense against a stray `settings = state.settings` left
    behind by the refactor — the function should bind settings
    once at the top and use it throughout."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    # Only one binding of state.settings, near the top.
    assert src.count("settings = state.settings") == 1
