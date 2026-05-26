"""Per-frame-type drop counter for outbox-full deliveries.

R6 third-pass review (HIGH): `deliver_frame` raises on
`asyncio.QueueFull` without persistence; `fanout_frame` logs +
drops silently. The task state in the DB IS already terminal,
so an agent that misses a Result frame on the wire can recover
by polling the admin API — but the missed-on-wire event was
SILENT to operators.

R6 fix (minimal): increment
`router_deliver_frame_dropped_total{frame_type}` on every drop
+ a per-call log line. Operators can alert on
`Result`-frame drop rate distinct from `Progress` drops.

Durable persistence (a `pending_outbound` table + replay on
reconnect) is deferred to a future architectural piece — the
counter makes the gap observable so operators see the rate
before that work lands.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest


def test_metric_registered_with_frame_type_label() -> None:
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import deliver_frame_dropped_total

    assert (
        deliver_frame_dropped_total._name  # type: ignore[attr-defined]
        == "router_deliver_frame_dropped"
    )
    # Every wire-frame type should be accepted.
    for ft in ("Result", "Progress", "NewTask", "Ack", "Ping"):
        deliver_frame_dropped_total.labels(frame_type=ft)


def test_fanout_frame_increments_counter_on_queue_full() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("prometheus_client")
    from bp_router import delivery
    from bp_router.observability.metrics import deliver_frame_dropped_total

    # State with one live entry whose outbox is full.
    full_queue = asyncio.Queue(maxsize=1)
    asyncio.run(full_queue.put("blocker"))

    entry = MagicMock()
    entry.outbox = full_queue

    state = MagicMock()
    state.socket_registry.get.return_value = entry

    frame = MagicMock()
    frame.type = "Progress"

    before = deliver_frame_dropped_total.labels(
        frame_type="Progress"
    )._value.get()  # type: ignore[attr-defined]

    delivered = delivery.fanout_frame(state, ["agt_x"], frame)

    after = deliver_frame_dropped_total.labels(
        frame_type="Progress"
    )._value.get()  # type: ignore[attr-defined]

    assert delivered == 0
    assert after - before == 1


def test_deliver_frame_increments_counter_on_queue_full() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("prometheus_client")
    from bp_router import delivery
    from bp_router.observability.metrics import deliver_frame_dropped_total

    async def _run() -> None:
        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait("blocker")

        entry = MagicMock()
        entry.outbox = full_queue
        entry.inflight_correlations = set()

        state = MagicMock()
        state.socket_registry.get.return_value = entry

        frame = MagicMock()
        frame.type = "Result"
        frame.correlation_id = "c1"

        before = deliver_frame_dropped_total.labels(
            frame_type="Result"
        )._value.get()  # type: ignore[attr-defined]

        with pytest.raises(asyncio.QueueFull):
            await delivery.deliver_frame(
                state, "agt_x", frame, await_ack=False
            )

        after = deliver_frame_dropped_total.labels(
            frame_type="Result"
        )._value.get()  # type: ignore[attr-defined]

        assert after - before == 1

    asyncio.run(_run())


def test_deliver_frame_logs_queue_full() -> None:
    """Source pin: the QueueFull branch logs at WARNING with the
    agent_id, frame type, and correlation_id so operators can
    track which delivery saturated."""
    pytest.importorskip("fastapi")
    from bp_router import delivery

    src = inspect.getsource(delivery.deliver_frame)
    assert "deliver_frame_queue_full" in src
    assert "bp.frame.type" in src
    assert "bp.correlation_id" in src


def test_record_helper_is_defensive() -> None:
    """The helper swallows metric-import errors so a registry
    hiccup doesn't compound the original QueueFull failure."""
    pytest.importorskip("fastapi")
    from bp_router import delivery

    src = inspect.getsource(delivery._record_deliver_dropped)
    assert "try:" in src
    assert "except Exception" in src


def test_fanout_drop_emits_metric_per_dropped_agent() -> None:
    """Functional: 3 agent_ids fan out to a state where ALL of
    them have saturated outboxes → 3 increments."""
    pytest.importorskip("fastapi")
    pytest.importorskip("prometheus_client")
    from bp_router import delivery
    from bp_router.observability.metrics import deliver_frame_dropped_total

    full = asyncio.Queue(maxsize=1)
    full.put_nowait("blocker")

    entry = MagicMock()
    entry.outbox = full

    state = MagicMock()
    state.socket_registry.get.return_value = entry

    frame = MagicMock()
    frame.type = "Progress"

    before = deliver_frame_dropped_total.labels(
        frame_type="Progress"
    )._value.get()  # type: ignore[attr-defined]

    delivery.fanout_frame(state, ["a", "b", "c"], frame)

    after = deliver_frame_dropped_total.labels(
        frame_type="Progress"
    )._value.get()  # type: ignore[attr-defined]
    assert after - before == 3
