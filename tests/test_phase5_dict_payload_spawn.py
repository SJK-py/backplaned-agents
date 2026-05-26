"""Tests for Phase 5: dict-or-class spawn payloads + spawn_from_tool_call.

Source-pin shape; the wire-level behaviour (router validation, dispatcher
re-typing) is exercised by the existing integration tests — those still
pass unchanged because the wire encoding is identical.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel


class _StubPayload(BaseModel):
    prompt: str
    extra: int = 0


# ===========================================================================
# Signature widening
# ===========================================================================


def test_peer_client_spawn_payload_accepts_basemodel_or_dict() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    sig = inspect.signature(PeerClient.spawn)
    annotation = sig.parameters["payload"].annotation
    rendered = str(annotation)
    # Pydantic + dict on the union (rendered with `|` under PEP 604).
    assert "BaseModel" in rendered
    assert "dict" in rendered


def test_peer_client_delegate_payload_accepts_basemodel_or_dict() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    sig = inspect.signature(PeerClient.delegate)
    rendered = str(sig.parameters["payload"].annotation)
    assert "BaseModel" in rendered
    assert "dict" in rendered


# ===========================================================================
# Branching on payload type
# ===========================================================================


def test_spawn_uses_model_dump_for_basemodel_payloads() -> None:
    """Source pin: when the payload is a BaseModel, the SDK must call
    `model_dump()` so the wire frame carries a plain dict — NOT the
    Pydantic instance, which doesn't JSON-serialise the same way."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.spawn)
    assert "isinstance(payload, BaseModel)" in src
    assert "payload.model_dump()" in src


def test_spawn_passes_dict_payloads_through_unchanged() -> None:
    """The dict branch must not transform the payload — that's the
    whole point of the convenience overload. Adding e.g. a copy()
    would silently break callers that rely on identity for downstream
    mutation tracking."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.spawn)
    # The else-branch binds payload directly to payload_dict.
    assert "else payload" in src


def test_delegate_branches_the_same_way() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.delegate)
    assert "isinstance(payload, BaseModel)" in src
    assert "payload.model_dump()" in src
    # delegate defensively copies the dict via dict(payload) rather
    # than aliasing the caller's dict into the frame payload.
    assert "dict(payload)" in src


# ===========================================================================
# spawn_from_tool_call sugar
# ===========================================================================


def test_peer_client_has_spawn_from_tool_call() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    assert hasattr(PeerClient, "spawn_from_tool_call")
    assert inspect.iscoroutinefunction(PeerClient.spawn_from_tool_call)


def test_spawn_from_tool_call_strips_call_prefix() -> None:
    """Source pin: the helper must strip the `call_` prefix that
    `build_tools` attaches. Pinning here so the prefix convention
    lives in exactly one place — tools.py emits it,
    spawn_from_tool_call removes it."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.spawn_from_tool_call)
    assert 'name.startswith("call_")' in src
    assert 'name.removeprefix("call_")' in src


def test_spawn_from_tool_call_rejects_non_call_prefixed_names() -> None:
    """An LLM hallucinating a tool the framework didn't advertise
    must not silently spawn an arbitrary agent. Source pin on the
    explicit ValueError."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.spawn_from_tool_call)
    assert "raise ValueError" in src
    assert "build_tools" in src  # error message points caller at the convention


def test_spawn_from_tool_call_forwards_kwargs() -> None:
    """All forwardable kwargs from spawn — wait, stream, timeout_s,
    idempotency_key, priority — must propagate through the helper.
    `mode` is the exception: the helper DERIVES it from the tool
    name (`tools.resolve_tool_name`), so it is intentionally not a
    parameter. Otherwise a caller migrating from spawn() loses
    behaviour they were relying on."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    spawn_params = set(inspect.signature(PeerClient.spawn).parameters)
    helper_params = set(inspect.signature(PeerClient.spawn_from_tool_call).parameters)
    # The helper's first positional is `tool_call` instead of
    # (destination_agent_id, payload); everything else carries over.
    assert "wait" in helper_params
    assert "stream" in helper_params
    assert "timeout_s" in helper_params
    assert "idempotency_key" in helper_params
    assert "priority" in helper_params
    # Symmetry: every non-payload, non-destination spawn kwarg is on
    # the helper EXCEPT `mode` (resolved from the tool name).
    for kw in spawn_params - {"self", "destination_agent_id", "payload", "mode"}:
        assert kw in helper_params, f"helper missing kwarg {kw!r} from spawn"


def test_spawn_from_tool_call_duck_types_tool_call() -> None:
    """Source pin: the helper reads `.name` and `.args` via getattr
    so it doesn't import bp_sdk.llm.ToolCall (would create a
    circular import). Documented in the docstring; this pin makes
    sure a future refactor doesn't regress to a typed parameter."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    sig = inspect.signature(PeerClient.spawn_from_tool_call)
    # The parameter type is `Any` (or unannotated), NOT a concrete
    # ToolCall import.
    annotation = sig.parameters["tool_call"].annotation
    assert annotation in (inspect.Parameter.empty, "Any") or "ToolCall" not in str(annotation)


def test_spawn_from_tool_call_handles_missing_args() -> None:
    """Source pin: a tool_call with `args=None` (rare — some providers
    emit this for zero-arg tools) must coerce to {} rather than
    crashing the underlying spawn."""
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    src = inspect.getsource(PeerClient.spawn_from_tool_call)
    assert 'getattr(tool_call, "args", {}) or {}' in src
