"""R11 CRIT: `/readyz` must report NOT ready when a required Redis
is degraded (boot-tolerant start with `state.redis is None`).

Emergent interaction between two merged changes:
  * boot is intentionally Redis-tolerant (PR #205) — a configured-
    but-unreachable Redis at startup degrades instead of
    crashlooping (`state.redis = None`).
  * `is_jti_revoked` returns `False` when `state.redis is None`
    (fail-open), and the cross-worker rate caps fall back to
    per-process.

Before this fix `/readyz` *skipped* its Redis check when
`state.redis is None` and returned 200 — so a fresh staging/prod
deploy with Redis down came up "ready" and silently served with
JWT revocation defeated fleet-wide. The gate makes a genuinely-
down *required* Redis an orchestrator-level fail-fast (pod never
marked ready) WITHOUT crashlooping the process (a restart into a
healthy Redis recovers it). Dev is unaffected.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def _request(*, deployment_env: str, valkey_url: str | None, redis: object) -> MagicMock:
    """Build a fake Request whose app.state.bp has the settings /
    redis / db_pool the readiness probe reads."""
    state = MagicMock()
    state.settings.deployment_env = deployment_env
    state.settings.valkey_url = valkey_url
    state.redis = redis

    # Working DB pool (acquire() → async-CM → conn.execute).
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.db_pool = pool

    req = MagicMock()
    req.app.state.bp = state
    return req


def _call(req: MagicMock):  # type: ignore[no-untyped-def]
    from bp_router.api import health

    return asyncio.run(health.readiness(req))


# ---------------------------------------------------------------------------
# Behavioural
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_degraded_redis_in_non_dev_is_not_ready(env: str) -> None:
    """The headline fix: Redis configured but `state.redis is None`
    in staging/prod → 503, so the orchestrator won't route traffic
    to a pod whose revocation is failing open."""
    resp = _call(_request(deployment_env=env, valkey_url="redis://x", redis=None))
    assert resp.status_code == 503
    assert b"degraded" in resp.body or b"redis" in resp.body.lower()


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_healthy_redis_in_non_dev_is_ready(env: str) -> None:
    """Redis configured AND connected → normal readiness (200)."""
    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    resp = _call(_request(deployment_env=env, valkey_url="redis://x", redis=redis))
    assert resp.status_code == 200


def test_dev_without_redis_stays_ready() -> None:
    """Dev single-worker (no Redis configured) is unaffected — the
    gate is non-dev only and also requires `valkey_url` set."""
    resp = _call(_request(deployment_env="dev", valkey_url=None, redis=None))
    assert resp.status_code == 200


def test_dev_with_configured_redis_down_is_not_gated() -> None:
    """Even a misconfigured dev (valkey_url set, redis None) is NOT
    gated — dev revocation is best-effort by design; only the DB
    check applies and it passes here."""
    resp = _call(_request(deployment_env="dev", valkey_url="redis://x", redis=None))
    assert resp.status_code == 200


def test_non_dev_degraded_gate_precedes_db_check() -> None:
    """The gate must short-circuit BEFORE the DB probe — a pod with
    a degraded required Redis is not-ready regardless of DB health
    (and we don't want to depend on DB I/O to surface a security
    regression)."""
    req = _request(deployment_env="prod", valkey_url="redis://x", redis=None)
    # Make the DB pool explode — if the gate didn't precede it this
    # would raise / 503-for-the-wrong-reason.
    req.app.state.bp.db_pool.acquire.side_effect = RuntimeError("db down")
    resp = _call(req)
    assert resp.status_code == 503
    assert b"degraded" in resp.body


# ---------------------------------------------------------------------------
# Source pins
# ---------------------------------------------------------------------------


def test_readiness_gate_checks_env_redis_url_and_state_redis() -> None:
    """AST pin: the gate conjunction is (deployment_env in
    {staging,prod}) AND (valkey_url is not None) AND
    (state.redis is None). Guards a refactor silently dropping a
    conjunct (e.g. forgetting `valkey_url is not None` would 503 a
    legitimately Redis-less dev)."""
    from bp_router.api import health

    src = inspect.getsource(health.readiness)
    assert 'deployment_env in ("staging", "prod")' in src
    assert "settings.valkey_url is not None" in src
    assert "state.redis is None" in src
    # The gate returns a 503 Response.
    tree = ast.parse(src.lstrip())
    assert any(
        isinstance(n, ast.Constant) and n.value == 503
        for n in ast.walk(tree)
    )


def test_readiness_still_does_not_author_redis_health() -> None:
    """Companion to test_review_redis_degradation: the probe must
    NOT call `.set(` (it must not author `redis_health` — the
    subsystem owns that gauge). Re-pinned here so this PR can't
    regress it."""
    from bp_router.api import health

    src = inspect.getsource(health.readiness)
    assert ".set(" not in src
