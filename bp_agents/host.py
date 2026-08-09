"""bp_agents.host — run a GROUP of suite agents in one process.

    python -m bp_agents.host --group suite-core
    python -m bp_agents.host --agents orchestrator,research,memory

Implements `docs/design/deployment-agent-host.md` §2. The deployment unit
should be an **isolation boundary, not an agent**: twelve agents that trust
each other equally, ship in one image at one tag, and are released together
do not need twelve containers, twelve identities provisioned out-of-band,
and twelve Python interpreters.

The saving is measured, not theoretical — the heavy dependencies (`lancedb`,
`fastapi`, `markitdown`, `asyncpg`) load once per *process*, not once per
*agent*:

    one suite agent imported                 ~40 MB RSS
    eleven suite agents in ONE process       ~96 MB RSS

Each agent keeps its own identity, its own WebSocket, and its own place in
the ACL. The host is a process, not a merged principal — nothing about the
security model changes.

**What must NOT be hosted.** `sandbox` runs as root with `CAP_SETUID` (so it
can drop each user's bash to a distinct uid), `no-new-privileges`, a
root-owned state dir and no database network. Those capabilities and that
network position ARE the isolation; co-hosting anything with it would hand
that agent the same. `mcp_bridge` is the same argument in miniature. Both
stay in their own containers, and `GROUPS` below does not list them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bp_sdk.agent import Agent

logger = logging.getLogger(__name__)


# Groups are ISOLATION boundaries, not convenience bundles (design §2.1).
#
#   suite-core — equal trust, no privileged requirements, same release
#   channels   — the user-facing edge: the only agents on the `edge` network
#                and the only ones whose restart a user notices
#
# `sandbox` and `mcp_bridge` are deliberately absent: see the module
# docstring.
GROUPS: dict[str, tuple[str, ...]] = {
    "suite-core": (
        "orchestrator",
        "deep_reasoning",
        "research",
        "computer_use",
        "knowledge_base",
        "memory",
        "history_summarizer",
        "md_converter",
        "config",
    ),
    "channels": ("chatbot", "webapp"),
}

# Agents that must never share a process, guarded here as well as in the
# compose file — a group typo should fail loudly rather than quietly
# co-hosting the untrusted sandbox.
NEVER_HOSTED = frozenset({"sandbox", "mcp_bridge"})

# Restart backoff for a crashed agent, in seconds. Grows to the cap so a
# reliably-broken agent doesn't spin, and resets once it has run cleanly for
# `_HEALTHY_AFTER_S`.
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0
_HEALTHY_AFTER_S = 120.0


def resolve_agents(group: str | None, agents: str | None) -> list[str]:
    """The roster this host runs. Rejects the never-hosted agents rather
    than silently dropping them — a config that names `sandbox` is wrong in
    a way the operator needs to see."""
    if group and agents:
        raise SystemExit("pass --group or --agents, not both")
    if group:
        if group not in GROUPS:
            raise SystemExit(
                f"unknown group {group!r}; known: {', '.join(sorted(GROUPS))}"
            )
        names = list(GROUPS[group])
    elif agents:
        names = [a.strip() for a in agents.split(",") if a.strip()]
    else:
        raise SystemExit("one of --group / --agents is required")

    forbidden = NEVER_HOSTED.intersection(names)
    if forbidden:
        raise SystemExit(
            f"{', '.join(sorted(forbidden))} must run in its own container — "
            "its capabilities and network position are the isolation "
            "(see docs/design/deployment-agent-host.md §2.1)"
        )
    if not names:
        raise SystemExit("no agents to run")
    return names


def load_agent(name: str) -> Agent:
    """Import one suite agent module and return its `Agent`.

    Mirrors the per-agent entrypoint (`python -m bp_agents.agents.<name>`),
    which each module already exposes as a module-level `agent`."""
    import importlib  # noqa: PLC0415

    module = importlib.import_module(f"bp_agents.agents.{name}.agent")
    agent = getattr(module, "agent", None)
    if agent is None:  # pragma: no cover - defensive
        raise SystemExit(f"bp_agents.agents.{name}.agent exposes no `agent`")
    return agent


class _Supervised:
    """One agent's supervision state: run it, restart it on failure, and
    stop restarting only when the whole host is going down.

    This is the cost of hosting (design §2.2). It is also strictly better
    than the status quo for the agent's neighbours: today an unhandled
    exception restarts a container and drops every in-flight task in it,
    where here one agent restarts while the rest keep their sockets."""

    def __init__(self, name: str, agent: Agent) -> None:
        self.name = name
        self.agent = agent
        self.restarts = 0
        self.permanently_failed = False

    async def run(self, stopping: asyncio.Event) -> None:
        from bp_sdk.errors import TransportPermanentlyFailed  # noqa: PLC0415

        backoff = _BACKOFF_START_S
        loop = asyncio.get_running_loop()
        while not stopping.is_set():
            started = loop.time()
            try:
                await self.agent.run_async()
                # A CLEAN return means the agent shut itself down — the SDK
                # reconnects transient transport failures internally and
                # raises `TransportPermanentlyFailed` for the rest, so
                # returning normally is a deliberate stop. Restarting here
                # would spin an agent that decided it was finished.
                logger.info(
                    "host_agent_exited",
                    extra={
                        "event": "host_agent_exited",
                        "bp.agent_id": self.name,
                    },
                )
                return
            except asyncio.CancelledError:
                raise
            except TransportPermanentlyFailed:
                # The router is unreachable for this agent in a way the SDK
                # considers final. Don't spin — mark it and let the host
                # decide whether ALL agents are down (§2.2).
                self.permanently_failed = True
                logger.error(
                    "host_agent_transport_permanently_failed",
                    extra={
                        "event": "host_agent_transport_permanently_failed",
                        "bp.agent_id": self.name,
                    },
                )
                return
            except Exception:  # noqa: BLE001 - supervision is the point
                logger.exception(
                    "host_agent_crashed",
                    extra={"event": "host_agent_crashed", "bp.agent_id": self.name},
                )
            if stopping.is_set():
                return
            # A long healthy run earns a fresh backoff; a crash-loop keeps
            # the accumulated one.
            if loop.time() - started >= _HEALTHY_AFTER_S:
                backoff = _BACKOFF_START_S
            self.restarts += 1
            logger.warning(
                "host_agent_restarting",
                extra={
                    "event": "host_agent_restarting",
                    "bp.agent_id": self.name,
                    "restarts": self.restarts,
                    "backoff_s": backoff,
                },
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)


async def run_host(names: list[str]) -> int:
    """Run every named agent until SIGTERM/SIGINT. Returns the process exit
    code: non-zero when every agent ended permanently failed, so a
    supervisor (compose `restart:`, systemd, k8s) still sees a dead host
    rather than a healthy-looking container full of dead agents."""
    supervised = [_Supervised(name, load_agent(name)) for name in names]
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        # SIGTERM reaches the whole process; each agent's own SDK shutdown
        # then drains its in-flight tasks within the container's grace
        # period, exactly as it does standalone.
        if not stopping.is_set():
            logger.info("host_stopping", extra={"event": "host_stopping"})
            stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _request_stop)

    logger.info(
        "host_starting",
        extra={"event": "host_starting", "agents": names, "count": len(names)},
    )
    tasks = [
        asyncio.create_task(s.run(stopping), name=f"agent:{s.name}")
        for s in supervised
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if all(s.permanently_failed for s in supervised):
        logger.error(
            "host_all_agents_failed", extra={"event": "host_all_agents_failed"}
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bp_agents.host",
        description="Run a group of suite agents in one process.",
    )
    parser.add_argument("--group", default=os.environ.get("SUITE_AGENT_GROUP"))
    parser.add_argument("--agents", default=os.environ.get("SUITE_AGENTS"))
    args = parser.parse_args(argv)

    names = resolve_agents(args.group, args.agents)
    try:
        return asyncio.run(run_host(names))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
