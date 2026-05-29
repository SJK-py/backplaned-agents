"""Tests for the auth-perimeter hardening review-fixes
(Sec-H2, Sec-H3, Sec-M1, Sec-M2).

Settings tests instantiate `Settings(...)` directly — no FastAPI app
spin-up. Endpoint tests build a minimal mock request + call the
handler function directly so the DB / Redis layers are stubbable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Sec-H3: jwt_secret minimum length
# ---------------------------------------------------------------------------


def _settings_kwargs(jwt_secret: str = "x" * 32, **overrides: Any) -> dict[str, Any]:
    """Minimal kwargs for `Settings(...)` — override one field per test."""
    base = {
        "db_url": "postgresql://u:p@h:5432/d",
        "public_url": "https://router.example.com",
        "jwt_secret": jwt_secret,
        "admin_session_secret": "admin-cookie-signing-secret-32b!!",
    }
    base.update(overrides)
    return base


def test_jwt_secret_below_32_bytes_rejected_at_startup() -> None:
    """Sec-H3: a placeholder / truncated jwt_secret must fail
    validation rather than running the router with a brute-forceable
    HMAC signer."""
    from pydantic import ValidationError

    from bp_router.settings import Settings

    with pytest.raises(ValidationError) as excinfo:
        Settings(**_settings_kwargs(jwt_secret="x"))  # type: ignore[arg-type]
    # The error message should mention jwt_secret + the byte floor.
    msg = str(excinfo.value)
    assert "jwt_secret" in msg
    assert "32 bytes" in msg


def test_jwt_secret_exactly_32_bytes_accepted() -> None:
    """Boundary: 32-byte secret passes (matches SHA-256 output size)."""
    from bp_router.settings import Settings

    s = Settings(**_settings_kwargs(jwt_secret="x" * 32))  # type: ignore[arg-type]
    assert s.jwt_secret.get_secret_value() == "x" * 32


def test_jwt_secret_base64_random_accepted() -> None:
    """A realistic operator-generated secret (`openssl rand -base64 32`)
    is 44 characters / 44 bytes UTF-8. Must pass."""
    from bp_router.settings import Settings

    # 44-char base64 string (no actual entropy required for the test).
    fake_b64 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN0123"
    assert len(fake_b64) == 44
    s = Settings(**_settings_kwargs(jwt_secret=fake_b64))  # type: ignore[arg-type]
    assert len(s.jwt_secret.get_secret_value().encode("utf-8")) >= 32


def test_jwt_secret_unicode_byte_count_not_char_count() -> None:
    """Edge case: 16 multi-byte characters totalling >= 32 bytes is OK.
    The validator measures bytes, not characters."""
    from bp_router.settings import Settings

    # 16 emoji × 4 bytes each = 64 bytes UTF-8.
    secret = "🔐" * 16
    assert len(secret) == 16
    assert len(secret.encode("utf-8")) == 64
    Settings(**_settings_kwargs(jwt_secret=secret))  # type: ignore[arg-type]


def test_jwt_secret_short_unicode_rejected() -> None:
    """Inverse edge: 7 emoji = 28 bytes UTF-8 < 32 byte floor → reject."""
    from pydantic import ValidationError

    from bp_router.settings import Settings

    secret = "🔐" * 7  # 28 bytes
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(jwt_secret=secret))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Sec-H2: jwt_algorithm Literal restricted to HS256
# ---------------------------------------------------------------------------


def test_jwt_algorithm_eddsa_rejected_until_keypair_support() -> None:
    """Sec-H2: `EdDSA` was previously listed in the Literal but
    `jwt_secret: SecretStr` only carries a symmetric secret string.
    Selecting EdDSA would either crash at startup or sign with
    nonsense. Removed from the Literal until proper Ed25519 keypair
    support lands."""
    from pydantic import ValidationError

    from bp_router.settings import Settings

    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(jwt_algorithm="EdDSA"))  # type: ignore[arg-type]


def test_jwt_algorithm_hs256_still_accepted() -> None:
    """Sanity: HS256 (the only intentionally-supported algorithm
    today) still passes."""
    from bp_router.settings import Settings

    s = Settings(**_settings_kwargs(jwt_algorithm="HS256"))  # type: ignore[arg-type]
    assert s.jwt_algorithm == "HS256"


def test_jwt_algorithm_default_is_hs256() -> None:
    """Default unchanged."""
    from bp_router.settings import Settings

    s = Settings(**_settings_kwargs())  # type: ignore[arg-type]
    assert s.jwt_algorithm == "HS256"


# ---------------------------------------------------------------------------
# Sec-M1: login timing-equalization on user-not-found / bad-kind
# ---------------------------------------------------------------------------
#
# `bp_router.api.auth` imports FastAPI at module load. Each handler-
# touching test below begins with `pytest.importorskip("fastapi")`
# so this file's settings-only tests still run on a CI matrix
# without FastAPI installed.


def _mock_login_request(
    *,
    user: Any,
    redis: Any = None,
) -> tuple[Any, Any]:
    """Build a `(state, request)` pair the `login` handler can run
    against. The DB pool returns `user` from `get_user_by_email`."""
    state = MagicMock()
    state.redis = redis
    state.settings = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.settings.session_jwt_ttl_s = 900
    state.settings.refresh_token_ttl_s = 86_400
    # Login rate-limit settings — generous enough that the test
    # path always lands in the "allowed" branch.
    state.settings.login_rate_limit_per_ip_per_s = 100.0
    state.settings.login_rate_limit_per_ip_burst = 100
    state.settings.login_rate_limit_per_email_per_s = 100.0
    state.settings.login_rate_limit_per_email_burst = 100
    state.settings.refresh_rate_limit_per_ip_per_s = 100.0
    state.settings.refresh_rate_limit_per_ip_burst = 100
    state.settings.change_password_rate_limit_per_user_per_s = 100.0
    state.settings.change_password_rate_limit_per_user_burst = 100

    # Stub login_quota with an always-allow try_consume.
    from bp_router.security.rate_limit import Decision  # noqa: PLC0415
    state.login_quota = MagicMock()
    state.login_quota.try_consume = AsyncMock(
        return_value=Decision(allowed=True, retry_after_s=0.0, tokens_remaining=100.0)
    )

    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    # Patchable query helpers wired onto the conn.
    request = MagicMock()
    request.app.state.bp = state
    request.client.host = "127.0.0.1"
    return state, request


def _mock_change_password_state(*, redis: Any) -> Any:
    """Build a minimal AppState mock for change_password tests, with the
    new rate-limit fields stubbed to always allow.
    """
    from bp_router.security.rate_limit import Decision  # noqa: PLC0415

    state = MagicMock()
    state.redis = redis
    state.settings = MagicMock()
    state.settings.session_jwt_ttl_s = 900
    state.settings.change_password_rate_limit_per_user_per_s = 100.0
    state.settings.change_password_rate_limit_per_user_burst = 100
    state.login_quota = MagicMock()
    state.login_quota.try_consume = AsyncMock(
        return_value=Decision(allowed=True, retry_after_s=0.0, tokens_remaining=100.0)
    )
    state.db_pool = MagicMock()
    return state


def test_login_runs_dummy_verify_when_user_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sec-M1 regression: when `get_user_by_email` returns None,
    `login` must still call `verify_password` (against the dummy
    hash) so the response time is indistinguishable from the
    user-exists / bad-password path."""
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.api import auth

    # Stub: no user in DB.
    monkeypatch.setattr(
        auth.queries, "get_user_by_email", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        auth.queries, "append_audit_event", AsyncMock(return_value=None)
    )
    verify_calls: list[tuple[str, str]] = []

    def _fake_verify(plaintext: str, encoded: str) -> bool:
        verify_calls.append((plaintext, encoded))
        return False

    monkeypatch.setattr(auth, "verify_password", _fake_verify)

    _, request = _mock_login_request(user=None)
    req = auth.LoginRequest(email="ghost@example.com", password="hunter2")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(auth.login(req, request))
    assert excinfo.value.status_code == 401

    # Critical: verify_password ran exactly once with the dummy hash.
    # If Sec-M1 ever regresses (early return before verify), the list
    # would be empty.
    assert len(verify_calls) == 1
    assert verify_calls[0][0] == "hunter2"
    assert verify_calls[0][1] == auth._DUMMY_HASH


