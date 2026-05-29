"""Tests for Phase 10e: AgentInfoUpdateFrame + router handler +
catalog broadcast + SDK helper.

Four layers:

  * Frame: shape, discriminated-union registration, "at least one
    field" validator, wire round-trip.
  * Router dispatch: rate limit, patch merge, re-validate, persist,
    audit, broadcast.
  * Settings: rate-limit knobs.
  * SDK: PeerClient.update_agent_info — source pins on the
    frame construction, ack handling, in-process mutation.

End-to-end through a live WS handshake belongs in the manual
walkthrough.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# Frame definition
# ===========================================================================


def test_agent_info_update_frame_in_discriminated_union() -> None:
    """`AgentInfoUpdateFrame` is part of the Frame union so
    `parse_frame` dispatches it correctly. Without this the frame
    type comes back as the wrong class (or worse, validation fails
    silently)."""
    from bp_protocol.frames import AgentInfoUpdateFrame, parse_frame

    raw = {
        "type": "AgentInfoUpdate",
        "agent_id": "agt_x",
        "trace_id": "0" * 32,
        "span_id": "0" * 16,
        "description": "new desc",
    }
    frame = parse_frame(raw)
    assert isinstance(frame, AgentInfoUpdateFrame)
    assert frame.description == "new desc"


def test_agent_info_update_frame_all_fields_optional_default_none() -> None:
    from bp_protocol.frames import AgentInfoUpdateFrame

    fields = AgentInfoUpdateFrame.model_fields
    for name in (
        "description", "groups", "capabilities",
        "accepts_schema", "non_tool_modes", "produces_schema",
        "hidden", "documentation_url",
    ):
        assert name in fields
        assert fields[name].default is None


def test_agent_info_update_frame_rejects_empty_patch() -> None:
    """An update with no mutations is a no-op on the wire. The
    validator refuses so the bridge / SDK can't silently
    send empty frames that consume rate-limit budget for nothing."""
    from pydantic import ValidationError

    from bp_protocol.frames import AgentInfoUpdateFrame

    with pytest.raises(ValidationError, match="at least one"):
        AgentInfoUpdateFrame(
            agent_id="agt_x", trace_id="0" * 32, span_id="0" * 16,
        )


def test_agent_info_update_frame_accepts_single_field() -> None:
    from bp_protocol.frames import AgentInfoUpdateFrame

    frame = AgentInfoUpdateFrame(
        agent_id="agt_x", trace_id="0" * 32, span_id="0" * 16,
        hidden=True,
    )
    assert frame.hidden is True


def test_agent_info_update_frame_round_trips_wire() -> None:
    """JSON serialise + parse_frame yields the same fields."""
    from bp_protocol.frames import (
        AgentInfoUpdateFrame,
        parse_frame,
        serialize_frame,
    )

    original = AgentInfoUpdateFrame(
        agent_id="agt_x", trace_id="0" * 32, span_id="0" * 16,
        groups=["mcp_bridge", "fast"],
        capabilities=["text.gen"],
        accepts_schema={"type": "object"},
    )
    decoded = parse_frame(serialize_frame(original))
    assert isinstance(decoded, AgentInfoUpdateFrame)
    assert decoded.groups == ["mcp_bridge", "fast"]
    assert decoded.capabilities == ["text.gen"]
    assert decoded.accepts_schema == {"type": "object"}


# ===========================================================================
# Frame is NOT a NewTask in disguise
# ===========================================================================


def test_agent_info_update_frame_does_not_carry_agent_id_as_mutation() -> None:
    """The frame deliberately omits `agent_id` from the mutable
    surface (it's the header `agent_id` from `_FrameBase`,
    identifying the sender — the router uses it to know WHOSE
    AgentInfo to update). Pin so the field doesn't accidentally
    become mutable later."""
    from bp_protocol.frames import AgentInfoUpdateFrame

    # The frame inherits `agent_id` from _FrameBase, but the
    # post-Hello mutable fields are explicit and limited.
    mutable = {"description", "groups", "capabilities",
               "accepts_schema", "non_tool_modes", "mode_descriptions",
               "produces_schema", "produces_files", "hidden",
               "documentation_url"}
    declared = set(AgentInfoUpdateFrame.model_fields.keys())
    # Plus _FrameBase fields:
    base = {"type", "protocol_version", "correlation_id", "trace_id",
            "span_id", "timestamp", "agent_id"}
    extras = declared - mutable - base
    assert not extras, f"Unexpected fields on AgentInfoUpdateFrame: {extras!r}"


def test_router_mutable_set_matches_frame_patch_surface() -> None:
    """Lockstep guard. The router's merge allow-list
    (`dispatch._AGENT_INFO_MUTABLE_FIELDS`) MUST equal the frame's
    declared patch surface (`AgentInfoUpdateFrame` fields minus the
    `_FrameBase` envelope). If a mutable field is added to the frame
    but not the router tuple (or vice-versa), the merge loop silently
    drops the patch and the agent still gets `accepted=True` — the
    exact `produces_files` regression this pins against recurring.
    The sibling test above only checks the frame in isolation; this
    binds the two modules together."""
    from bp_protocol.frames import AgentInfoUpdateFrame
    from bp_router.dispatch import _AGENT_INFO_MUTABLE_FIELDS

    base = {"type", "protocol_version", "correlation_id", "trace_id",
            "span_id", "timestamp", "agent_id"}
    frame_patch_surface = set(AgentInfoUpdateFrame.model_fields) - base
    router_set = set(_AGENT_INFO_MUTABLE_FIELDS)
    assert router_set == frame_patch_surface, (
        "router _AGENT_INFO_MUTABLE_FIELDS drifted from "
        f"AgentInfoUpdateFrame — only-router={router_set - frame_patch_surface}, "
        f"only-frame={frame_patch_surface - router_set}"
    )


# ===========================================================================
# Settings — rate limit knobs
# ===========================================================================


def test_settings_has_agent_info_update_rate_limit_fields() -> None:
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "agent_info_update_rate_limit_per_agent_per_s" in fields
    assert "agent_info_update_rate_limit_per_agent_burst" in fields


def test_settings_rate_limit_defaults_are_sensible() -> None:
    """1/s sustained, burst 5 — leaves room for normal use
    (occasional admin patch, MCP-bridge incremental refresh) while
    bounding a misbehaving agent."""
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert fields["agent_info_update_rate_limit_per_agent_per_s"].default == 1.0
    assert fields["agent_info_update_rate_limit_per_agent_burst"].default == 5


# ===========================================================================
# Query helper
# ===========================================================================


def test_update_agent_info_query_persists_jsonb_and_denormalised_columns() -> None:
    """`agents.agent_info` is JSONB; `agents.groups` + `capabilities`
    are denormalised text[] columns the ACL evaluator reads from
    directly. Both must stay in sync, in one UPDATE."""
    from bp_router.db import queries

    src = inspect.getsource(queries.update_agent_info)
    assert "SET agent_info" in src
    assert "groups       = $3" in src
    assert "capabilities = $4" in src
    assert "WHERE agent_id = $1" in src
    # Single UPDATE statement.
    assert src.count("UPDATE agents") == 1


def test_update_agent_info_query_returns_truthy_on_hit() -> None:
    from bp_router.db import queries

    src = inspect.getsource(queries.update_agent_info)
    assert "result.endswith(\" 1\")" in src


# ===========================================================================
# Router-side handler
# ===========================================================================


def test_handler_rejects_with_rate_limit_when_quota_exhausted() -> None:
    """Source pin: the handler consults login_quota.try_consume
    before doing any DB work. Bucket key is per-agent (centralised
    `BUCKET_AGENT_INFO_UPDATE` prefix + agent_id) so one
    misbehaving agent doesn't starve others."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "BUCKET_AGENT_INFO_UPDATE" in src
    assert "entry.agent_id" in src
    assert "agent_info_update_rate_limit_per_agent_per_s" in src
    assert 'reason="rate_limited"' in src


