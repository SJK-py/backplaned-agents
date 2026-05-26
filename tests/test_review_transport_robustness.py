"""Tests for the transport-robustness review fixes (WS-H1, WS-H2).

WS-H1 — SDK recv loop now bounds consecutive failures + backs off
between retries + bails to `run_until` when the cap is hit.
Previously a synchronous transport bug (programming error,
decoding bug, dead supervisor) would busy-loop at 100% CPU
spamming `recv_failed` logs forever.

WS-H2 — `admit_task` rejects spawns whose parent chain is at the
configured `spawn_max_depth` (default 16). Without it, agent A
spawning B spawning A → ... was unbounded and would exhaust
connection pool / WS outbox / task rows under runaway recursion
or adversarial topology.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# WS-H1: SDK recv loop has bounded failure backoff
# ===========================================================================


def test_recv_loop_bails_after_consecutive_failures() -> None:
    """Behavioral: a transport whose `recv()` always raises must
    cause `_recv_loop` to give up after the consecutive-failure
    cap, NOT spin at 100% CPU forever — and it gives up by RAISING
    `TransportPermanentlyFailed` (audit HIGH-1), not a silent
    `return` (which made the process exit 0 so a fleet on
    `Restart=on-failure` never restarted a permanently-dead agent).

    We patch the loop's internal sleep to a no-op so the test
    runs in milliseconds; the real loop honours the backoff
    schedule (verified separately at the source level)."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    class _FailingTransport:
        def __init__(self) -> None:
            self.recv_calls = 0

        async def recv(self) -> Any:
            self.recv_calls += 1
            raise RuntimeError(f"transport bug attempt {self.recv_calls}")

    disp = dispatch.Dispatcher.__new__(dispatch.Dispatcher)
    disp.transport = _FailingTransport()  # type: ignore[assignment]
    # The recv-loop now reads its bail threshold from
    # `self.agent.config.recv_consecutive_failures_max`
    # (gemini-readiness #8). Stub the chain with the production
    # default so the loop fires the cap at the same point it
    # used to.
    stub_config = SimpleNamespace(recv_consecutive_failures_max=16)
    disp.agent = SimpleNamespace(config=stub_config)  # type: ignore[assignment]

    async def _drive() -> None:
        # No-op sleep so the test doesn't pay the real backoff.
        with _patch_async_sleep():
            await disp._recv_loop()

    from bp_sdk.errors import TransportPermanentlyFailed

    with pytest.raises(TransportPermanentlyFailed):
        asyncio.run(_drive())
    # Should have called recv until the cap (16 in the production
    # constant) and then bailed. We don't pin the exact constant
    # here — just that we don't run unbounded.
    assert 1 < disp.transport.recv_calls <= 32, (
        f"recv loop called {disp.transport.recv_calls} times — "
        "expected the cap to fire under 32"
    )


