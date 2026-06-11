"""SDK self-heal: re-onboard when the router rejects a stale credential.

A persisted agent token signed by an OLD `ROUTER_JWT_SECRET` (e.g. the secret
was rotated on an env rebuild while the agent's state volume survived) can't be
detected locally — the agent has no secret — so `onboard_or_resume` resumes it
and the router rejects the WS handshake with 4001 `auth_failed`. Previously the
transport just reconnect-looped on the dead token forever (→ SystemExit →
crash-loop). Now it drops the token and re-onboards with its invitation.

These tests pin: the close-code classifier (which closes warrant a re-onboard),
the handshake/recv detection, the purge-and-onboard helper, and the bounded
attempt budget.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# --- close-code classifier -------------------------------------------------


class _FakeClose(Exception):
    """Stand-in for websockets.ConnectionClosed (exposes .code/.reason)."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"{code} {reason}")
        self.code = code
        self.reason = reason


class _FakeCloseNested(Exception):
    """Older websockets shape: code/reason under .rcvd, not on the exception."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"{code} {reason}")
        self.rcvd = type("Close", (), {"code": code, "reason": reason})()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_FakeClose(4001, "auth_failed"), "auth_failed"),
        (_FakeClose(4001, ""), "auth_failed"),          # default reason
        (_FakeClose(4003, "agent_reprovision"), "agent_reprovision"),
        (_FakeClose(4003, "agent_reset"), "agent_reset"),
        (_FakeCloseNested(4003, "agent_reprovision"), "agent_reprovision"),
        # NOT re-onboard-worthy:
        (_FakeClose(4003, "agent_evicted"), None),      # terminal
        (_FakeClose(4003, "agent_suspended"), None),    # intentional stop
        (_FakeClose(4003, "superseded"), None),         # newer socket won
        (_FakeClose(4002, "heartbeat_timeout"), None),  # transient
        (_FakeClose(4029, "rate_limited"), None),       # transient
        (TimeoutError(), None),                         # no code at all
    ],
)
def test_credential_rejection_classifier(exc: BaseException, expected: str | None) -> None:
    pytest.importorskip("websockets")
    from bp_sdk.transport.ws import _credential_rejection_reason

    assert _credential_rejection_reason(exc) == expected


# --- handshake detection ---------------------------------------------------


def _transport(tmp_path: Any, **cfg_over: Any):  # type: ignore[no-untyped-def]
    pytest.importorskip("websockets")
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig
    from bp_sdk.transport.ws import WebSocketTransport

    kwargs: dict[str, Any] = {
        "state_dir": tmp_path,
        "auth_token": "stale.token",
        "invitation_token": "inv-123",
        **cfg_over,
    }
    config = AgentConfig(**kwargs)
    info = AgentInfo(agent_id="webapp", description="x")
    return WebSocketTransport(config, info=info)


class _FakeWs:
    """Minimal async ws: records sends, returns a queued recv (or raises)."""

    def __init__(self, recv_result: Any = None, recv_exc: BaseException | None = None) -> None:
        self._recv_result = recv_result
        self._recv_exc = recv_exc
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> Any:
        if self._recv_exc is not None:
            raise self._recv_exc
        return self._recv_result


def _error_frame(code: str) -> str:
    from bp_protocol.frames import ErrorFrame, serialize_frame

    return serialize_frame(
        ErrorFrame(
            agent_id="router", trace_id="0" * 32, span_id="0" * 16,
            code=code, message="rejected",
        )
    )


def test_do_hello_auth_failed_errorframe_raises_credential_rejected(tmp_path: Any) -> None:
    from bp_protocol.frames import ErrorCode
    from bp_sdk.transport.ws import _CredentialRejected

    t = _transport(tmp_path)
    ws = _FakeWs(recv_result=_error_frame(ErrorCode.AUTH_FAILED))
    with pytest.raises(_CredentialRejected) as ei:
        asyncio.run(t._do_hello(ws))
    assert ei.value.reason == "auth_failed"


def test_do_hello_other_errorframe_is_not_reonboard(tmp_path: Any) -> None:
    # A protocol-version rejection must NOT trigger re-onboard.
    from bp_protocol.frames import ErrorCode
    from bp_sdk.transport.ws import _CredentialRejected

    t = _transport(tmp_path)
    ws = _FakeWs(recv_result=_error_frame(ErrorCode.PROTOCOL_VERSION))
    with pytest.raises(Exception) as ei:  # noqa: PT011
        asyncio.run(t._do_hello(ws))
    assert not isinstance(ei.value, _CredentialRejected)


def test_do_hello_4001_close_without_errorframe_raises_credential_rejected(tmp_path: Any) -> None:
    from bp_sdk.transport.ws import _CredentialRejected

    t = _transport(tmp_path)
    ws = _FakeWs(recv_exc=_FakeClose(4001, "auth_failed"))
    with pytest.raises(_CredentialRejected):
        asyncio.run(t._do_hello(ws))


# --- reonboard_with_invitation ---------------------------------------------


def test_reonboard_purges_credentials_then_onboards(tmp_path: Any, monkeypatch: Any) -> None:
    from bp_protocol.types import AgentInfo
    from bp_sdk import onboarding
    from bp_sdk.settings import AgentConfig

    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"auth_token": "stale", "agent_id": "webapp"}))
    cfg = AgentConfig(state_dir=tmp_path, auth_token="stale", invitation_token="inv-1")
    info = AgentInfo(agent_id="webapp", description="x")

    async def _fake_onboard(_info: Any, config: Any) -> None:
        # The stale credential MUST be gone before onboard runs, else
        # onboard_or_resume would just resume it.
        assert not creds.exists(), "credentials.json must be purged before onboard"
        config.auth_token = "fresh.token"

    monkeypatch.setattr(onboarding, "onboard_or_resume", _fake_onboard)
    ok = asyncio.run(onboarding.reonboard_with_invitation(info, cfg))
    assert ok is True
    assert cfg.auth_token == "fresh.token"


def test_reonboard_without_invitation_returns_false_without_onboarding(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from bp_protocol.types import AgentInfo
    from bp_sdk import onboarding
    from bp_sdk.settings import AgentConfig

    called = False

    async def _fake_onboard(_info: Any, _config: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(onboarding, "onboard_or_resume", _fake_onboard)
    cfg = AgentConfig(state_dir=tmp_path, invitation_token=None)
    info = AgentInfo(agent_id="webapp", description="x")
    assert asyncio.run(onboarding.reonboard_with_invitation(info, cfg)) is False
    assert called is False


def test_reonboard_propagates_terminal_onboard_error(tmp_path: Any, monkeypatch: Any) -> None:
    # A 409 (evicted) / 403 (spent invitation) surfaces as a raise out of
    # onboard_or_resume; reonboard_with_invitation must NOT swallow it.
    from bp_protocol.types import AgentInfo
    from bp_sdk import onboarding
    from bp_sdk.settings import AgentConfig

    async def _fake_onboard(_info: Any, _config: Any) -> None:
        raise RuntimeError("onboard rejected with 409")

    monkeypatch.setattr(onboarding, "onboard_or_resume", _fake_onboard)
    cfg = AgentConfig(state_dir=tmp_path, invitation_token="inv-1")
    info = AgentInfo(agent_id="webapp", description="x")
    with pytest.raises(RuntimeError):
        asyncio.run(onboarding.reonboard_with_invitation(info, cfg))


# --- bounded attempt budget ------------------------------------------------


def test_maybe_reonboard_is_bounded_by_max_attempts(tmp_path: Any, monkeypatch: Any) -> None:
    from bp_sdk import onboarding

    t = _transport(tmp_path, reonboard_max_attempts=2)
    calls = 0

    async def _fake_reonboard(_info: Any, _config: Any) -> bool:
        nonlocal calls
        calls += 1
        return False  # never recovers → attempts accumulate

    monkeypatch.setattr(onboarding, "reonboard_with_invitation", _fake_reonboard)

    # Two attempts allowed, the third is capped (no further onboard calls).
    assert asyncio.run(t._maybe_reonboard()) is False
    assert asyncio.run(t._maybe_reonboard()) is False
    assert asyncio.run(t._maybe_reonboard()) is False
    assert calls == 2, "must stop calling onboard after reonboard_max_attempts"


def test_maybe_reonboard_budget_resets_on_successful_handshake(tmp_path: Any, monkeypatch: Any) -> None:
    from bp_sdk import onboarding

    t = _transport(tmp_path, reonboard_max_attempts=1)
    calls = 0

    async def _fake_reonboard(_info: Any, _config: Any) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(onboarding, "reonboard_with_invitation", _fake_reonboard)

    assert asyncio.run(t._maybe_reonboard()) is True
    # Budget (1) now spent; a second attempt is capped...
    assert asyncio.run(t._maybe_reonboard()) is False
    assert calls == 1
    # ...until a successful handshake resets the counter (what
    # _run_one_connection does once connected).
    t._reonboard_attempts = 0
    assert asyncio.run(t._maybe_reonboard()) is True
    assert calls == 2


def test_maybe_reonboard_no_invitation_short_circuits(tmp_path: Any, monkeypatch: Any) -> None:
    from bp_sdk import onboarding

    t = _transport(tmp_path, invitation_token=None)

    async def _boom(_info: Any, _config: Any) -> bool:
        raise AssertionError("must not be called without an invitation")

    monkeypatch.setattr(onboarding, "reonboard_with_invitation", _boom)
    assert asyncio.run(t._maybe_reonboard()) is False
