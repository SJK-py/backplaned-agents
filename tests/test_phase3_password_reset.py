"""Tests for Phase 3: F9 password-reset token mint + consume.

Source-pin style — exercises the auth + admin handler shapes
without spinning up the full app. Cross-DB behaviour (FOR UPDATE,
the single-use transition) is integration-test territory.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# Settings
# ===========================================================================


def test_settings_has_password_reset_fields() -> None:
    from bp_router.settings import Settings

    fields = Settings.model_fields
    for name in (
        "password_reset_token_ttl_s",
        "password_reset_mint_rate_limit_per_target_per_s",
        "password_reset_mint_rate_limit_per_target_burst",
        "password_reset_consume_rate_limit_per_ip_per_s",
        "password_reset_consume_rate_limit_per_ip_burst",
    ):
        assert name in fields


def test_settings_password_reset_ttl_default_is_10_minutes() -> None:
    from bp_router.settings import Settings

    field = Settings.model_fields["password_reset_token_ttl_s"]
    assert field.default == 600


# ===========================================================================
# Query helpers
# ===========================================================================


def test_consume_password_reset_token_uses_for_update() -> None:
    """Single-use semantics require FOR UPDATE — two concurrent
    consumers must see the transition in order. Source-pin the
    clause."""
    from bp_router.db import queries

    src = inspect.getsource(queries.consume_password_reset_token)
    assert "FOR UPDATE" in src
    # Both the expired and the already-used branches return None.
    assert 'row["used_at"] is not None' in src
    assert 'row["expires_at"] < _now()' in src


def test_consume_password_reset_token_marks_used() -> None:
    from bp_router.db import queries

    src = inspect.getsource(queries.consume_password_reset_token)
    assert "SET used_at = now()" in src


def test_set_user_password_hash_does_not_touch_auth_kind() -> None:
    """Critical: the Gemini fork silently flipped auth_kind to
    'password'. F9 must NOT — OIDC users get refused at the consume
    handler instead. Inspect just the SQL string (the docstring
    legitimately mentions auth_kind to warn future maintainers)."""
    from bp_router.db import queries

    src = inspect.getsource(queries.set_user_password_hash)
    # The SQL string lives between the `"""` of the body and the
    # `,` that ends the args. Pin the exact UPDATE — and pin that
    # `SET ... auth_kind` does not appear.
    assert "SET auth_secret_hash = $2" in src
    assert "SET auth_kind" not in src
    # And: no other reference to `auth_kind = ` (the SET clause
    # would have to use that shape).
    assert "auth_kind =" not in src


def test_insert_password_reset_token_records_created_by() -> None:
    """Audit trail: the principal that minted the token (admin or
    service) must be recorded on the row."""
    from bp_router.db import queries

    src = inspect.getsource(queries.insert_password_reset_token)
    assert "created_by" in src


# ===========================================================================
# F9 mint — admin OR service+serviced_by gate
# ===========================================================================


def test_mint_password_reset_token_admin_path_skips_serviced_by_check() -> None:
    """Source pin: an admin caller hits the `pass` branch — no
    `serviced_by` check, no service-principal-only restriction."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert 'principal.level == "admin"' in src
    # And the service branch enforces the F8 gate.
    assert 'principal.level == "service"' in src
    assert "target.serviced_by" in src


def test_mint_password_reset_token_denies_non_admin_non_service() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert "must be admin or service principal" in src


def test_mint_password_reset_token_audits_denial() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert "auth.password_reset_mint_denied" in src
    assert '"reason": "not_serviced_by"' in src


def test_mint_password_reset_token_rate_limits_per_target() -> None:
    """Bucket prefix is per-endpoint so a noisy mint for one user
    doesn't bleed onto others. Routed through the shared
    `_enforce_per_target_mint_rate_limit` helper, passing the
    central `BUCKET_PASSWORD_RESET_MINT` constant."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert "_enforce_per_target_mint_rate_limit(" in src
    assert "BUCKET_PASSWORD_RESET_MINT" in src
    assert 'audit_event="auth.password_reset_mint_rate_limited"' in src


def test_mint_password_reset_token_refuses_suspended_target() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert "target.suspended_at is not None" in src


def test_mint_password_reset_token_uses_authenticated_dependency() -> None:
    """Endpoint isn't restricted to require_admin or require_service —
    it's `require_authenticated` and branches on level inside."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin
    from bp_router.security.jwt import require_authenticated

    sig = inspect.signature(admin.mint_password_reset_token)
    dep = sig.parameters["principal"].default
    assert dep.dependency is require_authenticated


# ===========================================================================
# F9 consume — OIDC refusal, kill refresh tokens, fresh pair
# ===========================================================================


def test_reset_password_refuses_non_password_users() -> None:
    """Critical departure from the Gemini fork: NEVER flip auth_kind
    silently. OIDC-only users must be told to use their identity
    provider's reset flow."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    assert 'user.auth_kind != "password"' in src
    # Error message includes the auth_kind value AND the
    # password-only phrasing (concatenated across two string literals
    # in source; check each part separately).
    assert "auth_kind={user.auth_kind!r}" in src
    assert "is only supported for password-authenticated users" in src


def test_reset_password_revokes_every_refresh_token() -> None:
    """The consume path MUST delete every refresh token before
    issuing the new pair — otherwise a stale device would survive
    the reset."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    assert "delete_user_refresh_tokens" in src


def test_reset_password_issues_fresh_pair_inside_same_txn() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    # All five mutations live inside one async-with conn.transaction().
    assert "consume_password_reset_token" in src
    assert "set_user_password_hash" in src
    assert "insert_refresh_token" in src
    assert "auth.password_reset_token_consumed" in src


def test_reset_password_per_ip_rate_limit() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    # Bucket key uses the centralised constant (R3 quota module).
    assert "BUCKET_RESET_PASSWORD" in src
    assert "password_reset_consume_rate_limit_per_ip_per_s" in src


def test_reset_password_invalid_token_returns_401() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    # When consume returns None, audit + raise 401.
    assert "if user_id is None:" in src
    assert "auth.password_reset_token_invalid" in src
    assert "401" in src and "invalid or expired token" in src


def test_reset_password_refuses_suspended_user() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth.reset_password)
    # Now routed through the `user_is_active` helper which folds
    # the `is None / suspended_at / deleted_at` triplet.
    assert "user_is_active(" in src
    assert "user inactive" in src


def test_reset_password_endpoint_is_unauthenticated() -> None:
    """The token IS the auth — no Bearer dependency. Source pin on
    the absence of a Depends in the signature."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    sig = inspect.signature(auth.reset_password)
    # No `principal` parameter, no Depends().
    assert "principal" not in sig.parameters
