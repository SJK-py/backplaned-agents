"""bp_router.security.jwt — JWT issuance, verification, and FastAPI deps.

See `docs/security.md` §3-5.
"""

from __future__ import annotations

import secrets as _secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request

from bp_router.principals import is_valid_level, level_satisfies_tier

ISSUER = "bp_router"


# ---------------------------------------------------------------------------
# Principal types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPrincipal:
    """Decoded session JWT — what require_admin / require_tier yield."""

    user_id: str
    level: str  # admin | service | tierN  (see bp_router.principals)
    expires_at: datetime
    jti: str


@dataclass(frozen=True)
class AgentPrincipal:
    """Decoded agent JWT — used by the WebSocket Hello validator."""

    agent_id: str
    expires_at: datetime
    jti: str
    sdk_protocol_version: str


@dataclass(frozen=True)
class FileUploadGrant:
    """Decoded one-shot file-upload token.

    Minted by the router (over the already-authenticated agent ws)
    so an agent can stream bytes to `POST /v1/files/upload` without
    a session JWT (agents only carry an `agent`-kind token, which
    the session-scoped file endpoints reject). Content-bound: the
    router re-hashes the streamed body and refuses it unless it
    matches `sha256` — so a leaked grant can't be repurposed to
    upload different bytes. Scoped to the minting user.
    """

    user_id: str
    sha256: str
    byte_size: int
    mime_type: str | None
    expires_at: datetime
    jti: str


@dataclass(frozen=True)
class FileFetchGrant:
    """Decoded file-fetch token minted by the router for a stash-file
    download (the `FileResult.fetch_token`).

    Lets a destination agent `GET /v1/files/{file_id}` for a stash
    file without a session JWT. Bound to a single `file_id`
    AND the owning `user_id`; short-TTL but multi-use within the
    TTL (an inbox re-fetch / handler retry must still resolve).
    """

    file_id: str
    user_id: str
    expires_at: datetime
    jti: str


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _new_jti() -> str:
    return _secrets.token_urlsafe(16)


def issue_session_token(
    *,
    user_id: str,
    level: str,
    secret: str,
    ttl_s: int,
    key_version: int,
    algorithm: str = "HS256",
) -> tuple[str, datetime, str]:
    """Returns (token, expires_at, jti)."""
    if not is_valid_level(level):
        raise ValueError(f"invalid principal level: {level!r}")
    iat = _now()
    exp = iat + timedelta(seconds=ttl_s)
    jti = _new_jti()
    claims = {
        "iss": ISSUER,
        "sub": user_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "kind": "session",
        "level": level,
        "kver": key_version,
        "jti": jti,
    }
    token = pyjwt.encode(claims, secret, algorithm=algorithm)
    return token, exp, jti


def issue_agent_token(
    *,
    agent_id: str,
    secret: str,
    ttl_s: int,
    key_version: int,
    protocol_version: str,
    algorithm: str = "HS256",
) -> tuple[str, datetime, str]:
    """Returns (token, expires_at, jti)."""
    iat = _now()
    exp = iat + timedelta(seconds=ttl_s)
    jti = _new_jti()
    claims = {
        "iss": ISSUER,
        "sub": agent_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "kind": "agent",
        "ver": protocol_version,
        "kver": key_version,
        "jti": jti,
    }
    token = pyjwt.encode(claims, secret, algorithm=algorithm)
    return token, exp, jti


def issue_file_upload_token(
    *,
    user_id: str,
    sha256: str,
    byte_size: int,
    mime_type: str | None,
    secret: str,
    ttl_s: int,
    key_version: int,
    algorithm: str = "HS256",
) -> tuple[str, datetime, str]:
    """Returns (token, expires_at, jti). `sub` = the owning user;
    the upload endpoint enforces the streamed body hashes to
    `sha256` and is no larger than `byte_size`."""
    iat = _now()
    exp = iat + timedelta(seconds=ttl_s)
    jti = _new_jti()
    claims = {
        "iss": ISSUER,
        "sub": user_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "kind": "file-upload",
        "sha": sha256,
        "sz": byte_size,
        "mt": mime_type,
        "kver": key_version,
        "jti": jti,
    }
    token = pyjwt.encode(claims, secret, algorithm=algorithm)
    return token, exp, jti


