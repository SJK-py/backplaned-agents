"""`service_mint_refresh_token` is per-target rate-limited.

Backstory: F8 added the `serviced_by` model — a service principal
can mint refresh tokens for any user in its `serviced_by` list via
`POST /v1/admin/users/{id}/refresh-tokens`. The sibling
`mint_password_reset_token` endpoint has a per-target rate-limit
bucket (3 mints / hour by default) to defend against a compromised
service principal mass-minting against every user. The refresh
endpoint was missing that defence; a compromised principal (or a
runaway script) could mint unlimited 24-hour refresh tokens, each
independently revocable only by hash. These tests pin the fix.
"""

from __future__ import annotations

import inspect

import pytest


def test_handler_consumes_rate_limit_bucket_per_target() -> None:
    """Source pin: `service_mint_refresh_token` calls the shared
    `_enforce_per_target_mint_rate_limit` helper with the central
    `BUCKET_SERVICE_MINT_REFRESH_TOKEN` constant. The helper owns
    the 429 + Retry-After + audit shape."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.service_mint_refresh_token)
    assert "_enforce_per_target_mint_rate_limit(" in src
    assert "BUCKET_SERVICE_MINT_REFRESH_TOKEN" in src
    # And references the per-endpoint settings fields.
    assert "service_mint_refresh_token_rate_limit_per_target_per_s" in src
    assert "service_mint_refresh_token_rate_limit_per_target_burst" in src


def test_handler_audits_rate_limit_hit() -> None:
    """Source pin: on rate-limit hit the handler appends an audit
    event distinct from the 'service-minted' success event so
    operators can correlate denied attempts."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.service_mint_refresh_token)
    assert 'event="auth.refresh_token_service_mint_rate_limited"' in src


def test_handler_rate_limit_check_runs_before_token_mint() -> None:
    """The rate-limit check MUST run before the
    `_secrets.token_urlsafe` mint — otherwise a saturated bucket
    still produces a wasted token + audit row. Source pin via
    line ordering: the helper call appears before the token mint."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.service_mint_refresh_token)
    lines = src.splitlines()
    helper_line = next(
        (i for i, line in enumerate(lines)
         if "_enforce_per_target_mint_rate_limit(" in line),
        -1,
    )
    mint_line = next(
        (i for i, line in enumerate(lines)
         if "_secrets.token_urlsafe(32)" in line),
        -1,
    )
    assert helper_line >= 0 and mint_line >= 0
    assert helper_line < mint_line, (
        "Rate-limit consume must run BEFORE the token mint."
    )


def test_settings_exposes_per_target_rate_limit_fields() -> None:
    """`Settings` exposes per-target rate / burst fields with
    sensible defaults (≈12/h per target, burst 5)."""
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    s = Settings(
        db_url="postgresql://x:x@localhost/x",
        public_url="https://example.com",
        jwt_secret="x" * 32,
        admin_session_secret="y" * 32,
    )
    # Field exists.
    assert hasattr(s, "service_mint_refresh_token_rate_limit_per_target_per_s")
    assert hasattr(s, "service_mint_refresh_token_rate_limit_per_target_burst")
    # Defaults are tight enough to defend (rate is ≪ 1/s) but not
    # zero (would lock the endpoint out entirely).
    rate = s.service_mint_refresh_token_rate_limit_per_target_per_s
    assert rate > 0, "rate must be > 0 — otherwise the endpoint can't mint"
    assert rate < 1.0, (
        f"default rate {rate} too generous — should be < 1/s for "
        "defence against mass-mint"
    )
    burst = s.service_mint_refresh_token_rate_limit_per_target_burst
    assert burst >= 1
    assert burst <= 20, (
        f"default burst {burst} too generous — should be ≤ 20 to "
        "keep mass-mint window small"
    )
