"""bp_sdk._metrics — Tiny Prometheus wrapper for ctx.metric().

Internal — agents should use `ctx.metric(...)` instead of importing this.
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

_metrics: dict[str, Any] = {}


def _emit(name: str, kind: type, value: float, labels: dict[str, str], op: str) -> None:
    """Create-on-first-use + emit, guarded.

    A metric is created once per `name` with the label set from its FIRST
    use; a later call with a different label set (or labels-then-none, or
    none-then-labels) raises inside prometheus_client. We swallow+log that
    here — a metric bookkeeping mistake must never crash an otherwise
    healthy handler.
    """
    try:
        metric = _metrics.get(name)
        if metric is None:
            metric = kind(name, name, labelnames=list(labels.keys()), registry=REGISTRY)
            _metrics[name] = metric
        target = metric.labels(**labels) if labels else metric
        getattr(target, op)(value)
    except Exception:  # noqa: BLE001 — metrics must never break the caller
        logger.warning(
            "metric_emit_failed",
            extra={"event": "metric_emit_failed", "metric": name},
            exc_info=True,
        )


def record(name: str, value: float, labels: dict[str, str]) -> None:
    """Record a counter increment. Auto-creates a Counter on first use."""
    _emit(name, Counter, value, labels, "inc")


def gauge(name: str, value: float, labels: dict[str, str]) -> None:
    _emit(name, Gauge, value, labels, "set")


def observe(name: str, value: float, labels: dict[str, str]) -> None:
    _emit(name, Histogram, value, labels, "observe")