def test_recv_loop_resets_failure_counter_on_success() -> None:
    """Only CONSECUTIVE failures count toward the cap. A single
    successful recv between failures resets the counter."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    class _IntermittentTransport:
        def __init__(self) -> None:
            self.script: list[Any] = []
            self.calls = 0

        async def recv(self) -> Any:
            self.calls += 1
            if not self.script:
                # Once the script is exhausted, simulate a clean
                # shutdown by raising CancelledError.
                raise asyncio.CancelledError
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    transport = _IntermittentTransport()
    # 5 fails, success, 5 fails, success — counter resets each
    # time, so we never hit the cap.
    for _ in range(5):
        transport.script.append(RuntimeError("boom"))
    transport.script.append(_FakeFrame())  # success
    for _ in range(5):
        transport.script.append(RuntimeError("boom"))
    transport.script.append(_FakeFrame())  # success

    disp = dispatch.Dispatcher.__new__(dispatch.Dispatcher)
    disp.transport = transport  # type: ignore[assignment]
    # Stub the agent.config chain so the recv-loop can read its
    # bail threshold (gemini-readiness #8).
    stub_config = SimpleNamespace(recv_consecutive_failures_max=16)
    disp.agent = SimpleNamespace(config=stub_config)  # type: ignore[assignment]
    # Stub _dispatch so successful recvs don't try to route.
    disp._dispatch = AsyncMock()  # type: ignore[assignment]

    async def _drive() -> None:
        with _patch_async_sleep():
            await disp._recv_loop()

    asyncio.run(_drive())
    # All 12 script entries consumed (5+1+5+1) before the
    # CancelledError bail.
    assert transport.calls == 12 + 1
    # The cap (16) was never tripped because failures didn't
    # cluster consecutively above it.


def test_recv_loop_source_uses_backoff_and_bail() -> None:
    """Source-level: the loop has both the consecutive-failure
    counter AND the backoff sleep call AND a hard bail return.
    Catches a future regression where someone removes the cap or
    the sleep."""
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher._recv_loop)
    assert "consecutive_failures" in src
    assert "MAX_CONSECUTIVE_FAILURES" in src
    assert "BACKOFF_INITIAL_S" in src
    assert "BACKOFF_MAX_S" in src
    # Backoff is exponential (uses `2 **`).
    assert "2 ** (consecutive_failures" in src
    # Bail return on the cap.
    assert "recv_loop_giving_up" in src
    # Counter resets on success.
    assert "consecutive_failures = 0" in src


# ===========================================================================
# WS-H2: spawn-depth cap rejects deep chains
# ===========================================================================


def test_count_task_chain_depth_query_shape() -> None:
    """The new `count_task_chain_depth` query uses a recursive CTE
    bounded by `_MAX_TASK_TREE_DEPTH`. Source-level pin so a
    refactor that drops the bound is caught immediately."""
    from bp_router.db import queries

    src = inspect.getsource(queries.count_task_chain_depth)
    # Shape pins: recursive CTE, user_id scoping at every step,
    # depth-counter bound, COALESCE for empty result.
    assert "WITH RECURSIVE" in src
    assert "ancestors" in src
    # User scoping in BOTH arms of the UNION ALL.
    assert src.count("user_id = $2") >= 2
    # Depth bound.
    assert "_depth + 1" in src
    assert "_depth <" in src
    assert "_MAX_TASK_TREE_DEPTH" in src
    # Empty-result safety.
    assert "COALESCE" in src


def test_count_task_chain_depth_returns_zero_for_missing_task() -> None:
    """When the task doesn't exist (or belongs to another user),
    return 0 — the caller treats that as "no parent" and proceeds.
    The C1 fix in `Scope.create_task` already rejects cross-user
    parent_task_id BEFORE this query runs."""
    from bp_router.db import queries

    class _StubConn:
        async def fetchrow(self, query: str, *args: Any) -> Any:
            # Recursive CTE with no anchor row → COALESCE returns 0.
            return {"depth": 0}

    out = asyncio.run(queries.count_task_chain_depth(
        _StubConn(),  # type: ignore[arg-type]
        task_id="tsk_nope",
        user_id="usr_alice",
    ))
    assert out == 0


def test_count_task_chain_depth_returns_actual_depth() -> None:
    """Happy path: a task whose chain is N levels deep returns N."""
    from bp_router.db import queries

    class _StubConn:
        async def fetchrow(self, query: str, *args: Any) -> Any:
            return {"depth": 7}

    out = asyncio.run(queries.count_task_chain_depth(
        _StubConn(),  # type: ignore[arg-type]
        task_id="tsk_x",
        user_id="usr_alice",
    ))
    assert out == 7


def test_admit_task_rejects_when_parent_chain_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral: when a parent's chain depth is already at the
    `spawn_max_depth` cap, admit_task raises
    `AdmitError('spawn_depth_exceeded')` BEFORE persisting the new
    task.

    Stubs every queries.* dependency so the test runs without a
    DB. The point is to verify the depth check is wired before
    `create_task`."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    # Fakes for the queries layer.
    monkeypatch.setattr(
        tasks.queries,
        "get_agent",
        AsyncMock(side_effect=[
            _agent("agt_caller"),  # caller_row
            _agent("agt_callee"),  # callee_row
        ]),
    )
    monkeypatch.setattr(
        tasks, "_session_level", AsyncMock(return_value="tier0")
    )
    monkeypatch.setattr(
        tasks, "is_allowed_for",
        lambda *a, **kw: type("D", (), {
            "allow": True, "rule_name": "stub",
        })(),
    )
    # The depth query reports the parent's chain at the cap.
    monkeypatch.setattr(
        tasks.queries,
        "count_task_chain_depth",
        AsyncMock(return_value=16),  # at default cap
    )
    # If the depth check is wired correctly, create_task NEVER fires.
    create_task_mock = AsyncMock()
    monkeypatch.setattr(
        "bp_router.db.queries.Scope.create_task", create_task_mock
    )

    state = _make_state(spawn_max_depth=16)
    frame = _new_task_frame(parent_task_id="tsk_deep_parent")

    with pytest.raises(tasks.AdmitError) as excinfo:
        asyncio.run(tasks.admit_task(state, frame, caller_agent_id="agt_caller"))
    assert excinfo.value.code == "spawn_depth_exceeded"
    create_task_mock.assert_not_called()


def test_admit_task_passes_when_parent_chain_below_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse: parent at depth N < cap → admit proceeds normally."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    monkeypatch.setattr(
        tasks.queries,
        "get_agent",
        AsyncMock(side_effect=[
            _agent("agt_caller"),
            _agent("agt_callee"),
        ]),
    )
    monkeypatch.setattr(
        tasks, "_session_level", AsyncMock(return_value="tier0")
    )
    monkeypatch.setattr(
        tasks, "is_allowed_for",
        lambda *a, **kw: type("D", (), {
            "allow": True, "rule_name": "stub",
        })(),
    )
    # Parent is 5 deep — well under default cap of 16.
    monkeypatch.setattr(
        tasks.queries,
        "count_task_chain_depth",
        AsyncMock(return_value=5),
    )
    # The session/transaction path is harder to stub end-to-end;
    # we verify that the depth check did NOT raise. A subsequent
    # error from a different stubbed-out call is acceptable here —
    # we just want to confirm the spawn-depth check didn't block.
    monkeypatch.setattr(
        "bp_router.db.queries.Scope.create_task",
        AsyncMock(side_effect=RuntimeError("stop here, depth check passed")),
    )

    state = _make_state(spawn_max_depth=16)
    frame = _new_task_frame(parent_task_id="tsk_normal_parent")

    # Either the test reaches the create_task RuntimeError (proof
    # that depth check didn't gate) or it reaches further admission
    # logic. Both confirm WS-H2 didn't false-positive here.
    with pytest.raises((tasks.AdmitError, RuntimeError)) as excinfo:
        asyncio.run(tasks.admit_task(state, frame, caller_agent_id="agt_caller"))
    # Importantly: NOT a spawn_depth_exceeded error.
    if isinstance(excinfo.value, tasks.AdmitError):
        assert excinfo.value.code != "spawn_depth_exceeded"


def test_admit_task_skips_depth_check_when_no_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level tasks (parent_task_id=None) skip the depth check
    entirely — they're new roots, not spawns."""
    pytest.importorskip("fastapi")
    from bp_router import tasks

    monkeypatch.setattr(
        tasks.queries,
        "get_agent",
        AsyncMock(side_effect=[
            _agent("agt_caller"),
            _agent("agt_callee"),
        ]),
    )
    monkeypatch.setattr(
        tasks, "_session_level", AsyncMock(return_value="tier0")
    )
    monkeypatch.setattr(
        tasks, "is_allowed_for",
        lambda *a, **kw: type("D", (), {
            "allow": True, "rule_name": "stub",
        })(),
    )
    depth_mock = AsyncMock(return_value=999)  # would trip if called
    monkeypatch.setattr(tasks.queries, "count_task_chain_depth", depth_mock)
    monkeypatch.setattr(
        "bp_router.db.queries.Scope.create_task",
        AsyncMock(side_effect=RuntimeError("create reached")),
    )

    state = _make_state(spawn_max_depth=16)
    frame = _new_task_frame(parent_task_id=None)

    with pytest.raises((tasks.AdmitError, RuntimeError)):
        asyncio.run(tasks.admit_task(state, frame, caller_agent_id="agt_caller"))
    # Critically: depth_mock was NEVER called for a parentless task.
    depth_mock.assert_not_called()