def test_handler_merges_patch_with_existing_record() -> None:
    """Source pin: PATCH semantics — None fields skipped, existing
    record's values preserved, non-None fields overlay."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "for field in _AGENT_INFO_MUTABLE_FIELDS:" in src
    assert "if value is not None:" in src
    assert "merged = {**existing, **patch}" in src


def test_handler_locks_agent_id_post_merge() -> None:
    """agent_id is the stable identity — refresh tokens, ACL `@<id>`
    rules, audit history all depend on it. Defence in depth: even
    if a future frame variant exposes agent_id as a mutable field,
    the handler overwrites with the WS-authenticated value."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert 'merged["agent_id"] = entry.agent_id' in src


def test_handler_revalidates_via_agent_info_model() -> None:
    """The merged shape goes through `AgentInfo.model_validate`
    so the same field validators that gated Hello (group grammar,
    capability grammar, documentation_url scheme) re-run. Defence
    in depth alongside the AgentInfoUpdateFrame's own validators."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "AgentInfo.model_validate(merged)" in src


def test_handler_audits_fields_changed() -> None:
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert 'event="agent.info_updated"' in src
    assert '"fields_changed": sorted(patch.keys())' in src


def test_handler_broadcasts_catalog_update_after_persist() -> None:
    """Other agents' `peers.visible()` must reflect the change.
    Source pin on the broadcast call so a future refactor can't
    drop it."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "push_catalog_update_to_all(state)" in src