def issue_file_fetch_token(
    *,
    file_id: str,
    user_id: str,
    secret: str,
    ttl_s: int,
    key_version: int,
    algorithm: str = "HS256",
) -> tuple[str, datetime, str]:
    """Returns (token, expires_at, jti). Bound to one `file_id`
    (claim `fid`) and the owning `user_id` (`sub`)."""
    iat = _now()
    exp = iat + timedelta(seconds=ttl_s)
    jti = _new_jti()
    claims = {
        "iss": ISSUER,
        "sub": user_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "kind": "file-fetch",
        "fid": file_id,
        "kver": key_version,
        "jti": jti,
    }
    token = pyjwt.encode(claims, secret, algorithm=algorithm)
    return token, exp, jti


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Generic verification failure — message is safe to surface to callers."""


def verify_token(
    token: str,
    *,
    secret: str,
    expected_kind: Literal["session", "agent", "file-upload", "file-fetch"],
    revoked_jti: set[str] | None = None,
    key_version: int,
    algorithm: str = "HS256",
) -> dict[str, Any]:
    """Verify signature, expiry, kind claim, and (optionally) revocation.
    Returns claims.

    Raises TokenError on any verification failure. Callers should treat
    all such failures uniformly (e.g. "invalid token") to avoid
    information leaks.

    `revoked_jti` is supported for backward compatibility — callers
    that have a snapshot of revoked jti's pre-loaded can pass it here.
    Modern call sites use `await is_jti_revoked(redis, claims["jti"])`
    AFTER calling this function — that path does
    a single Redis EXISTS lookup against a per-jti key, instead of
    pre-loading the entire revocation set on every request and then
    discarding it.
    """
    try:
        claims = pyjwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "kind", "jti", "kver"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenError("expired") from exc
    except pyjwt.InvalidTokenError as exc:
        raise TokenError(f"invalid: {exc}") from exc

    if claims.get("kind") != expected_kind:
        raise TokenError("wrong_kind")
    # `kver` shape isn't validated by pyjwt — only its presence is
    # ensured by `options={"require": [..., "kver"]}` above. A
    # malformed `kver` (string, dict, or anything non-numeric) would
    # crash `int(...)` with `ValueError`/`TypeError` and surface as
    # an opaque 500 with a stack trace exposing the JWT decode path.
    # Fail closed instead — same `TokenError("invalid")` surface as
    # any other malformed claim.
    try:
        kver = int(claims.get("kver", 0))
    except (ValueError, TypeError) as exc:
        raise TokenError("invalid") from exc
    if kver != key_version:
        raise TokenError("stale_key_version")
    if revoked_jti is not None and claims.get("jti") in revoked_jti:
        raise TokenError("revoked")
    return claims


def verify_agent_token(
    token: str,
    *,
    secret: str,
    revoked_jti: set[str] | None = None,
    key_version: int,
    algorithm: str = "HS256",
) -> AgentPrincipal:
    """Specialised wrapper that returns an AgentPrincipal."""
    claims = verify_token(
        token,
        secret=secret,
        expected_kind="agent",
        revoked_jti=revoked_jti,
        key_version=key_version,
        algorithm=algorithm,
    )
    return AgentPrincipal(
        agent_id=claims["sub"],
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        jti=claims["jti"],
        sdk_protocol_version=str(claims.get("ver", "1")),
    )


def verify_file_upload_token(
    token: str,
    *,
    secret: str,
    key_version: int,
    algorithm: str = "HS256",
) -> FileUploadGrant:
    """Verify a `file-upload` token. Raises TokenError on any
    failure (incl. wrong kind / malformed bound claims). Short-TTL
    by design, so no revocation lookup — the TTL is the control."""
    claims = verify_token(
        token,
        secret=secret,
        expected_kind="file-upload",
        key_version=key_version,
        algorithm=algorithm,
    )
    sha = claims.get("sha")
    sz = claims.get("sz")
    if not isinstance(sha, str) or not sha:
        raise TokenError("invalid")
    if not isinstance(sz, int) or isinstance(sz, bool) or sz < 0:
        raise TokenError("invalid")
    mt = claims.get("mt")
    if mt is not None and not isinstance(mt, str):
        raise TokenError("invalid")
    return FileUploadGrant(
        user_id=claims["sub"],
        sha256=sha,
        byte_size=sz,
        mime_type=mt,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        jti=claims["jti"],
    )


def verify_file_fetch_token(
    token: str,
    *,
    secret: str,
    key_version: int,
    algorithm: str = "HS256",
) -> FileFetchGrant:
    """Verify a `file-fetch` token. Raises TokenError on any
    failure. The caller MUST additionally enforce that the bound
    `file_id` equals the requested path file_id (a key minted for
    file A must not read file B)."""
    claims = verify_token(
        token,
        secret=secret,
        expected_kind="file-fetch",
        key_version=key_version,
        algorithm=algorithm,
    )
    fid = claims.get("fid")
    if not isinstance(fid, str) or not fid:
        raise TokenError("invalid")
    return FileFetchGrant(
        file_id=fid,
        user_id=claims["sub"],
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        jti=claims["jti"],
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def extract_bearer(authorization: str) -> str | None:
    """Parse an `Authorization: Bearer <token>` header value.

    Returns the trimmed token or None if the header is missing,
    not a Bearer credential, or the token is empty. Case-
    insensitive on the `Bearer` keyword to tolerate the small
    variations seen in the wild (`bearer`, `BEARER`).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[len("bearer "):].strip() or None