def test_login_runs_dummy_verify_on_wrong_auth_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same equalization on the OIDC / API-key paths: a user exists
    but their `auth_kind != 'password'`. Without the dummy verify,
    those accounts would respond ~5 ms vs. the ~50-100 ms password
    path — a fingerprint of "this email uses SSO / API keys."""
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.api import auth

    user = MagicMock()
    user.suspended_at = None
    user.auth_kind = "oidc"
    user.auth_secret_hash = None  # OIDC users have no password hash

    monkeypatch.setattr(
        auth.queries, "get_user_by_email", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        auth.queries, "append_audit_event", AsyncMock(return_value=None)
    )
    verify_calls: list[Any] = []
    monkeypatch.setattr(
        auth, "verify_password", lambda p, h: verify_calls.append((p, h)) or False
    )

    _, request = _mock_login_request(user=user)
    req = auth.LoginRequest(email="oidc-user@example.com", password="anything")

    with pytest.raises(HTTPException):
        asyncio.run(auth.login(req, request))
    # Dummy verify ran once on the bad-auth-kind path.
    assert len(verify_calls) == 1
    assert verify_calls[0][1] == auth._DUMMY_HASH


def test_login_dummy_hash_is_a_real_argon2_hash() -> None:
    """Source-level: the constant must be a parseable argon2id hash
    (otherwise verify_password would short-circuit on malformed
    input and the timing-equalization is defeated)."""
    pytest.importorskip("fastapi")
    from bp_router.api.auth import _DUMMY_HASH
    from bp_router.security.passwords import verify_password

    # Argon2 PHC string starts with `$argon2id$`.
    assert _DUMMY_HASH.startswith("$argon2id$")
    # And verify_password runs the full hash comparison without
    # raising on the wrong input — returns False.
    assert verify_password("not the dummy plaintext", _DUMMY_HASH) is False