def test_handler_404s_when_agent_row_missing() -> None:
    """Defensive: an authenticated WS connection should always
    have a matching agents row, but if the row was deleted out
    from under it (admin evict mid-flight), surface a clear
    ack reason."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert 'reason="agent_not_found"' in src


def test_dispatch_routes_agent_info_update_frame() -> None:
    """Source pin: dispatch_frame's elif chain routes
    AgentInfoUpdateFrame to the new handler. Without this entry
    the frame falls through to the 'unexpected_frame_in_dispatch'
    warning."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch.dispatch_frame)
    assert "isinstance(frame, AgentInfoUpdateFrame)" in src
    assert "_handle_agent_info_update" in src


# ===========================================================================
# SDK helper
# ===========================================================================


def test_peer_client_has_update_agent_info() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    assert hasattr(PeerClient, "update_agent_info")
    assert inspect.iscoroutinefunction(PeerClient.update_agent_info)


def test_sdk_helper_rejects_empty_patch() -> None:
    """Mirror of the wire-side validator: agent code that calls
    update_agent_info() with all-None fields gets a ValueError
    BEFORE the round-trip."""
    pytest.importorskip("fastapi")
    # Phase 10f: PeerClient.update_agent_info became a thin wrapper
    # over Agent.update_info; source pins follow.
    from bp_sdk.agent import Agent
    src = inspect.getsource(Agent.update_info)
    assert "if all(" in src
    assert 'raise ValueError(' in src
    assert "empty patch" in src


def test_sdk_helper_builds_frame_with_all_fields() -> None:
    """Source pin: the helper threads every mutable field through
    to the frame constructor. Adding a new mutable field on the
    frame should also surface here; this pin catches the drift."""
    pytest.importorskip("fastapi")
    # Phase 10f: PeerClient.update_agent_info became a thin wrapper
    # over Agent.update_info; source pins follow.
    from bp_sdk.agent import Agent
    src = inspect.getsource(Agent.update_info)
    for field in (
        "description", "groups", "capabilities",
        "accepts_schema", "non_tool_modes",
        "produces_schema", "hidden", "documentation_url",
    ):
        assert f"{field}={field}" in src, f"helper drops field {field!r}"


def test_sdk_helper_awaits_ack_and_raises_on_rejection() -> None:
    """Source pin: the helper awaits an Ack via the dispatcher's
    pending_acks (same machinery as spawn) and raises
    PeerCallError on accepted=False."""
    pytest.importorskip("fastapi")
    # Phase 10f: PeerClient.update_agent_info became a thin wrapper
    # over Agent.update_info; source pins follow.
    from bp_sdk.agent import Agent
    src = inspect.getsource(Agent.update_info)
    assert "pending_acks" in src
    assert "ack.accepted" in src
    assert "raise PeerCallError(" in src


def test_sdk_helper_mutates_local_agent_info_on_success() -> None:
    """The agent's own peers.visible() / build_tools() reads from
    its in-process AgentInfo. After a successful patch update,
    the local copy must reflect the new values too — the router's
    CatalogUpdate broadcast only fixes OTHER agents."""
    pytest.importorskip("fastapi")
    # Phase 10f: PeerClient.update_agent_info became a thin wrapper
    # over Agent.update_info; source pins follow.
    from bp_sdk.agent import Agent
    src = inspect.getsource(Agent.update_info)
    # The mutation loop walks each (field, value) pair and
    # setattr's on info when value is not None.
    # Agent.update_info mutates `self.info` directly (it owns the
    # AgentInfo). No indirection through dispatcher.agent.info needed.
    assert "setattr(self.info, field, value)" in src


def test_sdk_helper_uses_correlation_id_for_ack_routing() -> None:
    """Same pattern as spawn — register the ack future against
    the frame's correlation_id BEFORE sending so a fast response
    can't race the registration."""
    pytest.importorskip("fastapi")
    # Phase 10f: PeerClient.update_agent_info became a thin wrapper
    # over Agent.update_info; source pins follow.
    from bp_sdk.agent import Agent
    src = inspect.getsource(Agent.update_info)
    # Order of operations: register, send, await.
    register_idx = src.index("register_for_task")
    send_idx = src.index(".transport.send(frame)")
    await_idx = src.index("ack_fut")
    assert register_idx < send_idx
    # The await happens after the send.
    second_await = src.index("ack_fut", await_idx + 1)
    assert send_idx < second_await
