"""File transfer Phase 2: ws-negotiated upload credential.

An external agent can't auth the session-scoped file endpoints
with its `agent`-kind JWT. Phase 2 adds a ws negotiation:
`FileUploadRequest` → router authorises (the connection's
authenticated agent must be the task's active executor — the
`complete_task` rule) and replies `FileUploadGrant` with a
short-TTL, content-bound `file-upload` token; the SDK
(`FileStash._upload_blob`) then streams bytes to the Phase-1
endpoint over a separate http connection. Bulk bytes never touch
the ws control pump.

Security focus: the grant's `user_id` is derived from the task
row, NEVER from anything the agent sends.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bp_protocol.frames import (
    FileUploadGrantFrame,
    FileUploadRequestFrame,
    parse_frame,
    serialize_frame,
)
from bp_router.security import jwt as J

_SECRET = "k" * 32
_KV = 4


# ---------------------------------------------------------------------------
# Protocol frames
# ---------------------------------------------------------------------------


def test_frames_round_trip_through_discriminated_union() -> None:
    req = FileUploadRequestFrame(
        agent_id="agt", trace_id="t" * 32, span_id="s" * 16,
        task_id="tsk_1", sha256="ab" * 32, byte_size=10, mime_type="text/csv",
        filename="x.csv",
    )
    back = parse_frame(serialize_frame(req))
    assert isinstance(back, FileUploadRequestFrame)
    assert back.task_id == "tsk_1" and back.sha256 == "ab" * 32

    grant = FileUploadGrantFrame(
        agent_id="router", trace_id="t" * 32, span_id="s" * 16,
        ref_correlation_id=req.correlation_id,
        upload_url="/v1/files/upload", upload_token="tok",
    )
    gback = parse_frame(serialize_frame(grant))
    assert isinstance(gback, FileUploadGrantFrame)
    assert gback.ref_correlation_id == req.correlation_id
    assert gback.error is None


def test_request_frame_has_no_user_id_field() -> None:
    """Security pin: the agent cannot even *express* a user_id —
    the router derives it authoritatively from the task row."""
    assert "user_id" not in FileUploadRequestFrame.model_fields


# ---------------------------------------------------------------------------
# Router handler: authority is the task row, not the frame
# ---------------------------------------------------------------------------


def _router_state(*, active_agent_id: str, task_user_id: str = "usr_owner",
                   task_exists: bool = True, allowed: bool = True) -> MagicMock:
    state = MagicMock()
    s = state.settings
    s.jwt_secret.get_secret_value.return_value = _SECRET
    s.jwt_key_version = _KV
    s.jwt_algorithm = "HS256"
    s.file_upload_token_ttl_s = 300
    s.max_upload_bytes = 25 * 1024 * 1024
    s.file_upload_request_rate_limit_per_agent_per_s = 5.0
    s.file_upload_request_rate_limit_per_agent_burst = 20

    state.login_quota.try_consume = AsyncMock(
        return_value=MagicMock(allowed=allowed)
    )
    row = (
        {"user_id": task_user_id, "active_agent_id": active_agent_id}
        if task_exists else None
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    state.db_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    state.db_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return state


def _entry(agent_id: str) -> MagicMock:
    e = MagicMock()
    e.agent_id = agent_id
    e.outbox.put = AsyncMock()
    return e


def _req(task_id: str = "tsk_1") -> FileUploadRequestFrame:
    return FileUploadRequestFrame(
        agent_id="whatever",  # NOT trusted — entry.agent_id is
        trace_id="t" * 32, span_id="s" * 16,
        task_id=task_id, sha256="cd" * 32, byte_size=2048,
        mime_type="application/pdf", filename="r.pdf",
    )


def _run_handler(state, entry, frame):  # type: ignore[no-untyped-def]
    from bp_router.dispatch import _handle_file_upload_request

    asyncio.run(_handle_file_upload_request(state, entry, frame))
    return entry.outbox.put.await_args[0][0]  # the FileUploadGrantFrame


def test_active_agent_gets_token_scoped_to_task_user() -> None:
    """The authenticated connection agent IS the task's active
    executor → grant a token whose user_id is the TASK's user
    (not anything from the frame)."""
    state = _router_state(active_agent_id="agt_worker", task_user_id="usr_real")
    grant = _run_handler(state, _entry("agt_worker"), _req())
    assert grant.error is None
    assert grant.upload_url == "/v1/files/upload"
    g = J.verify_file_upload_token(
        grant.upload_token, secret=_SECRET, key_version=_KV
    )
    assert g.user_id == "usr_real"          # from the task row
    assert g.sha256 == "cd" * 32            # content-bound to the request
    assert g.byte_size == 2048


def test_non_active_agent_is_denied_opaquely() -> None:
    """Connection agent is NOT the task's active executor → opaque
    `denied`, no token (can't enumerate / can't upload for a task
    it doesn't service)."""
    state = _router_state(active_agent_id="agt_other")
    grant = _run_handler(state, _entry("agt_attacker"), _req())
    assert grant.error == "denied"
    assert grant.upload_token is None


def test_unknown_task_is_denied_with_same_opaque_error() -> None:
    state = _router_state(active_agent_id="x", task_exists=False)
    grant = _run_handler(state, _entry("agt_worker"), _req("tsk_missing"))
    assert grant.error == "denied"  # indistinguishable from not-your-task
    assert grant.upload_token is None


def test_rate_limited_is_distinguished() -> None:
    state = _router_state(active_agent_id="agt_worker", allowed=False)
    grant = _run_handler(state, _entry("agt_worker"), _req())
    assert grant.error == "rate_limited"


def test_oversize_request_denied_before_token() -> None:
    state = _router_state(active_agent_id="agt_worker")
    big = _req()
    big.byte_size = 25 * 1024 * 1024 + 1
    grant = _run_handler(state, _entry("agt_worker"), big)
    assert grant.error == "denied"
    assert grant.upload_token is None


# ---------------------------------------------------------------------------
# SDK dispatch routes the grant onto pending_acks
# ---------------------------------------------------------------------------


def test_sdk_dispatch_resolves_grant_on_pending_acks() -> None:
    from bp_protocol.types import AgentInfo
    from bp_sdk.agent import Agent
    from bp_sdk.dispatch import Dispatcher
    from bp_sdk.transport.inproc import InProcessTransport

    agent = Agent(info=AgentInfo(agent_id="a", description="t"))
    tr = InProcessTransport()
    tr.attach(inbound=asyncio.Queue(), outbound=asyncio.Queue())
    disp = Dispatcher(agent, tr)

    async def _run() -> None:
        fut = disp.pending_acks.register("corr-9")
        grant = FileUploadGrantFrame(
            agent_id="router", trace_id="t" * 32, span_id="s" * 16,
            ref_correlation_id="corr-9", upload_url="/v1/files/upload",
            upload_token="TK",
        )
        await disp._dispatch(grant)
        assert (await asyncio.wait_for(fut, 1.0)).upload_token == "TK"

    asyncio.run(_run())