# ---------------------------------------------------------------------------
# Sec-M2: change-password revokes the active access token's JTI
# ---------------------------------------------------------------------------


def test_change_password_revokes_active_jti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sec-M2 regression: after a successful password change, the
    caller's CURRENT access token JTI must be added to the revocation
    set so the token can't be reused. The previous implementation
    only deleted refresh tokens; the access token stayed valid until
    natural expiry."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth
    from bp_router.security.jwt import SessionPrincipal
    from bp_router.security.passwords import hash_password

    # Build a "real" current password hash so verify_password matches.
    current_hash = hash_password("old-password-123")

    user = MagicMock()
    user.user_id = "usr_alice"
    user.auth_kind = "password"
    user.auth_secret_hash = current_hash
    user.suspended_at = None
    user.deleted_at = None

    monkeypatch.setattr(
        auth.queries, "get_user_by_id", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        auth.queries,
        "delete_user_refresh_tokens",
        AsyncMock(return_value=2),
    )
    monkeypatch.setattr(
        auth.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    revoked: list[tuple[Any, str, int]] = []

    async def _fake_revoke(redis: Any, jti: str, *, ttl_s: int) -> None:
        revoked.append((redis, jti, ttl_s))

    monkeypatch.setattr(auth, "revoke_jti", _fake_revoke)

    state = _mock_change_password_state(redis=MagicMock())

    pool = state.db_pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    request = MagicMock()
    request.app.state.bp = state

    principal = SessionPrincipal(
        user_id="usr_alice",
        level="tier0",
        expires_at=datetime.now(UTC) + timedelta(seconds=900),
        jti="jti_active_session_123",
    )
    req = auth.ChangePasswordRequest(
        current_password="old-password-123",
        new_password="new-password-456",
    )

    asyncio.run(auth.change_password(req, request, principal))

    # Critical assertion: revoke_jti was called with the active jti.
    assert len(revoked) == 1
    _redis_arg, jti_arg, ttl_arg = revoked[0]
    assert jti_arg == "jti_active_session_123"
    assert ttl_arg >= 900  # at least the session TTL


def test_change_password_skips_revoke_when_redis_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-worker deployments without Redis have no JTI store —
    `revoke_jti` shouldn't be called at all (and certainly shouldn't
    raise). Same trade-off `/logout` documents."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth
    from bp_router.security.jwt import SessionPrincipal
    from bp_router.security.passwords import hash_password

    user = MagicMock()
    user.user_id = "usr_alice"
    user.auth_kind = "password"
    user.auth_secret_hash = hash_password("old-pwd")
    user.suspended_at = None
    user.deleted_at = None

    monkeypatch.setattr(
        auth.queries, "get_user_by_id", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(
        auth.queries,
        "delete_user_refresh_tokens",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        auth.queries, "append_audit_event", AsyncMock(return_value=None)
    )
    revoke_mock = AsyncMock()
    monkeypatch.setattr(auth, "revoke_jti", revoke_mock)

    state = _mock_change_password_state(redis=None)

    pool = state.db_pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    request = MagicMock()
    request.app.state.bp = state

    principal = SessionPrincipal(
        user_id="usr_alice",
        level="tier0",
        expires_at=datetime.now(UTC) + timedelta(seconds=900),
        jti="jti_x",
    )
    req = auth.ChangePasswordRequest(
        current_password="old-pwd", new_password="new-pwd-xyz",
    )

    asyncio.run(auth.change_password(req, request, principal))

    revoke_mock.assert_not_called()


def test_change_password_revokes_after_db_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source-level: the revoke_jti call must happen AFTER the `async
    with pool.acquire() as conn:` block exits. If we revoked first
    and then the password update raised, we'd have invalidated the
    user's only token without actually changing the password —
    locking them out for `session_jwt_ttl_s` seconds."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router.api import auth

    src = inspect.getsource(auth.change_password)
    # The acquire-block ends BEFORE the revoke_jti call.
    pool_block_idx = src.find("async with pool.acquire() as conn:")
    revoke_idx = src.find("revoke_jti(")
    assert pool_block_idx >= 0 and revoke_idx >= 0
    # `revoke_jti` appears AFTER the `async with` block — the
    # source order matters for transactionality.
    assert revoke_idx > pool_block_idx
    # And it's outside the indented block (zero leading indent on
    # the `if state.redis is not None:` check just before).
    # The block ends with a dedent before the revoke (the line
    # immediately before the `if` is unindented relative to the
    # acquire block).
    assert "\n    if state.redis is not None:" in src
