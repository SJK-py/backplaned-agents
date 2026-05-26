"""Bucket-key prefix constants.

The rate-limit bucket-key prefixes (`login`, `refresh`,
`change_password`, etc.) were duplicated as string literals
across `bp_router/api/auth.py`, `bp_router/api/admin.py`, and
`bp_router/dispatch.py` — and matched separately to per-endpoint
audit event names like `auth.password_reset_mint_denied`. A typo
in either half (a stray `_` in the prefix, a missing axis
segment) would silently shift the bucket without breaking any
tests.

The `bp_router.quota` module centralises the prefix constants so
every consume site uses the same canonical string.
"""

from __future__ import annotations

import inspect

import pytest


def test_quota_module_exports_expected_constants() -> None:
    """Pin the names + values. Any new bucket prefix should also
    land here so the central module stays the canonical list."""
    import bp_router.quota as q

    expected = {
        "BUCKET_LOGIN": "login",
        "BUCKET_REFRESH": "refresh",
        "BUCKET_RESET_PASSWORD": "reset_password",
        "BUCKET_CHANGE_PASSWORD": "change_password",
        "BUCKET_PASSWORD_RESET_MINT": "password_reset_mint",
        "BUCKET_SERVICE_MINT_REFRESH_TOKEN": "service_mint_refresh_token",
        "BUCKET_AGENT_INFO_UPDATE": "agent_info_update",
    }
    for name, value in expected.items():
        assert hasattr(q, name), f"bp_router.quota missing {name}"
        assert getattr(q, name) == value, (
            f"bp_router.quota.{name} drifted from canonical {value!r}"
        )


def test_auth_login_uses_constant() -> None:
    """Login rate-limit lives in the `_enforce_login_rate_limit`
    helper, not the route function itself. Pin at the module level
    so the constant is reachable from wherever the consume runs."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth)
    assert "BUCKET_LOGIN" in src
    assert 'f"login:ip:' not in src


def test_auth_refresh_uses_constant() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.refresh)
    assert "BUCKET_REFRESH" in src
    assert 'f"refresh:ip:' not in src


def test_auth_change_password_uses_constant() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.change_password)
    assert "BUCKET_CHANGE_PASSWORD" in src
    assert 'f"change_password:user:' not in src


def test_auth_reset_password_uses_constant() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    assert "BUCKET_RESET_PASSWORD" in src
    assert 'f"reset_password:ip:' not in src


def test_admin_mint_endpoints_pass_constants() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src_service = inspect.getsource(admin.service_mint_refresh_token)
    assert "BUCKET_SERVICE_MINT_REFRESH_TOKEN" in src_service
    assert 'bucket_prefix="service_mint_refresh_token"' not in src_service

    src_reset = inspect.getsource(admin.mint_password_reset_token)
    assert "BUCKET_PASSWORD_RESET_MINT" in src_reset
    assert 'bucket_prefix="password_reset_mint"' not in src_reset


def test_dispatch_agent_info_update_uses_constant() -> None:
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_agent_info_update)
    assert "BUCKET_AGENT_INFO_UPDATE" in src
    assert 'f"agent_info_update:' not in src