def test_settings_default_spawn_max_depth_is_16() -> None:
    """The default cap is 16. Operators can bump per-deployment for
    workflows that genuinely need deeper trees, but the out-of-the-
    box value is conservative."""
    from bp_router.settings import Settings

    s = Settings(  # type: ignore[call-arg]
        db_url="postgresql://u:p@h:5432/d",
        public_url="https://router.example.com",
        jwt_secret="x" * 32,
        admin_session_secret="x" * 32,
    )
    assert s.spawn_max_depth == 16


# ===========================================================================
# Helpers
# ===========================================================================


class _FakeFrame:
    """Sentinel for "successful recv". Real frames are Pydantic
    objects but the recv-loop test stubs `_dispatch` to no-op, so
    a bare object suffices."""


class _patch_async_sleep:
    """Context manager that no-ops `asyncio.sleep` so the recv-loop
    backoff test runs in milliseconds."""

    def __enter__(self) -> _patch_async_sleep:
        self._original = asyncio.sleep
        # Replace with a no-op coroutine.
        async def _no_sleep(*_a: Any, **_kw: Any) -> None:
            return None
        asyncio.sleep = _no_sleep  # type: ignore[assignment]
        return self

    def __exit__(self, *_args: Any) -> None:
        asyncio.sleep = self._original  # type: ignore[assignment]


def _agent(agent_id: str) -> Any:
    """Build a stub `AgentRow`-shaped object."""
    a = MagicMock()
    a.agent_id = agent_id
    a.status = "active"
    a.groups = []
    a.capabilities = []
    a.agent_info = {}
    return a


def _new_task_frame(*, parent_task_id: Any = None) -> Any:
    """Build a NewTaskFrame for admit_task tests."""
    from bp_protocol.frames import NewTaskFrame
    from bp_protocol.types import TaskPriority

    return NewTaskFrame(
        agent_id="agt_caller",
        trace_id="tr_test",
        span_id="sp_test",
        task_id=None,
        parent_task_id=parent_task_id,
        destination_agent_id="agt_callee",
        user_id="usr_alice",
        session_id="ses_1",
        priority=TaskPriority.NORMAL,
        payload={},
    )


def _make_state(*, spawn_max_depth: int = 16) -> Any:
    """Build a fake `state` for admit_task with the bare minimum."""
    state = MagicMock()
    state.settings.spawn_max_depth = spawn_max_depth
    state.settings.default_task_deadline_s = 300
    # `None` for every level disables the admit-rate quota (the
    # default for `admin` / `service` per `Settings`); short-circuits
    # before admit_task touches `state.admit_quota`. These tests
    # don't exercise the quota path.
    state.settings.quota_admit_rate_per_s = {"tier0": None}
    state.settings.quota_admit_burst = {"tier0": None}
    state.rules.rules = []  # ACL is stubbed at is_allowed_for layer
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"closed_at": None})
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return state
