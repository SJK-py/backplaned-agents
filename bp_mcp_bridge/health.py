"""Restart backoff and give-up for bridged agents.

The supervisor's reconcile loop is a fixed-interval respawner: a bridge task
that dies is evicted from `_active`, and the next pass (30s later) starts it
again. For a transient failure that is exactly right. For a permanent one —
a retired endpoint, a revoked token, a URL with a typo — it is an infinite
loop: a full connect attempt against someone else's server every 30 seconds,
a stack trace in the log every 30 seconds, for as long as the process lives.
Nothing in it ever escalated its waits and nothing ever stopped.

This is the piece that stops. Per bridged agent it tracks consecutive
failures and refuses to start again until a backoff has elapsed; past
`max_failures` it stops entirely and says so once.

**Giving up is not permanent, and does not need a restart.** Two things
that already exist in the admin UI clear it, because both mean an operator
has changed something and wants another go:

  * **editing the row** — the config signature changes;
  * **clicking Reconnect** — a fresh invitation token is minted onto the row.

Both are observed in the reconcile diff, so recovery costs the operator
nothing beyond the action they were going to take anyway.

**Why not mark the row failed in the database instead?** Because that makes
recovery from a two-hour upstream outage require a human, and upstreams
recover on their own. The gate reopens by itself for as long as anyone is
willing to keep trying; what it will not do is keep trying forever in
silence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bp_mcp_bridge import metrics

logger = logging.getLogger(__name__)

# Backoff schedule. The floor is one poll interval — below that the gate
# cannot express anything, since the reconcile only runs that often. Doubling
# from 30s reaches the 15-minute ceiling after about half an hour of trying.
_BACKOFF_BASE_S = 30.0
_BACKOFF_MAX_S = 900.0
# Consecutive failures before the gate closes for good. Eight spans roughly
# 30 minutes: long enough that a rolling deploy or a provider incident on the
# far end recovers on its own, short enough that a genuine misconfiguration
# stops being retried the same working day it was introduced.
_MAX_FAILURES = 8
# A run this long is treated as healthy: the bridge connected, listed tools
# and settled. Shorter than this and an exit is a failed start, however it
# ended. Well above a connect handshake (~1s) and well below any sane
# operator's patience.
_HEALTHY_RUN_S = 60.0


@dataclass
class _Entry:
    failures: int = 0
    not_before: float = 0.0
    given_up: bool = False
    # What the row looked like when we last saw it. A change to either means
    # the operator did something, so the gate reopens.
    signature: Any = None
    invitation: str | None = None
    last_error: str = ""
    started_at: float = field(default=0.0)


class HealthGate:
    """Per-agent restart policy. One instance per supervisor, shared by every
    bridged kind — MCP servers and the non-MCP agents fail the same way and
    deserve the same treatment."""

    def __init__(
        self,
        *,
        backoff_base_s: float = _BACKOFF_BASE_S,
        backoff_max_s: float = _BACKOFF_MAX_S,
        max_failures: int = _MAX_FAILURES,
        healthy_run_s: float = _HEALTHY_RUN_S,
        clock: Any = time.monotonic,
    ) -> None:
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._max_failures = max_failures
        self._healthy_run_s = healthy_run_s
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    # -- reconcile-time queries --------------------------------------------

    def observe(self, key: str, *, signature: Any, invitation: str | None) -> None:
        """Note the row's current shape. An edit (new signature) or a
        Reconnect (new invitation) clears any backoff or give-up, because
        both are an operator asking for another attempt."""
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(signature=signature, invitation=invitation)
            return
        if entry.signature != signature or entry.invitation != invitation:
            if entry.failures or entry.given_up:
                logger.info(
                    "agent_health_reset_by_operator",
                    extra={
                        "event": "agent_health_reset_by_operator",
                        "bp.bridged_agent_id": key,
                        "prior_failures": entry.failures,
                    },
                )
            entry.signature = signature
            entry.invitation = invitation
            self._clear(key, entry)

    def may_start(self, key: str) -> bool:
        """Whether the supervisor should spawn this agent's bridge now."""
        entry = self._entries.get(key)
        if entry is None:
            return True
        if entry.given_up:
            return False
        return self._clock() >= entry.not_before

    def record_start(self, key: str) -> None:
        self._entries.setdefault(key, _Entry()).started_at = self._clock()

    # -- exit accounting ----------------------------------------------------

    def record_exit(self, key: str, *, error: str | None) -> None:
        """Account for a bridge task that ended.

        A run that lasted `healthy_run_s` resets the counter regardless of how
        it ended: it did its job, and whatever killed it later is a fresh
        problem, not evidence of a persistent one. Anything shorter is a
        failed start — including a CLEAN early return, which is how a bridge
        with neither credentials nor an invitation exits, and which would
        otherwise spin at the poll interval forever."""
        entry = self._entries.setdefault(key, _Entry())
        ran_for = self._clock() - entry.started_at if entry.started_at else 0.0
        if ran_for >= self._healthy_run_s:
            self._clear(key, entry)
            return

        entry.failures += 1
        entry.last_error = error or "exited without connecting"
        if entry.failures >= self._max_failures:
            entry.given_up = True
            logger.error(
                "agent_bridge_gave_up",
                extra={
                    "event": "agent_bridge_gave_up",
                    "bp.bridged_agent_id": key,
                    "failures": entry.failures,
                    "error": entry.last_error,
                    "recovery": (
                        "edit the row or click Reconnect in the admin UI to "
                        "retry"
                    ),
                },
            )
            metrics.bridge_given_up.labels(agent_id=key).set(1)
            return

        delay = min(
            self._backoff_base_s * (2 ** (entry.failures - 1)),
            self._backoff_max_s,
        )
        entry.not_before = self._clock() + delay
        logger.warning(
            "agent_bridge_restart_deferred",
            extra={
                "event": "agent_bridge_restart_deferred",
                "bp.bridged_agent_id": key,
                "failures": entry.failures,
                "retry_in_s": delay,
                "error": entry.last_error,
            },
        )

    def keys_for(self, kind: str) -> list[str]:
        """Tracked keys belonging to one kind, so the supervisor can drop
        state for rows that left the table."""
        prefix = f"{kind}:"
        return [k for k in self._entries if k.startswith(prefix)]

    def forget(self, key: str) -> None:
        """Drop all state for an agent that left the desired set."""
        if self._entries.pop(key, None) is not None:
            metrics.bridge_given_up.labels(agent_id=key).set(0)

    # -- introspection (tests, future admin surface) ------------------------

    def status(self, key: str) -> dict[str, Any]:
        entry = self._entries.get(key)
        if entry is None:
            return {"failures": 0, "given_up": False, "last_error": ""}
        return {
            "failures": entry.failures,
            "given_up": entry.given_up,
            "last_error": entry.last_error,
        }

    def _clear(self, key: str, entry: _Entry) -> None:
        was_given_up = entry.given_up
        entry.failures = 0
        entry.not_before = 0.0
        entry.given_up = False
        entry.last_error = ""
        if was_given_up:
            metrics.bridge_given_up.labels(agent_id=key).set(0)
