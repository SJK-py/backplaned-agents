"""Second-pass ops/deploy hardening (PR C).

B1 — graceful shutdown: the router gets a bounded uvicorn
      `timeout_graceful_shutdown` (settings.shutdown_grace_s) and the prod
      compose gives router + suite agents a `stop_grace_period` that exceeds
      it, so the lifespan/SDK drain isn't SIGKILLed mid-flight on every
      restart.
B2 — the `bp_suite` Postgres role is created NOLOGIN (no committed
      'change-me-suite' login password — a dormant known-credential account).
M2 — the third-party `rustfs` (file store) and `searxng` images are pinned to
      immutable tags, not `:latest`.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_COMPOSE = (_REPO / "docker-compose.prod.yml").read_text()
_INIT_SQL = (_REPO / "deploy/postgres-init/01-create-suite-db.sql").read_text()


# --- B1: uvicorn graceful timeout -----------------------------------------


def test_shutdown_grace_setting_default_and_bound() -> None:
    from bp_router.settings import Settings

    field = Settings.model_fields["shutdown_grace_s"]
    assert field.default == 25.0
    # Negative grace is rejected (ge=0 constraint).
    meta = repr(field.metadata)
    assert "Ge(ge=0" in meta or "ge=0" in meta


def test_main_passes_graceful_shutdown_timeout() -> None:
    from bp_router import __main__ as entry

    src = inspect.getsource(entry.main)
    assert "timeout_graceful_shutdown" in src
    assert "settings.shutdown_grace_s" in src


# --- B1: compose stop_grace_period exceeds the uvicorn timeout -------------


def test_compose_router_and_agents_have_stop_grace_period() -> None:
    # Both the router service and the x-suite-agent anchor (inherited by the
    # webapp + every suite agent) declare a stop_grace_period.
    assert _COMPOSE.count("stop_grace_period: 30s") >= 2
    # And it exceeds the router's uvicorn graceful timeout (25s) so the drain
    # completes before SIGKILL.
    from bp_router.settings import Settings

    assert Settings.model_fields["shutdown_grace_s"].default < 30.0


# --- B2: suite role is NOLOGIN, no committed password ----------------------


def test_suite_role_is_nologin_without_committed_password() -> None:
    assert "CREATE ROLE bp_suite NOLOGIN;" in _INIT_SQL
    assert "change-me-suite" not in _INIT_SQL
    assert "LOGIN PASSWORD" not in _INIT_SQL
    # The database + ownership are still created for the future wiring.
    assert "CREATE DATABASE bp_suite OWNER bp_suite;" in _INIT_SQL


# --- M2: third-party images pinned, not :latest ---------------------------


@pytest.mark.parametrize("image_prefix", ["rustfs/rustfs:", "searxng/searxng:"])
def test_third_party_images_are_pinned(image_prefix: str) -> None:
    line = next(
        (ln.strip() for ln in _COMPOSE.splitlines()
         if image_prefix in ln and ln.strip().startswith("image:")),
        None,
    )
    assert line is not None, f"{image_prefix} image line not found"
    tag = line.split(image_prefix, 1)[1].strip()
    assert tag and tag != "latest", f"{image_prefix} must pin a real tag, got {tag!r}"
