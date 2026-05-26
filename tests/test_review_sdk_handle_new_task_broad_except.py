"""SDK `_handle_new_task` surfaces any input-model exception as Ack.

R1 PR #128 fixed this hazard on the router side
(`_pick_handler_by_payload` catches any exception during model
validation). The SDK-side `Dispatcher._handle_new_task` had the
same hazard remaining: it caught only `ValidationError` from
`handler.input_model.model_validate(...)`. A misbehaving custom
validator that raised plain `ValueError` outside the
`@field_validator` path, a forward-ref resolution failure
(`NameError`), a malformed input_model (`TypeError`), or any
other programming bug propagated up with NO Ack sent — the
caller's spawn future hung to deadline (default 30s).

R4 fix: broaden the except to `Exception` and route both
branches through `_reject_new_task`. Pydantic's typed
`ValidationError` keeps its rich message (via
`safe_validator_message`); anything else gets a bounded
`validation_error: <ExcType>` reason so the caller's correlation
future resolves promptly.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def _bridge_imports_available() -> bool:
    try:
        import bp_sdk.dispatch  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _bridge_imports_available(),
    reason="bp_sdk imports require pydantic install",
)


def test_handle_new_task_catches_validation_error() -> None:
    """Baseline: Pydantic `ValidationError` already routed through
    `_reject_new_task` with `safe_validator_message`."""
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._handle_new_task)
    assert "except ValidationError" in src
    assert "safe_validator_message(exc)" in src
    assert "_reject_new_task(" in src


def test_handle_new_task_catches_broad_exception() -> None:
    """R4 fix: any non-Pydantic exception from `model_validate` also
    routes through `_reject_new_task`. Catches the spawn-hang vector
    from a misbehaving custom validator that raises TypeError /
    NameError / AttributeError / RuntimeError."""
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._handle_new_task)
    assert "except Exception" in src
    # Logs with stack so SDK author can debug.
    assert "input_model_unexpected_exception" in src
    # Surfaces a bounded reason — only the exception type, no
    # `repr(exc)` (which would leak internals).
    assert "type(exc).__name__" in src


def test_handle_new_task_validation_error_takes_precedence() -> None:
    """Source pin: `except ValidationError` MUST come before
    `except Exception` so Pydantic errors keep their rich
    `safe_validator_message` formatting and don't fall into the
    generic type-name branch."""
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._handle_new_task)
    val_idx = src.index("except ValidationError")
    broad_idx = src.index("except Exception")
    assert val_idx < broad_idx


def test_reject_new_task_helper_exists() -> None:
    """Helper factored out so both error branches (handler-not-found
    + input-model failure) share the AckFrame shape. Future ack
    schema changes update once."""
    from bp_sdk import dispatch

    assert hasattr(dispatch.Dispatcher, "_reject_new_task")


def test_handle_new_task_sends_ack_on_misbehaving_validator() -> None:
    """Functional pin: feed `_handle_new_task` a NewTaskFrame whose
    handler's `input_model.model_validate` raises a plain `TypeError`
    (the misbehaving-validator vector). The dispatcher must send an
    `accepted=False` AckFrame with a bounded reason — NOT raise out
    of the handler (which would leave the caller's spawn future
    hanging)."""
    from bp_protocol.frames import NewTaskFrame
    from bp_sdk import dispatch

    transport = MagicMock()
    transport.send = AsyncMock()

    agent = MagicMock()
    agent.info.agent_id = "agt_test"
    agent.config.pending_buffer_window_s = 0.05
    agent.config.pending_buffer_max_size = 64
    agent.config.pending_acks_timeout_s = 5.0
    agent.config.pending_results_timeout_s = 5.0
    agent.config.recv_consecutive_failures_max = 4

    disp = dispatch.Dispatcher(agent, transport)

    class _BadInputModel:
        @staticmethod
        def model_validate(payload):
            raise TypeError("simulated misbehaving validator")

    handler = MagicMock()
    handler.input_model = _BadInputModel

    disp._resolve_handler_for = MagicMock(return_value=handler)  # type: ignore[method-assign]
    disp._build_context = MagicMock()  # type: ignore[method-assign]

    frame = NewTaskFrame(
        agent_id="agt_caller",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id=None,
        destination_agent_id="agt_test",
        payload={"shape": "any"},
        correlation_id="c_xyz",
        user_id="usr_test",
        session_id="sess_test",
    )

    asyncio.run(disp._handle_new_task(frame))

    # ONE send call — the rejection ack.
    assert transport.send.await_count == 1
    ack = transport.send.await_args.args[0]
    assert ack.accepted is False
    # Reason carries the exception type name (bounded), not the
    # full repr (which could leak internals).
    assert "TypeError" in ack.reason
    assert "validation_error" in ack.reason
    # And matches the inbound correlation_id so the caller's
    # spawn future resolves on this Ack.
    assert ack.ref_correlation_id == "c_xyz"


def test_handle_new_task_swallows_runtime_error_too() -> None:
    """Belt-and-braces: not just TypeError. Any Exception subclass
    routes to the broad-except branch."""
    from bp_protocol.frames import NewTaskFrame
    from bp_sdk import dispatch

    transport = MagicMock()
    transport.send = AsyncMock()

    agent = MagicMock()
    agent.info.agent_id = "agt_test"
    agent.config.pending_buffer_window_s = 0.05
    agent.config.pending_buffer_max_size = 64
    agent.config.pending_acks_timeout_s = 5.0
    agent.config.pending_results_timeout_s = 5.0
    agent.config.recv_consecutive_failures_max = 4

    disp = dispatch.Dispatcher(agent, transport)

    class _BadInputModel:
        @staticmethod
        def model_validate(payload):
            raise RuntimeError("forward-ref resolution failure simulated")

    handler = MagicMock()
    handler.input_model = _BadInputModel
    disp._resolve_handler_for = MagicMock(return_value=handler)  # type: ignore[method-assign]
    disp._build_context = MagicMock()  # type: ignore[method-assign]

    frame = NewTaskFrame(
        agent_id="agt_caller",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id=None,
        destination_agent_id="agt_test",
        payload={},
        correlation_id="c_zzz",
        user_id="usr_test",
        session_id="sess_test",
    )
    asyncio.run(disp._handle_new_task(frame))
    assert transport.send.await_count == 1
    ack = transport.send.await_args.args[0]
    assert ack.accepted is False
    assert "RuntimeError" in ack.reason
