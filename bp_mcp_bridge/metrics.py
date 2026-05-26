"""bp_mcp_bridge.metrics — Prometheus metrics for the MCP bridge.

The bridge runs as its OWN process (separate from the router), so
it needs its own metric registry and its own `/metrics` HTTP
endpoint — it can't piggy-back on `bp_router.observability.metrics`
(that registry lives in the router process).

Design constraints:

  - **Never load-bearing.** Metrics are observability, not
    correctness. `prometheus_client` may be absent in a minimal
    bridge container. The import is defensive: when it's missing,
    every metric handle degrades to a no-op stub with the same
    `.labels(...).inc()/.observe()/.set()` surface, so call sites
    stay unconditional and the bridge never crashes for want of a
    metrics library.
  - **Bounded cardinality.** `server_id` and `tool` are
    operator-defined and finite (dozens, not millions) so they're
    safe as labels here — unlike `agent_id` in the router, which
    is caller-supplied and ephemeral. We still keep the label set
    tight.
  - **Bridge-local registry.** A dedicated `CollectorRegistry` so
    a future in-process test harness that imports both the router
    and the bridge doesn't get duplicate-series collisions.

`start_metrics_server(port)` spins up `prometheus_client`'s
background WSGI thread. No-op (logged once) when prometheus is
unavailable or the port is disabled (<= 0).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive prometheus_client import
# ---------------------------------------------------------------------------


class _NoopMetric:
    """Stand-in with the prometheus handle surface. Every method is
    a no-op; `labels()` returns self so chained `.inc()` works."""

    def labels(self, *args: object, **kwargs: object) -> _NoopMetric:
        return self

    def inc(self, amount: float = 1.0) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass

    def set(self, value: float) -> None:
        pass


try:  # pragma: no cover - exercised via both branches in tests
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    _PROM_AVAILABLE = True
    REGISTRY: CollectorRegistry | None = CollectorRegistry()
except Exception:  # noqa: BLE001  - ImportError or any init failure
    _PROM_AVAILABLE = False
    REGISTRY = None


def _counter(name: str, doc: str, labels: tuple[str, ...]) -> object:
    if not _PROM_AVAILABLE:
        return _NoopMetric()
    return Counter(name, doc, list(labels), registry=REGISTRY)


def _histogram(
    name: str, doc: str, labels: tuple[str, ...], buckets: tuple[float, ...]
) -> object:
    if not _PROM_AVAILABLE:
        return _NoopMetric()
    return Histogram(
        name, doc, list(labels), buckets=buckets, registry=REGISTRY
    )


def _gauge(name: str, doc: str, labels: tuple[str, ...]) -> object:
    if not _PROM_AVAILABLE:
        return _NoopMetric()
    return Gauge(name, doc, list(labels), registry=REGISTRY)


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------

# `outcome`:
#   success   — `call_tool` returned (is_error False)
#   tool_error — returned with MCP `isError=true` (tool reported failure)
#   failed    — raised after exhausting retries (permanent / max attempts)
tool_calls_total = _counter(
    "bp_mcp_bridge_tool_calls_total",
    "MCP tool invocations forwarded by the bridge, by outcome.",
    ("server_id", "tool", "outcome"),
)
# One increment per retry sleep (transient error → another attempt).
tool_call_retries_total = _counter(
    "bp_mcp_bridge_tool_call_retries_total",
    "Transient-error retries within a single bridged tool call.",
    ("server_id", "tool"),
)
tool_call_duration_seconds = _histogram(
    "bp_mcp_bridge_tool_call_duration_seconds",
    "Wall-clock latency of a bridged tool call (all retries included).",
    ("server_id", "tool"),
    (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)


# ---------------------------------------------------------------------------
# SSE transport
# ---------------------------------------------------------------------------

sse_reconnects_total = _counter(
    "bp_mcp_bridge_sse_reconnects_total",
    "SSE stream reconnect cycles (drop or clean-close then re-GET).",
    ("server_id",),
)
sse_pending_stranded_total = _counter(
    "bp_mcp_bridge_sse_pending_stranded_total",
    "In-flight SSE requests failed-fast because their stream reconnected.",
    ("server_id",),
)


# ---------------------------------------------------------------------------
# Lifecycle / reconciliation
# ---------------------------------------------------------------------------

bridge_starts_total = _counter(
    "bp_mcp_bridge_bridge_starts_total",
    "ServerBridge.run() invocations (one per (re)start of a server).",
    ("server_id",),
)
# `reason`: cancelled | error | returned
bridge_exits_total = _counter(
    "bp_mcp_bridge_bridge_exits_total",
    "ServerBridge.run() exits, by reason.",
    ("server_id", "reason"),
)
# Unlabelled — call `.set(n)` DIRECTLY, never `.labels().set(n)`.
# Real prometheus raises ValueError on `.labels()` for a no-label
# metric; the _NoopMetric stub tolerates both but call sites must
# use the real-prometheus-compatible form.
active_bridges = _gauge(
    "bp_mcp_bridge_active_bridges",
    "ServerBridge instances the supervisor currently has running.",
    (),
)
# `change`: added | removed | schema_changed
tool_reconcile_changes_total = _counter(
    "bp_mcp_bridge_tool_reconcile_changes_total",
    "Per-tool changes applied during an incremental reconcile pass.",
    ("server_id", "change"),
)
aclose_timeouts_total = _counter(
    "bp_mcp_bridge_aclose_timeouts_total",
    "MCP client aclose() calls that hit the bounded timeout.",
    ("server_id",),
)
invitations_issued_total = _counter(
    "bp_mcp_bridge_invitations_issued_total",
    "Service invitations the bridge self-issued for tool onboarding.",
    ("server_id",),
)


# ---------------------------------------------------------------------------
# HTTP exposition
# ---------------------------------------------------------------------------


def start_metrics_server(port: int) -> bool:
    """Start the Prometheus exposition HTTP server on `port`.

    Returns True if the server started, False otherwise (prometheus
    unavailable, port disabled, or bind failure). Never raises —
    a metrics-server failure must not take down the bridge.

    `port <= 0` disables metrics entirely (the opt-out for
    deployments that scrape via a sidecar or don't scrape at all).
    """
    if port <= 0:
        logger.info(
            "mcp_bridge_metrics_disabled",
            extra={"event": "mcp_bridge_metrics_disabled", "port": port},
        )
        return False
    if not _PROM_AVAILABLE:
        logger.warning(
            "mcp_bridge_metrics_unavailable",
            extra={
                "event": "mcp_bridge_metrics_unavailable",
                "reason": "prometheus_client not importable",
            },
        )
        return False
    try:
        start_http_server(port, registry=REGISTRY)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mcp_bridge_metrics_server_failed",
            extra={
                "event": "mcp_bridge_metrics_server_failed",
                "port": port,
                "error": repr(exc),
            },
        )
        return False
    logger.info(
        "mcp_bridge_metrics_started",
        extra={"event": "mcp_bridge_metrics_started", "port": port},
    )
    return True
