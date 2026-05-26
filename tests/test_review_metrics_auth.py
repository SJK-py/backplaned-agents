"""`/metrics` is bearer-gated when ROUTER_METRICS_TOKEN is set.

Backstory: Prometheus exposition leaks the live agent ID list,
per-endpoint request rates, queue depths, and the error taxonomy.
Previously `/metrics` was wired open. The fix gates it behind a
static bearer (`ROUTER_METRICS_TOKEN`), with the settings model
requiring the token in staging / prod so an upgrade can't silently
leave the recon surface exposed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_request(
    *,
    metrics_token: str | None,
    auth_header: str | None,
) -> MagicMock:
    request = MagicMock()
    state = MagicMock()
    settings = MagicMock()
    if metrics_token is None:
        settings.metrics_token = None
    else:
        secret = MagicMock()
        secret.get_secret_value.return_value = metrics_token
        settings.metrics_token = secret
    state.settings = settings
    request.app.state.bp = state
    headers: dict[str, str] = {}
    if auth_header is not None:
        headers["authorization"] = auth_header
    request.headers = headers
    return request


def test_metrics_open_when_no_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev default: when `metrics_token` is None, /metrics returns 200
    without an Authorization header. Preserves single-worker / loopback
    Prometheus workflows that don't bother with auth."""
    pytest.importorskip("fastapi")
    import asyncio

    from bp_router.api import health
    from bp_router.observability import metrics as metrics_mod
    monkeypatch.setattr(
        metrics_mod, "render_exposition", lambda: "bp_router_up 1\n"
    )

    request = _make_request(metrics_token=None, auth_header=None)
    response = asyncio.run(health.metrics(request))
    assert response.status_code == 200
    assert b"bp_router_up 1" in response.body


def test_metrics_rejects_missing_bearer_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token configured + no Authorization header → 401."""
    pytest.importorskip("fastapi")
    import asyncio

    from bp_router.api import health
    from bp_router.observability import metrics as metrics_mod
    monkeypatch.setattr(
        metrics_mod, "render_exposition", lambda: "bp_router_up 1\n"
    )

    request = _make_request(metrics_token="s3cret", auth_header=None)
    response = asyncio.run(health.metrics(request))
    assert response.status_code == 401


def test_metrics_rejects_wrong_bearer_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token configured + wrong Authorization header → 401.

    Comparison must NOT leak via short-circuit equality. We exercise
    a near-match (matching prefix, differing suffix) to lock in the
    `hmac.compare_digest` path."""
    pytest.importorskip("fastapi")
    import asyncio

    from bp_router.api import health
    from bp_router.observability import metrics as metrics_mod
    monkeypatch.setattr(
        metrics_mod, "render_exposition", lambda: "bp_router_up 1\n"
    )

    request = _make_request(
        metrics_token="s3cret-correct",
        auth_header="Bearer s3cret-wrong",
    )
    response = asyncio.run(health.metrics(request))
    assert response.status_code == 401


def test_metrics_accepts_matching_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token configured + matching Bearer → 200 with exposition body."""
    pytest.importorskip("fastapi")
    import asyncio

    from bp_router.api import health
    from bp_router.observability import metrics as metrics_mod
    monkeypatch.setattr(
        metrics_mod, "render_exposition", lambda: "bp_router_up 1\n"
    )

    request = _make_request(
        metrics_token="s3cret",
        auth_header="Bearer s3cret",
    )
    response = asyncio.run(health.metrics(request))
    assert response.status_code == 200
    assert b"bp_router_up 1" in response.body


def test_settings_rejects_unconfigured_metrics_token_in_prod() -> None:
    """The Settings validator refuses to start with deployment_env=prod
    and no ROUTER_METRICS_TOKEN. Pins the upgrade-safety: a deployment
    promoting to prod without setting the token fails loudly at
    startup rather than silently exposing /metrics."""
    pytest.importorskip("pydantic_settings")
    from pydantic import ValidationError

    from bp_router.settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            db_url="postgresql://x:x@localhost/x",
            public_url="https://example.com",
            jwt_secret="x" * 32,
            redis_url="redis://localhost:6379/0",
            deployment_env="prod",
            metrics_token=None,
            admin_session_secret="y" * 32,
        )
    assert "METRICS_TOKEN" in str(exc_info.value) or "metrics_token" in str(
        exc_info.value
    )


def test_settings_rejects_unconfigured_metrics_token_in_staging() -> None:
    """Same upgrade-safety guard applies to staging."""
    pytest.importorskip("pydantic_settings")
    from pydantic import ValidationError

    from bp_router.settings import Settings

    with pytest.raises(ValidationError):
        Settings(
            db_url="postgresql://x:x@localhost/x",
            public_url="https://example.com",
            jwt_secret="x" * 32,
            redis_url="redis://localhost:6379/0",
            deployment_env="staging",
            metrics_token=None,
            admin_session_secret="y" * 32,
        )


def test_settings_accepts_metrics_token_in_prod() -> None:
    """Sanity: set the token and prod boots cleanly."""
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    s = Settings(
        db_url="postgresql://x:x@localhost/x",
        public_url="https://example.com",
        jwt_secret="x" * 32,
        redis_url="redis://localhost:6379/0",
        deployment_env="prod",
        metrics_token="z" * 32,
        admin_session_secret="y" * 32,
    )
    assert s.metrics_token is not None
