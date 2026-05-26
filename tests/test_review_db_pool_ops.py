"""R10 MED: DB-pool operability (two batched).

MED-7  `db_pool_max_size` defaults to 10 — a fine *dev* default but
   the most likely first-week production bottleneck (a delegation
   ack-storm, a fleet reconnect opening a pooled conn per agent, or
   chatty Progress fan-out can exhaust 10 and stall every other
   router DB op). A new model-validator WARNS (never raises — the
   right ceiling is workload-specific and a hard failure would be
   user-hostile) when a staging/prod deployment never bumped it
   past the dev default.

MED-8  Pool exhaustion was invisible until requests started timing
   out. `router_db_pool_connections{state}` is now sampled once per
   timeout-sweep tick (~5s) so saturation is alertable
   (`in_use / max → 1`) before it becomes an outage.
"""
from __future__ import annotations

import ast
import inspect
import logging
import textwrap

import pytest


def _base_settings_kwargs() -> dict:
    # Mirrors tests/test_settings_redis_quota_validators.py: the
    # other non-dev validators (`_metrics_token_required_in_non_dev`,
    # `_redis_required_in_non_dev`) fire first; satisfy them so this
    # file only exercises the db-pool advisory.
    return dict(
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
        metrics_token="m" * 32,
        redis_url="redis://localhost:6379/0",
    )


# ===========================================================================
# MED-7: staging/prod under-provisioning advisory
# ===========================================================================


def test_dev_default_constant_matches_field_default() -> None:
    """The advisory threshold and the field default must stay in
    lockstep — otherwise the warn fires (or doesn't) for the wrong
    value after someone retunes the default."""
    from bp_router.settings import _DB_POOL_DEV_DEFAULT, Settings

    assert Settings.model_fields["db_pool_max_size"].default == _DB_POOL_DEV_DEFAULT


def test_staging_small_pool_warns_but_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with caplog.at_level(logging.WARNING, logger="bp_router.settings"):
        cfg = Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            deployment_env="staging",
            db_pool_max_size=10,
        )

    # WARN, not raise — construction succeeds.
    assert cfg.db_pool_max_size == 10
    recs = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "db_pool_small_non_dev"
    ]
    assert len(recs) == 1
    assert recs[0].db_pool_max_size == 10
    assert recs[0].deployment_env == "staging"


def test_prod_small_pool_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with caplog.at_level(logging.WARNING, logger="bp_router.settings"):
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            deployment_env="prod",
            db_pool_max_size=5,
        )
    assert any(
        getattr(r, "event", None) == "db_pool_small_non_dev"
        for r in caplog.records
    )


def test_dev_small_pool_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dev is the expected home of the small pool — no noise."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_REDIS_URL", raising=False)
    monkeypatch.delenv("ROUTER_DEPLOYMENT_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with caplog.at_level(logging.WARNING, logger="bp_router.settings"):
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            deployment_env="dev",
            db_pool_max_size=10,
        )
    assert not any(
        getattr(r, "event", None) == "db_pool_small_non_dev"
        for r in caplog.records
    )


def test_staging_bumped_pool_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator who actually provisioned the pool gets no
    advisory — the signal must not cry wolf."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    with caplog.at_level(logging.WARNING, logger="bp_router.settings"):
        Settings(  # type: ignore[arg-type]
            **_base_settings_kwargs(),
            deployment_env="staging",
            db_pool_max_size=50,
        )
    assert not any(
        getattr(r, "event", None) == "db_pool_small_non_dev"
        for r in caplog.records
    )


# ===========================================================================
# MED-8: db pool saturation gauge
# ===========================================================================


def test_db_pool_gauge_exists_with_bounded_state_label() -> None:
    from bp_router.observability import metrics

    g = metrics.db_pool_connections
    # Bounded label set — no per-connection cardinality.
    g.labels(state="in_use")
    g.labels(state="idle")
    g.labels(state="max")
    assert "router_db_pool_connections" in g._name


def test_sampler_sets_in_use_idle_max() -> None:
    from bp_router.observability import metrics
    from bp_router.tasks import _sample_db_pool_metrics

    class _Pool:
        def get_size(self) -> int:
            return 9

        def get_idle_size(self) -> int:
            return 4

        def get_max_size(self) -> int:
            return 30

    _sample_db_pool_metrics(_Pool())
    g = metrics.db_pool_connections
    assert g.labels(state="in_use")._value.get() == 5  # 9 - 4
    assert g.labels(state="idle")._value.get() == 4
    assert g.labels(state="max")._value.get() == 30


def test_sampler_silently_skips_pool_without_introspection() -> None:
    """A fake/legacy pool that lacks `get_size` must not break the
    deadline sweep — a metric is never worth stalling enforcement."""
    from bp_router.tasks import _sample_db_pool_metrics

    _sample_db_pool_metrics(object())  # must not raise

    class _Broken:
        def get_size(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("pool closed")

    _sample_db_pool_metrics(_Broken())  # must not raise


def test_sweep_loop_samples_pool_each_tick_before_sweep() -> None:
    """Pin the wiring: `timeout_sweep_loop` calls
    `_sample_db_pool_metrics(state.db_pool)` every tick, and BEFORE
    `_sweep_once` so the gauge reflects ambient pressure rather than
    the sweep's own transient checkout."""
    from bp_router import tasks

    src = textwrap.dedent(inspect.getsource(tasks.timeout_sweep_loop))
    tree = ast.parse(src).body[0]

    sample_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_sample_db_pool_metrics"
    ]
    assert len(sample_calls) == 1, "sampler must be called once per tick"

    # Lexical order inside the loop body: sample before _sweep_once.
    sample_line = sample_calls[0].lineno
    sweep_calls = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_sweep_once"
    ]
    assert sweep_calls and sample_line < min(sweep_calls), (
        "sampler must run before _sweep_once acquires its connection"
    )