async def _principal_from_request(request: Request) -> SessionPrincipal:
    token = extract_bearer(request.headers.get("authorization", ""))
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    state = request.app.state.bp
    settings = state.settings
    try:
        claims = verify_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            expected_kind="session",
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    # Revocation check uses per-jti EXISTS.
    # Done AFTER verify_token so we don't pay the Redis lookup for
    # tokens that fail signature/expiry first — a malformed Bearer
    # header doesn't even hit Redis.
    if await is_jti_revoked(state.redis, claims["jti"]):
        raise HTTPException(status_code=401, detail="invalid token")
    level = claims.get("level")
    if not isinstance(level, str) or not is_valid_level(level):
        raise HTTPException(status_code=401, detail="invalid token")
    # Soft-delete boundary check. login / refresh / reset_password
    # already refuse `deleted_at`, but a session JWT outlives a
    # post-issue soft-delete unless we re-check here.
    # Fast path: a fresh entry in LlmService's user-level cache means
    # the user was active when cached. `delete_user` calls
    # `invalidate_user_level` so a soft-delete drops the entry
    # immediately — the next request falls through to DB and is
    # refused below.
    user_id = claims["sub"]
    if state.llm_service.peek_user_level_cached(user_id) is None:
        from bp_router.db import queries  # noqa: PLC0415

        async with state.db_pool.acquire() as conn:
            user = await queries.get_user_by_id(conn, user_id)
        if user is None or user.deleted_at is not None:
            raise HTTPException(status_code=401, detail="invalid token")
    return SessionPrincipal(
        user_id=user_id,
        level=level,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        jti=claims["jti"],
    )


