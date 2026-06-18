"""Tests for the Redis-completeness audit's settings validators.

  1. `_redis_required_in_non_dev` — ROUTER_VALKEY_URL is mandatory
     when deployment_env in {staging, prod}. Without it, JWT
     revocation and the admit-rate quota silently fall back to
     per-process state — correct for single-worker dev, a security
     and throughput foot-gun across multiple replicas.

  2. `_quota_admit_rate_burst_paired` — for each level, both rate
     AND burst must be set, OR both must be None. Half-set
     configurations either crash with divide-by-zero or
     misconfigure the bucket silently.
"""

from __future__ import annotations

import pytest


def _base_settings_kwargs() -> dict:
    return dict(
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
        # The `_metrics_token_required_in_non_dev` validator fires
        # before `_redis_required_in_non_dev`. Stub it out so tests
        # in this file only exercise the redis validator they
        # intend to test.
        metrics_token="m" * 32,
    )


# ===========================================================================
# Validator 1: ROUTER_VALKEY_URL required in non-dev
# ===========================================================================


def test_dev_without_redis_is_ok(monkeypatch, tmp_path) -> None:
    """The single-worker dev case: no Redis configured, no error.
    Backwards-compatible — operators relying on the silent
    revocation no-op for local development keep working."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_VALKEY_URL", raising=False)
    monkeypatch.delenv("ROUTER_DEPLOYMENT_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    cfg = Settings(  # type: ignore[arg-type]
        **_base_settings_kwargs(),
        deployment_env="dev",
    )
    assert cfg.valkey_url is None
    assert cfg.deployment_env == "dev"


def test_staging_without_redis_rejected(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_VALKEY_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            deployment_env="staging",
        )
    msg = str(excinfo.value).lower()
    assert "valkey_url" in msg or "redis" in msg
    assert "staging" in msg or "deployment_env" in msg


def test_prod_without_redis_rejected(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_VALKEY_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            deployment_env="prod",
        )
    msg = str(excinfo.value).lower()
    assert "valkey_url" in msg or "redis" in msg


def test_prod_with_redis_is_ok(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_VALKEY_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    cfg = Settings(  # type: ignore[arg-type]
        **_base_settings_kwargs(),
        deployment_env="prod",
        valkey_url="redis://example.com:6379/0",
    )
    assert cfg.valkey_url == "redis://example.com:6379/0"


def test_validator_error_message_actionable(monkeypatch, tmp_path) -> None:
    """Pin the ergonomics of the failure message — operators have
    to be able to fix this without reading the validator source."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_VALKEY_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(), deployment_env="prod",
        )
    msg = str(excinfo.value)
    # Names the env var, the offending value, and the way out.
    assert "ROUTER_VALKEY_URL" in msg
    assert "prod" in msg
    # Mentions the security implication so the operator understands
    # WHY the validator exists.
    assert "revocation" in msg.lower() or "quota" in msg.lower()


# ===========================================================================
# Validator 2: rate + burst must be paired
# ===========================================================================


def test_rate_set_with_burst_none_rejected(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            quota_admit_rate_per_s={"tier1": 5.0},
            quota_admit_burst={"tier1": None},
        )
    msg = str(excinfo.value).lower()
    assert "tier1" in msg
    # Mentions both knobs so the operator sees what's mismatched.
    assert "rate" in msg and "burst" in msg


def test_burst_set_with_rate_none_rejected(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            quota_admit_rate_per_s={"tier1": None},
            quota_admit_burst={"tier1": 10},
        )
    assert "tier1" in str(excinfo.value).lower()


def test_both_none_is_ok(monkeypatch, tmp_path) -> None:
    """`None` on both means 'no cap for this level' — the standard
    way to disable quota for `admin` / `service`."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    cfg = Settings(  # type: ignore[arg-type]
        **_base_settings_kwargs(),
        quota_admit_rate_per_s={"admin": None, "tier1": 5.0},
        quota_admit_burst={"admin": None, "tier1": 10},
    )
    assert cfg.quota_admit_rate_per_s["admin"] is None
    assert cfg.quota_admit_burst["admin"] is None
    assert cfg.quota_admit_rate_per_s["tier1"] == 5.0


def test_negative_rate_rejected(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            quota_admit_rate_per_s={"tier1": -1.0},
            quota_admit_burst={"tier1": 10},
        )


def test_zero_rate_rejected(monkeypatch, tmp_path) -> None:
    """Zero rate would never refill — must be either a positive
    rate or `None` (no cap). Catches a typo where the operator
    meant `None` but typed `0`."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            quota_admit_rate_per_s={"tier1": 0.0},
            quota_admit_burst={"tier1": 10},
        )


def test_default_quota_table_passes_validation(monkeypatch, tmp_path) -> None:
    """The shipped defaults must obviously satisfy the validator
    they ship with — pin so a future default tweak that breaks the
    pairing is caught at unit time."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    cfg = Settings(**_base_settings_kwargs())  # type: ignore[arg-type]
    # Spot-check a few rows.
    assert cfg.quota_admit_rate_per_s["admin"] is None
    assert cfg.quota_admit_burst["admin"] is None
    assert cfg.quota_admit_rate_per_s["tier1"] == 20.0
    assert cfg.quota_admit_burst["tier1"] == 40
