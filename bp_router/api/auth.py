"""bp_router.api.auth — Login, refresh-token rotation, logout, password change,
password reset."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from bp_router.api.admin import _PRIVILEGED_LEVELS, _denial_audit_allowed
from bp_router.db import queries
from bp_router.quota import (
    BUCKET_CHANGE_PASSWORD,
    BUCKET_LINK_TOKEN_MINT,
    BUCKET_LOGIN,
    BUCKET_REFRESH,
    BUCKET_RESET_PASSWORD,
)
from bp_router.security.jwt import (
    SessionPrincipal,
    issue_session_token,
    require_authenticated,
    require_service,
    revoke_jti,
)
from bp_router.security.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime
    level: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


# Pre-computed argon2 hash used to equalise login timing on the
# user-not-found / wrong-auth-kind paths. Without this, an attacker
# can probe email-existence by measuring response time: ~5 ms when
# the user doesn't exist (no argon2 verify) vs. ~50-100 ms when it
# does (full argon2id at 64 MiB cost). With the dummy verify, both
# paths take the same time.
#
# The plaintext is never sent over the wire and the hash isn't a
# secret — its only purpose is to give `verify_password` something
# to chew on. Computed once at import to avoid paying the hash cost
# on every "no user" request.
_DUMMY_HASH = hash_password("bp_router-timing-equalization-dummy-secret")


def _client_ip(request: Request) -> str:
    """Source IP for rate-limit bucketing.

    Uses `request.client.host` and deliberately ignores
    `X-Forwarded-For`. A client behind an untrusted proxy can spoof
    that header and bucket-hop indefinitely. Operators terminating
    TLS behind a trusted reverse proxy should run uvicorn with
    `--proxy-headers`; that path rewrites `request.client.host` to
    the real client IP for us, and we read it directly here.
    """
    client = request.client
    return client.host if client else "unknown"


async def _enforce_single_bucket_rate_limit(
    *,
    quota: object,
    key: str,
    rate_per_s: float,
    burst: int,
) -> float:
    """Try the bucket once. Returns 0.0 on allowed, else the
    `retry_after_s` from the bucket. The caller turns the max wait
    across multiple buckets into a single 429 response.
    """
    decision = await quota.try_consume(  # type: ignore[attr-defined]
        key=key, rate_per_s=rate_per_s, burst=burst
    )
    return 0.0 if decision.allowed else max(decision.retry_after_s, 0.0)


async def _enforce_login_rate_limit(
    state: object, request: Request, email: str
) -> None:
    """Run the per-IP and per-email login buckets; raise 429 if either
    exhausts. Runs BEFORE argon2 verify so a saturated bucket
    short-circuits without paying the hash cost (and without giving
    the attacker a timing oracle — denial is on rate, not on whether
    the email exists).
    """
    settings = state.settings  # type: ignore[attr-defined]
    quota = state.login_quota  # type: ignore[attr-defined]
    ip = _client_ip(request)

    ip_wait = await _enforce_single_bucket_rate_limit(
        quota=quota,
        key=f"{BUCKET_LOGIN}:ip:{ip}",
        rate_per_s=settings.login_rate_limit_per_ip_per_s,
        burst=settings.login_rate_limit_per_ip_burst,
    )
    email_wait = await _enforce_single_bucket_rate_limit(
        quota=quota,
        key=f"login:email:{email.lower()}",
        rate_per_s=settings.login_rate_limit_per_email_per_s,
        burst=settings.login_rate_limit_per_email_burst,
    )
    if ip_wait > 0 or email_wait > 0:
        which = "ip" if ip_wait >= email_wait else "email"
        retry_after = max(int(max(ip_wait, email_wait, 1.0) + 0.999), 1)
        pool = state.db_pool  # type: ignore[attr-defined]
        try:
            async with pool.acquire() as conn:
                await queries.append_audit_event(
                    conn,
                    actor_kind="user",
                    actor_id=None,
                    event="auth.login_rate_limited",
                    payload={"bucket": which, "email": email, "ip": ip},
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "auth_rate_limit_audit_failed",
                extra={"event": "auth_rate_limit_audit_failed"},
                exc_info=True,
            )
        raise HTTPException(
            status_code=429,
            detail="too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenPair)
async def login(req: LoginRequest, request: Request) -> TokenPair:
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    # Rate-limit BEFORE the DB read so a saturated bucket short-circuits
    # without paying the read cost; also before argon2 so a flood doesn't
    # burn 50-100 ms × N on the hash.
    await _enforce_login_rate_limit(state, request, req.email)

    async with pool.acquire() as conn:
        user = await queries.get_user_by_email(conn, req.email)
        # Timing-equalization: on every failure
        # path that returns "invalid credentials", run argon2 verify
        # against a precomputed dummy hash so the response time looks
        # the same regardless of whether the email exists. Without
        # this, an attacker can enumerate accounts by measuring
        # ~5 ms (no user) vs ~50-100 ms (argon2 verify ran).
        if not queries.user_is_active(user):
            verify_password(req.password, _DUMMY_HASH)
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=None,
                event="auth.login_failed",
                payload={"email": req.email, "reason": "no_user"},
            )
            raise HTTPException(status_code=401, detail="invalid credentials")

        if user.auth_kind != "password" or not user.auth_secret_hash:
            verify_password(req.password, _DUMMY_HASH)
            raise HTTPException(status_code=401, detail="invalid credentials")

        if not verify_password(req.password, user.auth_secret_hash):
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=user.user_id,
                event="auth.login_failed",
                payload={"reason": "bad_password"},
            )
            raise HTTPException(status_code=401, detail="invalid credentials")

        # TOTP would be enforced here; out of scope for the skeleton happy path.

        if needs_rehash(user.auth_secret_hash):
            from bp_router.security.passwords import hash_password  # noqa: PLC0415

            new_hash = hash_password(req.password)
            await conn.execute(
                "UPDATE users SET auth_secret_hash = $2 WHERE user_id = $1",
                user.user_id,
                new_hash,
            )

    access, expires_at, _jti = issue_session_token(
        user_id=user.user_id,
        level=user.level,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.session_jwt_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )

    refresh = secrets.token_urlsafe(32)
    refresh_expires = _now() + timedelta(seconds=settings.refresh_token_ttl_s)

    async with pool.acquire() as conn:
        # Atomic refresh-token-insert + audit.
        # If the audit append fails, the refresh token MUST roll back
        # — otherwise we'd return a `TokenPair` whose refresh half was
        # issued without a matching audit row.
        async with conn.transaction():
            await queries.insert_refresh_token(
                conn,
                token_hash=_hash_refresh_token(refresh),
                user_id=user.user_id,
                expires_at=refresh_expires,
            )
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=user.user_id,
                event="auth.login_succeeded",
            )

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        level=user.level,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(req: RefreshRequest, request: Request) -> TokenPair:
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    # Per-IP rate limit on refresh — looser than login since legitimate
    # BFFs refresh every few minutes per agent.
    ip = _client_ip(request)
    wait = await _enforce_single_bucket_rate_limit(
        quota=state.login_quota,
        key=f"{BUCKET_REFRESH}:ip:{ip}",
        rate_per_s=settings.refresh_rate_limit_per_ip_per_s,
        burst=settings.refresh_rate_limit_per_ip_burst,
    )
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail="too many refresh attempts",
            headers={"Retry-After": str(max(int(wait + 0.999), 1))},
        )

    new_refresh = secrets.token_urlsafe(32)

    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await queries.consume_refresh_token(
                conn,
                token_hash=_hash_refresh_token(req.refresh_token),
                replaced_by=_hash_refresh_token(new_refresh),
            )
            if user_id is None:
                await queries.append_audit_event(
                    conn,
                    actor_kind="user",
                    actor_id=None,
                    event="auth.refresh_replayed",
                )
                raise HTTPException(status_code=401, detail="invalid refresh token")

            user = await queries.get_user_by_id(conn, user_id)
            if not queries.user_is_active(user):
                raise HTTPException(status_code=401, detail="user inactive")

            new_expires = _now() + timedelta(seconds=settings.refresh_token_ttl_s)
            await queries.insert_refresh_token(
                conn,
                token_hash=_hash_refresh_token(new_refresh),
                user_id=user.user_id,
                expires_at=new_expires,
            )
            # Symmetric audit for the success path. Without this, silence
            # after a Redis outage is ambiguous between "no-one is
            # refreshing" and "rows aren't being persisted" — operators
            # had no way to tell.
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=user.user_id,
                event="auth.refresh_succeeded",
            )

    access, expires_at, _jti = issue_session_token(
        user_id=user.user_id,
        level=user.level,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.session_jwt_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )

    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_at=expires_at,
        level=user.level,
    )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class LogoutRequest(BaseModel):
    refresh_token: str | None = None
    """Optional. When supplied, the corresponding refresh-token row is
    revoked so it cannot mint new access tokens. Without it, only the
    presented access token's `jti` is added to the Redis revocation
    set; refresh tokens for the user remain valid."""


@router.post("/logout", status_code=204)
async def logout(
    req: LogoutRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> None:
    """Revoke the active session token. Optionally revoke a matching
    refresh token in the same call (the BFF / admin UI should pass it).

    Idempotent: a second logout with the same tokens is a no-op."""
    state = request.app.state.bp
    settings = state.settings

    # Revoke the access token via the Redis JTI set (no-op if Redis
    # isn't configured — single-worker deployments accept that the
    # access token stays valid until natural expiry).
    #
    # TTL = `max(remaining, session_jwt_ttl_s)` — defence-in-depth
    # Keeps the entry revoked at LEAST for the default TTL even
    # when the token only had seconds left. A future maintainer
    # should NOT "fix" this to `min(...)`, which would shrink the
    # revocation window for almost-expired tokens.
    if state.redis is not None:
        ttl_s = max(
            int((principal.expires_at - _now()).total_seconds()),
            settings.session_jwt_ttl_s,
        )
        await revoke_jti(state.redis, principal.jti, ttl_s=ttl_s)

    # Revoke the refresh token if supplied. Wrong-user / invalid /
    # already-used tokens silently no-op so logout is always 204.
    if req.refresh_token:
        async with state.db_pool.acquire() as conn:
            await queries.revoke_refresh_token(
                conn, _hash_refresh_token(req.refresh_token)
            )
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="auth.logout",
                payload={"refresh_token_revoked": True},
            )
    else:
        async with state.db_pool.acquire() as conn:
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="auth.logout",
                payload={"refresh_token_revoked": False},
            )


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/change-password", status_code=204)
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> None:
    """Change the caller's own password.

    Requires the current password as a confirmation. On success:

      - All of the user's refresh tokens are deleted (every device
        is forced to re-login on next refresh).
      - The CURRENT access token's `jti` is added to the revocation
        set so it can't be reused after this call returns.
        Tokens issued earlier on OTHER devices remain
        valid until natural expiry; a per-user epoch flag for
        global-logout-on-password-change is future work.
    """
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    # Per-user rate limit. Very tight by default — a human changes
    # their password rarely.
    wait = await _enforce_single_bucket_rate_limit(
        quota=state.login_quota,
        key=f"{BUCKET_CHANGE_PASSWORD}:user:{principal.user_id}",
        rate_per_s=settings.change_password_rate_limit_per_user_per_s,
        burst=settings.change_password_rate_limit_per_user_burst,
    )
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail="too many change-password attempts",
            headers={"Retry-After": str(max(int(wait + 0.999), 1))},
        )

    async with pool.acquire() as conn:
        user = await queries.get_user_by_id(conn, principal.user_id)
        if not queries.user_is_active(user):
            # 404 on exists-but-locked too — don't leak which.
            raise HTTPException(status_code=404, detail="user not found")
        if user.auth_kind != "password" or not user.auth_secret_hash:
            raise HTTPException(
                status_code=409,
                detail=f"cannot change password for auth_kind={user.auth_kind!r}",
            )
        if not verify_password(req.current_password, user.auth_secret_hash):
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="auth.password_change_failed",
                payload={"reason": "bad_current_password"},
            )
            raise HTTPException(status_code=401, detail="invalid credentials")
        if req.current_password == req.new_password:
            raise HTTPException(
                status_code=400,
                detail="new password must differ from current password",
            )

        new_hash = hash_password(req.new_password)
        # Atomic password update + refresh-token wipe + audit.
        # All three changes commit together or none of
        # them does. The post-commit `revoke_jti` outside the
        # `pool.acquire()` block stays unchanged — it's deliberately
        # after-commit so a transaction abort doesn't strand the
        # caller's only token in the revocation set.
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET auth_secret_hash = $2 WHERE user_id = $1",
                principal.user_id,
                new_hash,
            )
            deleted = await queries.delete_user_refresh_tokens(
                conn, principal.user_id
            )
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=principal.user_id,
                event="auth.password_changed",
                payload={
                    "refresh_tokens_revoked": deleted,
                    # Intent only — the actual revoke happens
                    # post-commit (see below) and may fail. A
                    # follow-up `auth.password_change_revoke_jti`
                    # audit event records the real outcome so
                    # operators can tell "claimed" from "did".
                    "active_jti_revoke_attempted": state.redis is not None,
                },
            )

    # Revoke the active access token's jti AFTER the DB transaction
    # commits — if the password update fails partway through, we
    # don't want to have already invalidated the token. Best-effort
    # against Redis: single-worker deployments without Redis accept
    # that the current access token stays valid until natural expiry
    # (matches the same trade-off the `/logout` endpoint documents).
    #
    # TTL = `max(remaining, session_jwt_ttl_s)` — defence-in-depth.
    # Keeps the entry revoked at LEAST for the default TTL even
    # when the token only had seconds left. A future maintainer
    # should NOT "fix" this to `min(...)`.
    if state.redis is not None:
        ttl_s = max(
            int((principal.expires_at - _now()).total_seconds()),
            settings.session_jwt_ttl_s,
        )
        try:
            await revoke_jti(state.redis, principal.jti, ttl_s=ttl_s)
            revoke_outcome = "ok"
        except Exception as exc:
            # Best-effort: password change has already committed and
            # refresh tokens are wiped, so the worst case is that
            # the current access token stays valid until natural
            # expiry. Log so operators can investigate Redis health.
            logger.warning(
                "auth.password_changed.revoke_jti_failed",
                extra={
                    "event": "auth.password_changed.revoke_jti_failed",
                    "bp.user_id": principal.user_id,
                    "error": str(exc),
                },
            )
            revoke_outcome = f"failed:{type(exc).__name__}"

        # Follow-up audit so the trail records what actually
        # happened, not only the in-transaction intent. Wrapped in
        # its own try/except — failure to write this audit row must
        # not break the password-change response since the password
        # is already changed.
        try:
            async with pool.acquire() as conn2:
                await queries.append_audit_event(
                    conn2,
                    actor_kind="user",
                    actor_id=principal.user_id,
                    event="auth.password_change_revoke_jti",
                    payload={"outcome": revoke_outcome},
                )
        except Exception:
            logger.warning(
                "auth.password_change_revoke_jti.audit_failed",
                extra={
                    "event": "auth.password_change_revoke_jti.audit_failed",
                    "bp.user_id": principal.user_id,
                },
            )


# ---------------------------------------------------------------------------
# F9: password-reset token consume
# ---------------------------------------------------------------------------


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password", response_model=TokenPair, status_code=200)
async def reset_password(
    req: ResetPasswordRequest,
    request: Request,
) -> TokenPair:
    """Consume a password-reset token and set the user's password.

    The token IS the auth — no Bearer header required. Per-IP rate
    limit bounds token-enumeration attempts.

    On success:
      1. Validates the token (FOR UPDATE).
      2. Refuses 409 if the user has been suspended since mint.
      3. Refuses 409 if `auth_kind != "password"` — we do NOT
         silently flip OIDC users to password auth.
      4. Hashes the new password and writes via
         `set_user_password_hash` (which DOES NOT touch auth_kind).
      5. Deletes every existing refresh token for the user (forces
         re-login on every device).
      6. Issues a fresh access + refresh pair so the user can
         continue without a separate login step.
    """
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    # Per-IP rate-limit BEFORE we touch the DB; bounds token-
    # enumeration scans. Bucket key uses request.client.host
    # directly (NOT X-Forwarded-For — operators behind a trusted
    # reverse proxy should run uvicorn with --proxy-headers).
    ip = _client_ip(request)
    wait = await _enforce_single_bucket_rate_limit(
        quota=state.login_quota,
        key=f"{BUCKET_RESET_PASSWORD}:ip:{ip}",
        rate_per_s=settings.password_reset_consume_rate_limit_per_ip_per_s,
        burst=settings.password_reset_consume_rate_limit_per_ip_burst,
    )
    if wait > 0:
        retry_after = max(int(wait + 0.999), 1)
        try:
            async with pool.acquire() as conn:
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=None,
                    event="auth.password_reset_rate_limited",
                    payload={"ip": ip, "retry_after_s": retry_after},
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "auth_rate_limit_audit_failed",
                extra={"event": "auth_rate_limit_audit_failed"},
                exc_info=True,
            )
        raise HTTPException(
            status_code=429,
            detail="too many reset-password attempts; retry later",
            headers={"Retry-After": str(retry_after)},
        )

    new_refresh = secrets.token_urlsafe(32)
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await queries.consume_password_reset_token(
                conn, token_hash=_hash_refresh_token(req.token),
            )
            if user_id is None:
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=None,
                    event="auth.password_reset_token_invalid",
                )
                raise HTTPException(401, "invalid or expired token")
            user = await queries.get_user_by_id(conn, user_id)
            if not queries.user_is_active(user):
                raise HTTPException(409, "user inactive")
            if user.auth_kind != "password":
                # Critical departure from the Gemini fork: do NOT
                # silently flip auth_kind to "password". OIDC users
                # must use the OIDC reset flow; surface the mismatch
                # so the caller hits the right one.
                raise HTTPException(
                    409,
                    f"user auth_kind={user.auth_kind!r}; password reset "
                    "is only supported for password-authenticated users",
                )
            new_hash = hash_password(req.new_password)
            await queries.set_user_password_hash(
                conn, user_id=user_id, auth_secret_hash=new_hash,
            )
            deleted = await queries.delete_user_refresh_tokens(conn, user_id)
            new_expires = _now() + timedelta(seconds=settings.refresh_token_ttl_s)
            await queries.insert_refresh_token(
                conn,
                token_hash=_hash_refresh_token(new_refresh),
                user_id=user_id,
                expires_at=new_expires,
            )
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=user_id,
                event="auth.password_reset_token_consumed",
                target_kind="user", target_id=user_id,
                payload={"refresh_tokens_revoked": deleted},
            )

    access, expires_at, _jti = issue_session_token(
        user_id=user.user_id,
        level=user.level,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.session_jwt_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_at=expires_at,
        level=user.level,
    )


class VerifyResetTokenRequest(BaseModel):
    token: str


class VerifyResetTokenResponse(BaseModel):
    user_id: str


@router.post(
    "/verify-reset-token",
    response_model=VerifyResetTokenResponse,
    status_code=200,
)
async def verify_reset_token(
    req: VerifyResetTokenRequest,
    request: Request,
) -> VerifyResetTokenResponse:
    """Consume a password-reset token and return the user it belongs to —
    WITHOUT setting a password or issuing a session.

    The verify-only sibling of `reset-password`: the suite's channel-link
    flow (`/link <token>`) uses it to prove that whoever pastes a token
    into an unmapped chat owns an existing account, then binds the chat to
    the returned `user_id`. Like reset-password, the token IS the auth (no
    Bearer header) and it is SINGLE-USE — consumed here, so a leaked token
    can't be replayed to hijack a link. The same per-IP rate limit bounds
    token enumeration; the bucket is shared with reset-password so the two
    consumption paths draw on one enumeration budget.

    On a missing / expired / already-used token: 401. If the user has been
    deactivated since the token was minted: 409.
    """
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    ip = _client_ip(request)
    wait = await _enforce_single_bucket_rate_limit(
        quota=state.login_quota,
        key=f"{BUCKET_RESET_PASSWORD}:ip:{ip}",
        rate_per_s=settings.password_reset_consume_rate_limit_per_ip_per_s,
        burst=settings.password_reset_consume_rate_limit_per_ip_burst,
    )
    if wait > 0:
        retry_after = max(int(wait + 0.999), 1)
        try:
            async with pool.acquire() as conn:
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=None,
                    event="auth.password_reset_rate_limited",
                    payload={"ip": ip, "retry_after_s": retry_after,
                             "purpose": "verify"},
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "auth_rate_limit_audit_failed",
                extra={"event": "auth_rate_limit_audit_failed"},
                exc_info=True,
            )
        raise HTTPException(
            status_code=429,
            detail="too many verify-reset-token attempts; retry later",
            headers={"Retry-After": str(retry_after)},
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await queries.consume_password_reset_token(
                conn, token_hash=_hash_refresh_token(req.token),
            )
            if user_id is None:
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=None,
                    event="auth.password_reset_token_invalid",
                    payload={"purpose": "verify"},
                )
                raise HTTPException(401, "invalid or expired token")
            user = await queries.get_user_by_id(conn, user_id)
            if not queries.user_is_active(user):
                raise HTTPException(409, "user inactive")
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=user_id,
                event="auth.password_reset_token_verified",
                target_kind="user", target_id=user_id,
                payload={"purpose": "link"},
            )
    return VerifyResetTokenResponse(user_id=user_id)


# ---------------------------------------------------------------------------
# Channel linking — self-service link-token mint + service-side consume.
# ---------------------------------------------------------------------------


class LinkTokenResponse(BaseModel):
    link_token: str
    expires_at: datetime


@router.post("/link-tokens", response_model=LinkTokenResponse, status_code=201)
async def mint_link_token(
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> LinkTokenResponse:
    """Mint a single-use token the CALLER can paste into a chat bot's
    `/link` to bind that chat to this (already-authenticated) account.

    Self-service: authorised by the caller's OWN access token, so the token
    is always minted for `principal.user_id` — a logged-in user can only
    ever link channels to themselves. This is the one legitimate
    self-service mint (the channel-anchored `/password` reset is
    service-gated because there the channel, not a session, is the identity
    proof). It bootstraps the FIRST chat link for a web-only account, which
    otherwise has no channel and thus no way to ever reach the
    service-minted reset flow.

    Reuses the `password_reset_tokens` table; consumed at
    `POST /v1/auth/link-channel` (or harmlessly at reset-password — it's
    the caller's own account either way). Per-user rate-limited; single-use;
    short TTL (`link_token_ttl_s`).
    """
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    wait = await _enforce_single_bucket_rate_limit(
        quota=state.login_quota,
        key=f"{BUCKET_LINK_TOKEN_MINT}:user:{principal.user_id}",
        rate_per_s=settings.link_token_mint_rate_limit_per_user_per_s,
        burst=settings.link_token_mint_rate_limit_per_user_burst,
    )
    if wait > 0:
        retry_after = max(int(wait + 0.999), 1)
        raise HTTPException(
            status_code=429,
            detail="too many link-token mints; retry later",
            headers={"Retry-After": str(retry_after)},
        )

    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=settings.link_token_ttl_s)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await queries.insert_password_reset_token(
                conn,
                token_hash=_hash_refresh_token(token),
                user_id=principal.user_id,
                expires_at=expires_at,
                created_by=principal.user_id,
            )
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=principal.user_id,
                event="auth.link_token_minted",
                target_kind="user", target_id=principal.user_id,
                payload={"expires_at": expires_at.isoformat()},
            )
    return LinkTokenResponse(link_token=token, expires_at=expires_at)


class LinkChannelRequest(BaseModel):
    token: str


class LinkChannelResponse(BaseModel):
    user_id: str


@router.post("/link-channel", response_model=LinkChannelResponse, status_code=200)
async def link_channel(
    req: LinkChannelRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_service),
) -> LinkChannelResponse:
    """Consume a link token AND grant the calling service principal
    `serviced_by` over the owning user — the service-authenticated sibling
    of `verify-reset-token`.

    A chat channel (Telegram/Kakao) calls this from its `/link` command:
    the user pasted a token proving they own an existing account, so the
    channel binds the chat to `user_id` (caller-side) and is authorised to
    service them going forward (mint reset/refresh tokens — e.g. the
    `/password` recovery flow, scheduled-task delivery). Granting
    `serviced_by` HERE — gated on a single-use token the user deliberately
    generated and pasted into this channel — is the link-time analogue of
    the registration-approval auto-grant, without an admin round-trip.

    Privilege boundary: refuses to grant over an admin/service target (a
    service principal must never gain minting rights on a privileged
    account — same guard as the F8/F9 mint endpoints). Token is single-use
    (consumed even when the grant is refused, so a leaked token can't be
    replayed). Returns the owning `user_id`.
    """
    state = request.app.state.bp
    pool = state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await queries.consume_password_reset_token(
                conn, token_hash=_hash_refresh_token(req.token),
            )
            if user_id is None:
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=principal.user_id,
                    event="auth.password_reset_token_invalid",
                    payload={"purpose": "link_channel"},
                )
                raise HTTPException(401, "invalid or expired token")
            user = await queries.get_user_by_id(conn, user_id)
            if not queries.user_is_active(user):
                raise HTTPException(409, "user inactive")
            if user.level in _PRIVILEGED_LEVELS:
                # Raising here rolls back the consume (it's in this txn), so
                # the token stays valid — harmless, since a service principal
                # can never turn an admin/service token into a grant: every
                # attempt 403s right here.
                if await _denial_audit_allowed(
                    state, principal.user_id, "user.serviced_by_grant_denied"
                ):
                    await queries.append_audit_event(
                        conn, actor_kind="user", actor_id=principal.user_id,
                        event="user.serviced_by_grant_denied",
                        target_kind="user", target_id=user_id,
                        payload={"reason": "privileged_target",
                                 "purpose": "link_channel"},
                    )
                raise HTTPException(
                    403,
                    "service principals may not be granted serviced_by over "
                    "admin/service users",
                )
            changed = await queries.append_to_serviced_by(
                conn, user_id, principal.user_id,
            )
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=principal.user_id,
                event="auth.channel_linked",
                target_kind="user", target_id=user_id,
                payload={"service_user_id": principal.user_id,
                         "serviced_by_granted": changed},
            )
    return LinkChannelResponse(user_id=user_id)