async def require_authenticated(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    """Any valid principal level. Use when the endpoint has no tier gate."""
    return principal


@dataclass(frozen=True)
class FileReadAccess:
    """Resolved authorization for a file download. `via_key_file_id`
    is set iff the caller authenticated with a `file-fetch` key
    (the endpoint MUST then enforce it equals the path file_id);
    None means a full session principal authorised the read."""

    user_id: str
    via_key_file_id: str | None


async def file_read_access(request: Request) -> FileReadAccess:
    """Dual-auth for `GET /v1/files/{file_id}`.

    Tries a `file-fetch` key FIRST (cheap — one local decode, no
    Redis/DB; this is the hot path for agent attachment fetches).
    On any failure (incl. a `session`/`agent` token → wrong_kind)
    falls back to the EXACT existing session-principal resolution
    (`_principal_from_request`) so revocation + soft-delete +
    level checks are byte-for-byte unchanged for UI/admin callers.
    """
    token = extract_bearer(request.headers.get("authorization", ""))
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    state = request.app.state.bp
    settings = state.settings
    try:
        grant = verify_file_fetch_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
        return FileReadAccess(
            user_id=grant.user_id, via_key_file_id=grant.file_id
        )
    except TokenError:
        pass
    principal = await _principal_from_request(request)
    return FileReadAccess(user_id=principal.user_id, via_key_file_id=None)


async def require_admin(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    """Only `admin`. Service principals do NOT pass."""
    if principal.level != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return principal


async def require_service(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    """Only `service`. Use for endpoints meant for automated principals."""
    if principal.level != "service":
        raise HTTPException(status_code=403, detail="service only")
    return principal


def _is_mcp_bridge(principal: SessionPrincipal) -> bool:
    # Exact id + level match — the MCP bridge is a single fixed principal, so
    # this stands in for a per-endpoint capability without a capability system.
    from bp_router.principals import MCP_BRIDGE_USER_ID  # noqa: PLC0415

    return principal.level == "service" and principal.user_id == MCP_BRIDGE_USER_ID


async def require_mcp_bridge(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    """Only the fixed `service_mcp` bridge principal (bridge-write endpoints)."""
    if not _is_mcp_bridge(principal):
        raise HTTPException(status_code=403, detail="mcp bridge service only")
    return principal


async def require_admin_or_mcp_bridge(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    """`admin` (the UI) OR the `service_mcp` bridge — the MCP config reads
    served to both the admin console and the bridge's poll loop."""
    if principal.level != "admin" and not _is_mcp_bridge(principal):
        raise HTTPException(status_code=403, detail="admin or mcp bridge only")
    return principal


def require_tier(max_tier: int) -> Callable[..., Any]:
    """Dependency factory: admit admin, service, and tier0..tierN.

    Use as:

        @router.post(...)
        async def endpoint(
            principal: SessionPrincipal = Depends(require_tier(2)),
        ): ...

    `require_tier(N)` admits any principal whose tier index is ≤ N
    (tier0 most privileged, tierN least). Admin and service satisfy
    every ceiling.
    """
    if max_tier < 0:
        raise ValueError("max_tier must be non-negative")

    async def _dep(
        principal: SessionPrincipal = Depends(_principal_from_request),
    ) -> SessionPrincipal:
        if not level_satisfies_tier(principal.level, max_tier):
            raise HTTPException(
                status_code=403,
                detail=f"tier{max_tier} or stricter required",
            )
        return principal

    return _dep


# ---------------------------------------------------------------------------
# Revocation helpers
# ---------------------------------------------------------------------------


# Redis key prefix for revoked JTIs. Each jti gets its own key with
# TTL = the remaining JWT lifetime. The previous
# implementation kept all jtis in a single SET with a sliding TTL on
# the WHOLE set — every new revocation bumped the TTL forward, so
# expired jtis (whose underlying JWT was past `exp` already) lingered
# in the set indefinitely under continuous traffic. Per-key with EX
# means each jti expires exactly when its JWT does.
_REVOKED_JTI_KEY_PREFIX = "router:revoked_jti:"


def _revoked_jti_key(jti: str) -> str:
    return f"{_REVOKED_JTI_KEY_PREFIX}{jti}"


async def revoke_jti(redis: Any, jti: str, *, ttl_s: int) -> None:
    """Mark a jti as revoked for the next `ttl_s` seconds. After that
    the underlying JWT's `exp` claim makes the token invalid anyway,
    so the revocation entry would be redundant — Redis evicts it
    automatically.

    Callers (logout, change-password) pass `ttl_s` = remaining JWT
    lifetime so the revocation only stays alive as long as the JWT
    itself could be replayed. No-op when Redis isn't configured;
    single-worker deployments accept that revocation is best-effort.
    """
    if redis is None:
        return
    # Per-key SET with EX. Atomic, no race with pipeline ordering,
    # automatically expires on time.
    await redis.set(_revoked_jti_key(jti), "1", ex=ttl_s)


async def is_jti_revoked(redis: Any, jti: str) -> bool:
    """Single-jti revocation check. Returns False when Redis isn't
    configured (revocation unsupported in single-worker deploys) AND
    when the Redis call raises — see the availability tradeoff below.

    Availability tradeoff: a Redis exception fails OPEN (token is
    treated as not revoked) so a Redis blip doesn't take the auth
    path with it. The downside is that a known-revoked JTI passes
    through during the outage. Operators see the degraded mode via
    the `router_redis_health` gauge / `router_redis_fallback_total`
    counter (subsystem="jwt"); see `docs/security.md` for the
    fail-open vs fail-closed discussion.

    Replaces the previous "load every revoked jti into a set then
    check membership" pattern, which made every authenticated
    request pay an `SMEMBERS` round-trip whose result we
    immediately discarded. `EXISTS` on a per-key marker is one
    round-trip and one byte over the wire.
    """
    if redis is None:
        return False
    try:
        out = await redis.exists(_revoked_jti_key(jti))
    except Exception:  # noqa: BLE001
        try:
            from bp_router.observability.metrics import (  # noqa: PLC0415
                redis_fallback_total,
                redis_health,
            )
            redis_fallback_total.labels(subsystem="jwt").inc()
            redis_health.set(0)
        except Exception:  # noqa: BLE001
            pass
        return False
    return bool(out)
