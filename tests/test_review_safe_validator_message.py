"""`safe_validator_message` bounds Pydantic-validator message echo.

Several wire-frame surfaces include `exc.errors()[0]['msg']` in
Ack / Error.reason responses. Pydantic message text CAN echo
input fragments verbatim (custom validators that f-string the
offending value), giving a hostile sender an oracle if they pump
oversized payloads through the validator.

The helper bounds the message at 200 chars by default so a
caller can't squeeze more out of the validator than necessary
for legitimate debugging.
"""

from __future__ import annotations

import inspect

import pytest


def test_safe_validator_message_returns_short_messages_unchanged() -> None:
    from pydantic import BaseModel, ValidationError

    from bp_protocol.errors import safe_validator_message

    class M(BaseModel):
        x: int

    try:
        M(x="not an int")  # type: ignore[arg-type]
    except ValidationError as exc:
        msg = safe_validator_message(exc)
        # Real Pydantic message for this case is roughly
        # "Input should be a valid integer, unable to parse string as an integer"
        # — well under the 200-char bound.
        assert len(msg) < 200
        assert msg


def test_safe_validator_message_truncates_oversized_messages() -> None:
    """Custom validator that echoes the offending input would be
    bounded. We simulate by passing a fake ValidationError whose
    errors()[0]['msg'] is artificially long."""
    from bp_protocol.errors import safe_validator_message

    class _FakeError:
        def errors(self) -> list[dict]:
            return [{"msg": "x" * 1000}]

    # 200-char default cap (199 + ellipsis = 200 chars total).
    result = safe_validator_message(_FakeError())  # type: ignore[arg-type]
    assert len(result) == 200
    assert result.endswith("…")


def test_safe_validator_message_handles_malformed_error_shape() -> None:
    """Defends against `errors()` returning an empty list or a
    dict missing the `msg` key (rare but custom code can do it)."""
    from bp_protocol.errors import safe_validator_message

    class _NoErrors:
        def errors(self) -> list[dict]:
            return []

    class _NoMsgKey:
        def errors(self) -> list[dict]:
            return [{"type": "value_error"}]

    assert safe_validator_message(_NoErrors()) == "validation failed"  # type: ignore[arg-type]
    assert safe_validator_message(_NoMsgKey()) == "validation failed"  # type: ignore[arg-type]


def test_safe_validator_message_respects_custom_max_len() -> None:
    from bp_protocol.errors import safe_validator_message

    class _FakeError:
        def errors(self) -> list[dict]:
            return [{"msg": "x" * 1000}]

    result = safe_validator_message(_FakeError(), max_len=50)  # type: ignore[arg-type]
    assert len(result) == 50


def test_router_dispatch_uses_safe_message() -> None:
    """`_handle_agent_info_update` formats Ack.reason via the
    helper, not raw `exc.errors()[0]['msg']`."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "safe_validator_message(exc)" in src
    assert "exc.errors()[0]" not in src


def test_router_wshub_uses_safe_message() -> None:
    """`_handshake` and `_run_socket` both format frame_invalid
    via the helper."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub)
    assert src.count("safe_validator_message(exc)") >= 2
    assert "exc.errors()[0]" not in src


def test_sdk_dispatch_uses_safe_message() -> None:
    """The SDK validation-error Ack also uses the helper — same
    leak path applies to SDK-side handler validation."""
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch)
    assert "safe_validator_message(exc)" in src
    assert "exc.errors()[0]" not in src
