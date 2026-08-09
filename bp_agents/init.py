"""bp_agents.init — the single boot one-shot.

    python -m bp_agents.init            # all steps, in order
    python -m bp_agents.init --step acl  # one step, for debugging

Implements `docs/design/deployment-agent-host.md` §4. Replaces three Compose
services — `migrate` (router schema), `suite-migrate` (suite schema),
`bootstrap` (invitations + ACL) — each of which was an ordering edge every
agent had to declare, and three separate places to look when a boot failed.

The steps are unchanged in substance; only their packaging is. They stay
individually runnable via `--step` precisely because collapsing services
must not cost debuggability.

**This holds the admin credential and the host must not.** The split is the
point: an admin-authenticated one-shot mints the roster token and applies the
ACL; the host then holds only a credential that can produce the agents it was
given (design §3).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

STEPS = ("router-schema", "suite-schema", "acl")


def _alembic(config: str) -> int:
    """Run one Alembic config to head. Subprocess rather than the Python API
    so the migration runs with exactly the environment (and DSN resolution)
    the standalone command has — a migration that only works in-process is a
    migration nobody can debug."""
    logger.info(
        "init_migrating", extra={"event": "init_migrating", "config": config}
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-c", config, "upgrade", "head"],
        check=False,
    )
    return proc.returncode


def step_router_schema() -> int:
    return _alembic(os.environ.get("ROUTER_ALEMBIC_INI", "alembic.ini"))


def step_suite_schema() -> int:
    return _alembic(os.environ.get("SUITE_ALEMBIC_INI", "alembic_suite.ini"))


def step_acl() -> int:
    """Register invitations + apply the suite ACL against a live router.

    Delegates to the existing bootstrap entrypoint, which already logs in as
    the admin, registers pre-supplied invitation tokens idempotently, and
    MERGES the ACL so admin-added rules survive each boot."""
    from bp_agents.bootstrap import _main  # noqa: PLC0415

    return asyncio.run(_main())


_RUNNERS = {
    "router-schema": step_router_schema,
    "suite-schema": step_suite_schema,
    "acl": step_acl,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bp_agents.init",
        description="Apply schemas and bootstrap the suite against a router.",
    )
    parser.add_argument(
        "--step",
        choices=STEPS,
        help="run ONE step instead of all three (debugging)",
    )
    args = parser.parse_args(argv)

    steps = (args.step,) if args.step else STEPS
    for name in steps:
        rc = _RUNNERS[name]()
        if rc != 0:
            # Stop at the first failure: the later steps assume the earlier
            # ones landed, and a half-migrated schema with a bootstrapped ACL
            # is harder to reason about than a clean stop.
            logger.error(
                "init_step_failed",
                extra={"event": "init_step_failed", "step": name, "rc": rc},
            )
            return rc
        logger.info("init_step_ok", extra={"event": "init_step_ok", "step": name})
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
