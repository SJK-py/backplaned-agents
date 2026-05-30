"""bp_router.api.admin — Admin-only endpoints (invitations, users, ACL, agents, audit)."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets as _secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from bp_router.acl import (
    Rule,
    TraceStep,
    is_allowed,
    is_valid_pattern,
    is_valid_rule_user_level,
)
from bp_router.db import queries
from bp_router.principals import (
    SERVICE_USER_ID_PREFIX,
    is_valid_level,
    service_user_id_for_agent,
)
from bp_router.quota import (
    BUCKET_PASSWORD_RESET_MINT,
    BUCKET_SERVICE_MINT_REFRESH_TOKEN,
)
from bp_router.security.jwt import (
    SessionPrincipal,
    require_admin,
    require_authenticated,
    require_service,
)
from bp_router.security.passwords import hash_password

router = APIRouter()


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC.

    Postgres `timestamptz` columns interpret naive datetimes in the
    session's `TIMEZONE` setting, which silently shifts results around
    DST boundaries. Treat anything without explicit `tzinfo` as UTC —
    that's the convention every timestamp the admin UI generates
    follows, and FastAPI's default datetime parser accepts ISO-8601
    strings either with or without a timezone suffix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Per-actor dampener for denial-audit rows. A hostile or
# misconfigured service principal hammering an admin mint
# endpoint otherwise writes one hash-chained audit row per
# attempt; each row holds `pg_advisory_xact_lock` and serialises
# the chain, so a 100/s flood blocks legitimate audit writes
# behind the lock queue. The dampener consumes from a per-actor
# bucket; allowed → write audit; denied → drop silently + bump
# `audit_denials_dropped_total{event}`.
#
# Rates intentionally tight: a real admin denies ~1 mint/minute
# on operator error; the 1-per-10s rate with burst 10 absorbs
# that and any reasonable retry storm without blocking the
# legit-flow audit. R4 second-pass review.
_AUDIT_DENIAL_DAMPENER_RATE_PER_S = 0.1
_AUDIT_DENIAL_DAMPENER_BURST = 10

# Levels that confer privilege beyond an ordinary tier user. Per
# `bp_router.principals`, `admin` and `service` both have
# tier_index -1 ("always satisfy any tier ceiling"). The
# `serviced_by` delegated-token-minting mechanism must never let a
# *service* principal mint credentials for — or be granted
# serviced-by over — one of these: a service credential is
# low-trust (embedded in CI / containers / automation), so a path
# from "compromised service principal" to "admin session" is a
# full privilege escalation. Only an admin session may manage
# admin/service accounts; token rotation for those must use an
# admin credential, not a service principal.
_PRIVILEGED_LEVELS = ("admin", "service")


async def _denial_audit_allowed(
    state,  # type: ignore[no-untyped-def]
    actor_user_id: str | None,
    event: str,
) -> bool:
    """Per-actor dampener gate for denial audits.

    Returns True iff this denial-audit row should be written.
    False means the caller should SKIP the `append_audit_event`
    call and proceed with the HTTPException as before. The
    dropped-counter increment is sealed behind try/except so a
    metrics outage never affects business logic.
    """
    bucket_key = (
        f"audit_denial:{event}:{actor_user_id or 'anon'}"
    )
    decision = await state.login_quota.try_consume(
        bucket_key,
        rate_per_s=_AUDIT_DENIAL_DAMPENER_RATE_PER_S,
        burst=_AUDIT_DENIAL_DAMPENER_BURST,
    )
    if decision.allowed:
        return True
    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            audit_denials_dropped_total,
        )
        audit_denials_dropped_total.labels(event=event).inc()
    except Exception:  # noqa: BLE001
        pass
    return False


async def _enforce_per_target_mint_rate_limit(
    *,
    state,  # type: ignore[no-untyped-def]
    actor_user_id: str,
    target_user_id: str,
    bucket_prefix: str,
    rate_per_s: float,
    burst: int,
    audit_event: str,
    error_detail: str,
) -> None:
    """Consume one token from a per-target mint bucket. On
    saturation, write a `<audit_event>` audit row (dampened to
    avoid flooding the hash-chain) and raise HTTP 429 with a
    Retry-After header.

    Folds the duplicate rate-limit shape shared by F8
    `service_mint_refresh_token` and F9 `mint_password_reset_token`.
    Both endpoints want: per-target bucket; deny → 429 + audit row +
    Retry-After; bucket key shape `<prefix>:user:<target_id>`.

    `state.login_quota` is the existing TokenBucket instance the
    pre-extraction code already used (`login_quota` is just the
    naming; it's a general-purpose per-key bucket store).
    """
    bucket_key = f"{bucket_prefix}:user:{target_user_id}"
    decision = await state.login_quota.try_consume(
        bucket_key,
        rate_per_s=rate_per_s,
        burst=burst,
    )
    if decision.allowed:
        return
    retry_after = max(int(decision.retry_after_s + 0.999), 1)
    # Dampen the denial audit so a flood (the same actor pounding
    # the saturated bucket) doesn't push out legitimate audit
    # writes under the hash-chain lock.
    if await _denial_audit_allowed(state, actor_user_id, audit_event):
        async with state.db_pool.acquire() as conn:
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=actor_user_id,
                event=audit_event,
                target_kind="user",
                target_id=target_user_id,
                payload={"retry_after_s": retry_after},
            )
    raise HTTPException(
        status_code=429,
        detail=error_detail,
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class IssueInvitationRequest(BaseModel):
    level: str
    expires_in_s: int = 86_400
    token: str | None = None
    provisions_service_user: bool = False
    """When true, consuming this invitation at `POST /v1/onboard` also
    provisions a co-located `level=service` user (`usr_service_{agent_id}`)
    and returns a refresh token for it alongside the agent JWT. This is a
    higher-privilege invitation (it yields a minting-capable principal),
    so it is admin-only like every invitation and single-use; guard the
    token accordingly."""
    """Optional caller-supplied token plaintext. When set, the router
    stores its hash instead of generating one. Designed for bootstrap
    paths where the operator pre-supplies token values in `.env` so
    the same value can be read by both this endpoint (via the
    bootstrap script) and the agent containers — no router-side
    generation, no paste step.

    Must be at least 32 characters of URL-safe alphabet
    (`[A-Za-z0-9_-]`) to match the entropy of an auto-generated
    token. Pair with an `Idempotency-Key` header so a bootstrap
    re-run with the same token returns the existing row instead
    of erroring on the primary-key collision."""

    @field_validator("level")
    @classmethod
    def _level_grammar(cls, v: str) -> str:
        if not is_valid_level(v):
            raise ValueError("level must be admin | service | tierN")
        return v

    @field_validator("token")
    @classmethod
    def _token_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) < 32:
            raise ValueError("token must be at least 32 characters")
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(
                "token must use URL-safe characters only (A-Z a-z 0-9 - _)"
            )
        return v


class InvitationCreated(BaseModel):
    invitation_token: str
    expires_at: datetime


# TTL for a reprovision invitation. 7 days gives an operator a comfortable
# window to restart the stuck agent after clicking the button (the conventional
# agent invite — `IssueInvitationRequest` — defaults to 24h; recovery wants
# more slack since a human is in the loop).
_REPROVISION_INVITATION_TTL_S = 7 * 86_400


class AgentReprovisioned(BaseModel):
    """Result of resetting an agent to `pending` AND minting a fresh
    invitation it can re-onboard with. `invitation_token` is the plaintext
    token — shown ONCE, never retrievable again."""

    agent_id: str
    status: str
    failed_tasks: int
    invitation_token: str
    expires_at: datetime
    provisions_service_user: bool


@router.post("/invitations", response_model=InvitationCreated, status_code=201)
async def issue_invitation(
    req: IssueInvitationRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
) -> InvitationCreated:
    """Issue a new invitation token.

    Send an `Idempotency-Key` header to make the call retry-safe.
    A network retry with the same key
    returns the EXISTING invitation row instead of creating a
    second valid token (which the operator would otherwise have
    to manually revoke).

    Scope is per-admin: two admins using the same client-side
    idempotency key get independent invitations. Without the
    header, every call creates a fresh token (legacy behavior
    preserved for backward compat).

    Note: the response token field is the original plaintext
    token (only available at issue time). On an idempotent
    retry, we DON'T have the original plaintext — only the
    hash — so the response is a 409 instead. Clients that need
    retry-safety must persist the token from the first call.
    """
    import asyncpg  # noqa: PLC0415

    state = request.app.state.bp
    token = req.token if req.token else _secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=req.expires_in_s)

    # Sentinel for the bootstrap re-run path. A re-run with the same
    # caller-supplied `token` AND `Idempotency-Key` collides on
    # `invitations_pkey` (the token-hash PK) BEFORE the idempotency-key
    # uniqueness constraint can match. Postgres aborts on first error,
    # so we have to deal with this in a fresh transaction after the
    # original one rolls back.
    class _IdempotentPkCollision(Exception):
        pass

    async with state.db_pool.acquire() as conn:
        # Atomic mutation + audit: if the audit append fails (advisory
        # lock contention, JSON encode error, conn reset), the
        # invitation insert MUST roll back. Otherwise we'd ship a live
        # invitation token whose creation isn't in the audit log —
        # silent privilege grant. `append_audit_event` opens its own
        # transaction; nested inside this outer one it becomes a
        # savepoint so a failure aborts both halves cleanly.
        try:
            async with conn.transaction():
                try:
                    await queries.insert_invitation(
                        conn,
                        token_hash=_hash(token),
                        level=req.level,
                        expires_at=expires_at,
                        created_by=principal.user_id,
                        idempotency_key=idempotency_key,
                        provisions_service_user=req.provisions_service_user,
                    )
                except asyncpg.UniqueViolationError as exc:
                    constraint = getattr(exc, "constraint_name", None)
                    if (
                        constraint == "invitations_pkey"
                        and req.token is not None
                        and idempotency_key is not None
                    ):
                        # Bootstrap re-run: same token + same key. Defer
                        # the verify to a fresh txn so we don't try to
                        # work inside an aborted one.
                        raise _IdempotentPkCollision() from exc
                    # Existing idempotency-key collision handling.
                    if (
                        idempotency_key is None
                        or constraint != "invitations_created_by_idempotency_key_uniq"
                    ):
                        raise
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Idempotency-Key {idempotency_key!r} already used "
                            "for an invitation by this admin; reuse the "
                            "original response token (server has only the "
                            "hash, not the plaintext)"
                        ),
                    ) from exc
                await queries.append_audit_event(
                    conn,
                    actor_kind="admin",
                    actor_id=principal.user_id,
                    event="invitation.issued",
                    payload={
                        "level": req.level,
                        "idempotent": idempotency_key is not None,
                        "token_supplied": req.token is not None,
                        "provisions_service_user": req.provisions_service_user,
                    },
                )
        except _IdempotentPkCollision:
            # Re-verify in a fresh transaction. The (admin,
            # idempotency_key, level) trio MUST match the stored row;
            # otherwise we'd let a colliding caller probe for an
            # existing hash. expires_at is taken from the stored row
            # (not the request) — the bootstrap script gets the
            # canonical answer it would have gotten on first run.
            async with conn.transaction():
                existing = await queries.get_invitation(conn, _hash(token))
                if (
                    existing is None
                    or existing.created_by != principal.user_id
                    or existing.idempotency_key != idempotency_key
                    or existing.level != req.level
                    or existing.provisions_service_user
                    != req.provisions_service_user
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "token already exists with different "
                            "(admin, level, idempotency_key, "
                            "provisions_service_user) — refusing "
                            "to overwrite"
                        ),
                    ) from None
                expires_at = existing.expires_at

    return InvitationCreated(invitation_token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


_USER_ID_RE = re.compile(r"^usr_[A-Za-z0-9_-]{8,128}$")


class CreateUserRequest(BaseModel):
    email: EmailStr
    level: str
    initial_password: str | None = None
    user_id: str | None = None
    """Optional caller-supplied user_id. Must match
    `usr_[A-Za-z0-9_-]{8,128}`. Useful for bootstrap scripts that
    need to wire downstream config (e.g. `ROUTER_*_USER_ID` env vars
    passed to agent containers) before the user is actually
    created. Auto-generated when omitted."""

    initial_refresh_token: str | None = None
    """Optional pre-seeded refresh token. When set, the router inserts
    a refresh-token row alongside the user creation in the same
    transaction. The plaintext is owned by the caller (typically a
    bootstrap script that distributes it to agent containers via env
    vars); the router stores only the hash. Must be at least 32
    URL-safe characters to match the entropy of an auto-generated
    token."""

    serviced_by: list[str] | None = None
    """Optional initial `serviced_by` list (F8). Each entry must point
    at a user with `level="service"`; validated app-side inside the
    create transaction. The new user starts with no service
    principals authorised to mint credentials (default-deny) when
    omitted or empty."""

    @field_validator("level")
    @classmethod
    def _level_grammar(cls, v: str) -> str:
        if not is_valid_level(v):
            raise ValueError("level must be admin | service | tierN")
        return v

    @field_validator("user_id")
    @classmethod
    def _user_id_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _USER_ID_RE.match(v):
            raise ValueError(
                "user_id must match usr_[A-Za-z0-9_-]{8,128}"
            )
        if v.startswith(SERVICE_USER_ID_PREFIX):
            raise ValueError(
                f"user_id prefix {SERVICE_USER_ID_PREFIX!r} is reserved for "
                "service principals provisioned at agent onboarding"
            )
        return v

    @field_validator("initial_refresh_token")
    @classmethod
    def _refresh_token_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) < 32:
            raise ValueError("initial_refresh_token must be at least 32 characters")
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(
                "initial_refresh_token must use URL-safe characters only "
                "(A-Z a-z 0-9 - _)"
            )
        return v


class UpdateUserRequest(BaseModel):
    level: str | None = None
    suspended: bool | None = None

    @field_validator("level")
    @classmethod
    def _level_grammar(cls, v: str | None) -> str | None:
        if v is not None and not is_valid_level(v):
            raise ValueError("level must be admin | service | tierN")
        return v


class UserView(BaseModel):
    user_id: str
    email: str | None = None
    level: str
    auth_kind: str
    created_at: datetime
    suspended_at: datetime | None = None
    deleted_at: datetime | None = None
    serviced_by: list[str] = []


def _user_to_view(row) -> UserView:  # type: ignore[no-untyped-def]
    return UserView(
        user_id=row.user_id,
        email=row.email,
        level=row.level,
        auth_kind=row.auth_kind,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
        serviced_by=list(row.serviced_by or []),
        suspended_at=row.suspended_at,
    )


@router.post("/users", status_code=201, response_model=UserView)
async def create_user(
    req: CreateUserRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> UserView:
    state = request.app.state.bp
    settings = state.settings
    auth_kind = "password" if req.initial_password else "api_key"
    auth_secret_hash = hash_password(req.initial_password) if req.initial_password else None

    async with state.db_pool.acquire() as conn:
        existing = await queries.get_user_by_email(conn, req.email)
        if existing is not None:
            raise HTTPException(status_code=409, detail="email already registered")

        # Pre-check caller-supplied user_id for collision. The
        # transaction below would fail on a `users_pkey` violation
        # anyway, but a typed 409 is friendlier than a 500 from a
        # bubbled-up UniqueViolation.
        if req.user_id is not None:
            existing_id = await queries.get_user_by_id(conn, req.user_id)
            if existing_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"user_id {req.user_id!r} already exists",
                )

        # Validate caller-supplied serviced_by entries: each MUST
        # point at a level=service user. App-side enforcement of the
        # invariant Postgres can't express declaratively.
        if req.serviced_by:
            for svc_id in req.serviced_by:
                svc = await queries.get_user_by_id(conn, svc_id)
                if svc is None:
                    raise HTTPException(
                        404, f"service user {svc_id!r} not found"
                    )
                if svc.level != "service":
                    raise HTTPException(
                        400,
                        f"user {svc_id!r} has level={svc.level!r}; "
                        "only level=service users can be entries in serviced_by",
                    )

        # Atomic insert + (optional refresh-token seed) + audit.
        async with conn.transaction():
            user = await queries.insert_user(
                conn,
                user_id=req.user_id,
                email=req.email,
                level=req.level,
                auth_kind=auth_kind,
                auth_secret_hash=auth_secret_hash,
                serviced_by=req.serviced_by or [],
            )
            if req.initial_refresh_token is not None:
                new_expires = _now() + timedelta(
                    seconds=settings.refresh_token_ttl_s
                )
                await queries.insert_refresh_token(
                    conn,
                    token_hash=_hash(req.initial_refresh_token),
                    user_id=user.user_id,
                    expires_at=new_expires,
                )
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="user.created",
                target_kind="user",
                target_id=user.user_id,
                payload={
                    "level": req.level,
                    "user_id_supplied": req.user_id is not None,
                    "refresh_token_seeded": req.initial_refresh_token is not None,
                    "serviced_by": req.serviced_by or [],
                },
            )
    return _user_to_view(user)


@router.get("/users", response_model=list[UserView])
async def list_users(
    request: Request,
    level: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: SessionPrincipal = Depends(require_admin),
) -> list[UserView]:
    if level is not None and not is_valid_level(level):
        raise HTTPException(status_code=400, detail="invalid level filter")
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_users(
            conn,
            level=level,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )
    return [_user_to_view(r) for r in rows]


@router.get("/users/{user_id}", response_model=UserView)
async def get_user(
    user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> UserView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        user = await queries.get_user_by_id(conn, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _user_to_view(user)


# Whitelist of columns the PATCH endpoint may set. The dynamic SET clause
# below interpolates only names from this set — never user-supplied keys —
# so SQL injection via column name is impossible. Values are always bound
# via $-parameters.
_USER_PATCHABLE_COLUMNS = frozenset({"level", "suspended_at"})


@router.patch("/users/{user_id}", response_model=UserView)
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> UserView:
    state = request.app.state.bp
    fields: list[str] = []
    values: list[Any] = []
    # Each transition emits its own audit event. Combined PATCHes
    # produce one event per change so the audit log keeps the
    # narrative granular ("level_changed" + "suspended" rather than
    # one ambiguous "updated").
    audit_events: list[tuple[str, dict[str, Any]]] = []

    if req.level is not None:
        fields.append("level")
        values.append(req.level)
        audit_events.append(("user.level_changed", {"level": req.level}))
    if req.suspended is True:
        fields.append("suspended_at")
        values.append(_now())
        audit_events.append(("user.suspended", {}))
    elif req.suspended is False:
        fields.append("suspended_at")
        values.append(None)
        audit_events.append(("user.unsuspended", {}))

    if not fields:
        async with state.db_pool.acquire() as conn:
            existing = await queries.get_user_by_id(conn, user_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="user not found")
        return _user_to_view(existing)

    # Defense-in-depth: every name we interpolate must be in the
    # allowlist. The code paths above only ever produce names from
    # `_USER_PATCHABLE_COLUMNS`, so this branch fires only on a future
    # maintainer adding a request-model field without updating the
    # allowlist — a real internal bug, not caller error. Surface it
    # as a structured 500 rather than a bare `RuntimeError` so the
    # client sees a clean error envelope and the operator's metrics
    # see a 500-coded response. Use `assert`-free
    # code because `python -O` strips asserts.
    bad = [name for name in fields if name not in _USER_PATCHABLE_COLUMNS]
    if bad:
        raise HTTPException(
            status_code=500,
            detail="internal: user column allowlist drift",
        )

    set_clause = ", ".join(f"{name} = ${i+2}" for i, name in enumerate(fields))
    sql = f"UPDATE users SET {set_clause} WHERE user_id = $1 RETURNING *"

    async with state.db_pool.acquire() as conn:
        # Atomic update + audit. The 404 raise
        # below happens BEFORE entering the transaction so it short-
        # circuits without needing rollback.
        async with conn.transaction():
            row = await conn.fetchrow(sql, user_id, *values)
            if row is None:
                raise HTTPException(status_code=404, detail="user not found")
            for event, payload in audit_events:
                await queries.append_audit_event(
                    conn,
                    actor_kind="admin",
                    actor_id=principal.user_id,
                    event=event,
                    target_kind="user",
                    target_id=user_id,
                    payload=payload,
                )
    # If the level changed OR the user was suspended/unsuspended, drop
    # the LLM service's cached level so a demotion / suspension takes
    # effect immediately on the next call rather than waiting out the
    # 60s TTL. (Suspended users return None from resolve_user_level,
    # which the tier gate then denies on every preset other than `*`.)
    cache_invalidating = {"user.level_changed", "user.suspended", "user.unsuspended"}
    if any(event in cache_invalidating for event, _ in audit_events):
        try:
            state.llm_service.invalidate_user_level(user_id)
        except AttributeError:
            pass
    from bp_router.db.models import UserRow  # noqa: PLC0415
    return _user_to_view(UserRow.model_validate(dict(row)))


# ---------------------------------------------------------------------------
# F8: users.serviced_by — service-principal credential-minting grants
# ---------------------------------------------------------------------------


class ServiceMintedRefreshToken(BaseModel):
    refresh_token: str
    expires_at: datetime
    target_user_id: str


@router.post(
    "/users/{target_user_id}/refresh-tokens",
    response_model=ServiceMintedRefreshToken,
    status_code=201,
)
async def service_mint_refresh_token(
    target_user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_service),
) -> ServiceMintedRefreshToken:
    """Mint a refresh token for `target_user_id` on behalf of a
    service principal.

    Authorised only if the caller's `user_id` is in
    `target_user.serviced_by`. On miss → 403 + audit
    `auth.refresh_token_mint_denied { reason: "not_serviced_by" }`.

    The plaintext is returned exactly once; the router persists only
    the SHA-256 hash. Refresh-token TTL follows
    `Settings.refresh_token_ttl_s` — no separate knob.
    """
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool
    async with pool.acquire() as conn:
        target = await queries.get_user_by_id(conn, target_user_id)
        if target is None:
            raise HTTPException(404, "user not found")
        if target.suspended_at is not None:
            raise HTTPException(409, "target user is suspended")
        if target.deleted_at is not None:
            raise HTTPException(410, "target user has been deleted")
        # Privilege boundary: this endpoint is `require_service`, so
        # the caller is ALWAYS a service principal. A service
        # principal must never be able to mint a refresh token for
        # an admin/service target — that token redeems at
        # `/v1/auth/refresh` into a session at the target's level
        # (auth.py), turning a low-trust service credential into an
        # admin session. Refuse regardless of `serviced_by` (a stale
        # or mis-granted entry must not be sufficient).
        if target.level in _PRIVILEGED_LEVELS:
            if await _denial_audit_allowed(
                state, principal.user_id, "auth.refresh_token_mint_denied"
            ):
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=principal.user_id,
                    event="auth.refresh_token_mint_denied",
                    target_kind="user", target_id=target_user_id,
                    payload={"reason": "privileged_target"},
                )
            raise HTTPException(
                403,
                "service principals may not mint tokens for "
                "admin/service users",
            )
        if principal.user_id not in target.serviced_by:
            if await _denial_audit_allowed(
                state, principal.user_id, "auth.refresh_token_mint_denied"
            ):
                await queries.append_audit_event(
                    conn, actor_kind="user", actor_id=principal.user_id,
                    event="auth.refresh_token_mint_denied",
                    target_kind="user", target_id=target_user_id,
                    payload={"reason": "not_serviced_by"},
                )
            raise HTTPException(403, "not authorized to service this user")

    await _enforce_per_target_mint_rate_limit(
        state=state,
        actor_user_id=principal.user_id,
        target_user_id=target_user_id,
        bucket_prefix=BUCKET_SERVICE_MINT_REFRESH_TOKEN,
        rate_per_s=settings.service_mint_refresh_token_rate_limit_per_target_per_s,
        burst=settings.service_mint_refresh_token_rate_limit_per_target_burst,
        audit_event="auth.refresh_token_service_mint_rate_limited",
        error_detail="too many refresh-token mints for this user; retry later",
    )

    token = _secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=settings.refresh_token_ttl_s)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await queries.insert_refresh_token(
                conn,
                token_hash=_hash(token),
                user_id=target_user_id,
                expires_at=expires_at,
            )
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=principal.user_id,
                event="auth.refresh_token_service_minted",
                target_kind="user", target_id=target_user_id,
                payload={"expires_at": expires_at.isoformat()},
            )
    return ServiceMintedRefreshToken(
        refresh_token=token,
        expires_at=expires_at,
        target_user_id=target_user_id,
    )


@router.put(
    "/users/{target_user_id}/serviced-by/{service_user_id}", status_code=204
)
async def grant_serviced_by(
    target_user_id: str,
    service_user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    """Idempotently append `service_user_id` to
    `target_user.serviced_by`. The grantee must have `level="service"`
    — app-side enforcement of the F8.2 invariant Postgres can't
    declaratively express."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        svc = await queries.get_user_by_id(conn, service_user_id)
        if svc is None:
            raise HTTPException(404, "service user not found")
        if svc.level != "service":
            raise HTTPException(
                400,
                f"user {service_user_id!r} has level={svc.level!r}; "
                "only level=service users can be entries in serviced_by",
            )
        target = await queries.get_user_by_id(conn, target_user_id)
        if target is None:
            raise HTTPException(404, "target user not found")
        # Defense-in-depth: refuse to create the dangerous config in
        # the first place. `serviced_by` lets the service principal
        # mint refresh / password-reset tokens for the target; over
        # an admin/service target that is a service→admin escalation
        # primitive. The mint endpoints also refuse this at use time,
        # but blocking the grant stops the footgun being set up at
        # all. Automation that must rotate an admin/service account's
        # tokens has to use an admin credential, not a service
        # principal.
        if target.level in _PRIVILEGED_LEVELS:
            raise HTTPException(
                400,
                f"target user {target_user_id!r} has "
                f"level={target.level!r}; serviced_by may not be "
                "granted over admin/service users (would let a "
                "service principal mint their credentials)",
            )
        async with conn.transaction():
            changed = await queries.append_to_serviced_by(
                conn, target_user_id, service_user_id,
            )
            if changed:
                await queries.append_audit_event(
                    conn, actor_kind="admin", actor_id=principal.user_id,
                    event="user.serviced_by_granted",
                    target_kind="user", target_id=target_user_id,
                    payload={"service_user_id": service_user_id},
                )


@router.delete(
    "/users/{target_user_id}/serviced-by/{service_user_id}", status_code=204
)
async def revoke_serviced_by(
    target_user_id: str,
    service_user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    """Remove `service_user_id` from `target_user.serviced_by`.

    IMPORTANT: this does NOT invalidate refresh tokens the service
    principal has already minted for the target user. Admin must
    follow up with `DELETE /v1/admin/users/{target_user_id}
    /refresh-tokens` to revoke active sessions.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            changed = await queries.remove_from_serviced_by(
                conn, target_user_id, service_user_id,
            )
            if changed:
                await queries.append_audit_event(
                    conn, actor_kind="admin", actor_id=principal.user_id,
                    event="user.serviced_by_revoked",
                    target_kind="user", target_id=target_user_id,
                    payload={"service_user_id": service_user_id},
                )


# ---------------------------------------------------------------------------
# F9: password-reset token mint
# ---------------------------------------------------------------------------


class MintedPasswordResetToken(BaseModel):
    reset_token: str
    expires_at: datetime
    target_user_id: str


@router.post(
    "/users/{target_user_id}/password-reset-tokens",
    response_model=MintedPasswordResetToken,
    status_code=201,
)
async def mint_password_reset_token(
    target_user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> MintedPasswordResetToken:
    """Mint a single-use password-reset token for `target_user_id`.

    Authorisation:
      * admin caller → always allowed.
      * service caller → allowed iff caller's user_id is in
        `target_user.serviced_by` (same gate as F8.3's
        service-mint refresh-token endpoint).
      * any other level → 403.

    The token expires in `password_reset_token_ttl_s` (default 10
    min). Per-target rate-limit is intentionally tight (~3/h
    default) to defend the table against a compromised service
    principal. The plaintext is returned exactly once.
    """
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    async with pool.acquire() as conn:
        target = await queries.get_user_by_id(conn, target_user_id)
        if target is None:
            raise HTTPException(404, "user not found")
        if target.suspended_at is not None:
            raise HTTPException(409, "target user is suspended")
        if target.deleted_at is not None:
            raise HTTPException(410, "target user has been deleted")

        if principal.level == "admin":
            pass
        elif principal.level == "service":
            # Privilege boundary: a service principal must never
            # mint a password-reset token for an admin/service
            # target — redeeming it at `/v1/auth/reset-password`
            # then logging in yields a session at the target's
            # level, escalating a low-trust service credential to
            # admin. Refuse regardless of `serviced_by`. (An admin
            # caller still passes via the branch above — managing
            # admin/service accounts is legitimate admin action.)
            if target.level in _PRIVILEGED_LEVELS:
                if await _denial_audit_allowed(
                    state,
                    principal.user_id,
                    "auth.password_reset_mint_denied",
                ):
                    await queries.append_audit_event(
                        conn, actor_kind="user", actor_id=principal.user_id,
                        event="auth.password_reset_mint_denied",
                        target_kind="user", target_id=target_user_id,
                        payload={"reason": "privileged_target"},
                    )
                raise HTTPException(
                    403,
                    "service principals may not mint tokens for "
                    "admin/service users",
                )
            if principal.user_id not in target.serviced_by:
                if await _denial_audit_allowed(
                    state,
                    principal.user_id,
                    "auth.password_reset_mint_denied",
                ):
                    await queries.append_audit_event(
                        conn, actor_kind="user", actor_id=principal.user_id,
                        event="auth.password_reset_mint_denied",
                        target_kind="user", target_id=target_user_id,
                        payload={"reason": "not_serviced_by"},
                    )
                raise HTTPException(
                    403, "not authorized to mint for this user"
                )
        else:
            raise HTTPException(
                403, "must be admin or service principal"
            )

    await _enforce_per_target_mint_rate_limit(
        state=state,
        actor_user_id=principal.user_id,
        target_user_id=target_user_id,
        bucket_prefix=BUCKET_PASSWORD_RESET_MINT,
        rate_per_s=settings.password_reset_mint_rate_limit_per_target_per_s,
        burst=settings.password_reset_mint_rate_limit_per_target_burst,
        audit_event="auth.password_reset_mint_rate_limited",
        error_detail="too many password-reset mints for this user; retry later",
    )

    token = _secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=settings.password_reset_token_ttl_s)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await queries.insert_password_reset_token(
                conn,
                token_hash=_hash(token),
                user_id=target_user_id,
                expires_at=expires_at,
                created_by=principal.user_id,
            )
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=principal.user_id,
                event="auth.password_reset_token_minted",
                target_kind="user", target_id=target_user_id,
                payload={"expires_at": expires_at.isoformat()},
            )
    return MintedPasswordResetToken(
        reset_token=token,
        expires_at=expires_at,
        target_user_id=target_user_id,
    )


@router.delete("/users/{target_user_id}/refresh-tokens", status_code=204)
async def revoke_user_refresh_tokens(
    target_user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    """Delete every refresh token for `target_user_id`. Forces
    re-login on every device on the next refresh attempt. Pair with
    `DELETE /v1/admin/users/{id}/serviced-by/{service_id}` to fully
    cut off a compromised service principal's access to a user.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            target = await queries.get_user_by_id(conn, target_user_id)
            if target is None:
                raise HTTPException(404, "user not found")
            deleted = await queries.delete_user_refresh_tokens(conn, target_user_id)
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="user.refresh_tokens_revoked",
                target_kind="user", target_id=target_user_id,
                payload={"deleted_count": deleted},
            )


@router.delete("/users/{target_user_id}", status_code=204)
async def delete_user(
    target_user_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    """Soft-delete a user. Terminal — distinct from suspend, which
    is reversible.

    Pipeline (inside one transaction):
      1. `users.deleted_at = now()` if NULL. Idempotent: a second
         call on an already-deleted user is a no-op (still 204,
         no second audit row).
      2. Delete every refresh token (forced logout).
      3. Delete every pending password-reset token.
      4. Run the F8 `serviced_by` sweep to remove this user from
         every other user's credential-mint trust list.

    The user ROW stays in the table — `actor_id` foreign-key
    references from `audit_events`, `tasks`, etc. remain valid so
    historical attribution doesn't dangle. Authentication paths
    (login, refresh, change_password) refuse the user on the
    `deleted_at` check; the admin user list hides them by default
    (pass `?include_deleted=true` to surface).

    Refuses to delete the admin's own user (404 on the lookup
    path or 400 here) — operators shouldn't be able to lock
    themselves out via a misclick.
    """
    if target_user_id == principal.user_id:
        raise HTTPException(
            400, "cannot delete your own user — ask another admin"
        )
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            result = await queries.soft_delete_user(conn, target_user_id)
            if result is None:
                raise HTTPException(404, "user not found")
            if result["was_already_deleted"]:
                # Idempotent — return 204 without a second audit row.
                return
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="user.deleted",
                target_kind="user", target_id=target_user_id,
                payload={
                    "refresh_tokens_deleted": result["refresh_tokens_deleted"],
                    "reset_tokens_deleted": result["reset_tokens_deleted"],
                    "serviced_by_sweep_count": result["serviced_by_sweep_count"],
                },
            )
    # Drop the cached level so LLM tier-gates (and the
    # `_principal_from_request` cache short-circuit) refuse the user
    # immediately, not after the 10-min TTL. Outside the DB
    # transaction so a rollback doesn't leave a stale-invalidate
    # side effect.
    try:
        state.llm_service.invalidate_user_level(target_user_id)
    except AttributeError:
        pass

    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            users_soft_deleted_total,
        )
        users_soft_deleted_total.inc()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Service-principal discovery: sessions of the caller's serviced users
# ---------------------------------------------------------------------------


class ServicedSessionView(BaseModel):
    user_id: str
    session_id: str
    external_id: str | None = None
    channel: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None


@router.get("/serviced-sessions", response_model=list[ServicedSessionView])
async def serviced_sessions(
    request: Request,
    principal: SessionPrincipal = Depends(require_service),
    channel: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[ServicedSessionView]:
    """List sessions of users the calling service principal services.

    A channel/gateway uses this to learn which of its serviced users have
    been provisioned by admin approval — approval opens a session whose
    `metadata.external_id` is the channel-native id (e.g. the Telegram
    chat). The channel reconciles `(external_id → user_id, session_id)`
    into its own store from the result. Strictly scoped to the caller's
    serviced users (`require_service` + `serviced_by` filter); `since`
    cursors on `opened_at` for incremental polling.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_serviced_sessions(
            conn,
            service_user_id=principal.user_id,
            channel=channel,
            since=since,
            limit=limit,
        )
    out: list[ServicedSessionView] = []
    for r in rows:
        md = r["metadata"] or {}
        out.append(
            ServicedSessionView(
                user_id=r["user_id"],
                session_id=r["session_id"],
                external_id=md.get("external_id"),
                channel=md.get("kind"),
                opened_at=r["opened_at"],
                closed_at=r["closed_at"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# F7: pending user registrations — admin approve/reject queue
# ---------------------------------------------------------------------------


class PendingRegistrationView(BaseModel):
    registration_id: str
    channel: str
    external_id: str
    display_name: str | None = None
    requested_email: str | None = None
    metadata: dict[str, Any] = {}
    requested_at: datetime
    attempts: int
    last_attempt_at: datetime
    submitted_by_service_user_id: str | None = None


def _registration_to_view(row) -> PendingRegistrationView:  # type: ignore[no-untyped-def]
    return PendingRegistrationView(
        registration_id=row.registration_id,
        channel=row.channel,
        external_id=row.external_id,
        display_name=row.display_name,
        requested_email=row.requested_email,
        metadata=row.metadata,
        requested_at=row.requested_at,
        attempts=row.attempts,
        last_attempt_at=row.last_attempt_at,
        submitted_by_service_user_id=row.submitted_by_service_user_id,
    )


@router.get("/registrations", response_model=list[PendingRegistrationView])
async def list_registrations(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
    channel: str | None = Query(default=None),
    submitted_by_service_user_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[PendingRegistrationView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_pending_registrations(
            conn,
            channel=channel,
            submitted_by_service_user_id=submitted_by_service_user_id,
            limit=limit,
            offset=offset,
        )
    return [_registration_to_view(r) for r in rows]


@router.get("/registrations/{registration_id}", response_model=PendingRegistrationView)
async def get_registration(
    registration_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> PendingRegistrationView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.get_pending_registration(conn, registration_id)
    if row is None:
        raise HTTPException(404, "registration not found")
    return _registration_to_view(row)


class ApproveRegistrationRequest(BaseModel):
    email: EmailStr | None = None
    initial_password: str | None = None
    level: str = "tier0"
    label: str | None = None

    @field_validator("level")
    @classmethod
    def _level_grammar(cls, v: str) -> str:
        if not is_valid_level(v):
            raise ValueError("level must be admin | service | tierN")
        return v


class ApproveRegistrationResponse(BaseModel):
    registration_id: str
    user_id: str
    email: str
    level: str
    channel: str
    external_id: str
    session_id: str
    initial_password: str


@router.post(
    "/registrations/{registration_id}/approve",
    response_model=ApproveRegistrationResponse,
)
async def approve_registration(
    registration_id: str,
    req: ApproveRegistrationRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> ApproveRegistrationResponse:
    """Approve a pending registration: create the user (auto-granting
    `serviced_by` to the submitter when there was one), open the
    initial session, delete the pending row. Returns the initial
    password exactly once."""
    state = request.app.state.bp
    pool = state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE so two concurrent admins can't both approve.
            pending_row = await conn.fetchrow(
                """
                SELECT registration_id::text, channel, external_id,
                       display_name, requested_email, metadata,
                       requested_at, attempts, last_attempt_at,
                       submitted_by_service_user_id
                FROM pending_user_registrations
                WHERE registration_id = $1::uuid
                FOR UPDATE
                """,
                registration_id,
            )
            if pending_row is None:
                raise HTTPException(404, "registration not found")
            pending = dict(pending_row)
            email = req.email or pending["requested_email"]
            if not email:
                raise HTTPException(
                    422,
                    "email required (no requested_email on the pending row)",
                )
            existing = await queries.get_user_by_email(conn, email)
            if existing is not None:
                raise HTTPException(
                    409, f"user with email {email!r} already exists"
                )
            password = req.initial_password or _secrets.token_urlsafe(16)
            submitter = pending["submitted_by_service_user_id"]
            user = await queries.insert_user(
                conn,
                email=email,
                level=req.level,
                auth_kind="password",
                auth_secret_hash=hash_password(password),
                serviced_by=[submitter] if submitter else [],
            )
            label = req.label or (
                f"{pending['channel']} · "
                f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
            )
            session = await queries.Scope.user(conn, user.user_id).open_session(
                metadata={
                    "kind": pending["channel"],
                    "label": label,
                    "external_id": pending["external_id"],
                },
            )
            await conn.execute(
                "DELETE FROM pending_user_registrations "
                "WHERE registration_id = $1::uuid",
                registration_id,
            )
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="registration.approved",
                target_kind="user", target_id=user.user_id,
                payload={
                    "channel": pending["channel"],
                    "external_id": pending["external_id"],
                    "email": email,
                    "level": req.level,
                    "session_id": session.session_id,
                    "auto_serviced_by": submitter,
                },
            )
    return ApproveRegistrationResponse(
        registration_id=registration_id,
        user_id=user.user_id,
        email=email,
        level=req.level,
        channel=pending["channel"],
        external_id=pending["external_id"],
        session_id=session.session_id,
        initial_password=password,
    )


@router.post(
    "/registrations/{registration_id}/reject", status_code=204
)
async def reject_registration(
    registration_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            pending = await queries.get_pending_registration(conn, registration_id)
            if pending is None:
                raise HTTPException(404, "registration not found")
            await queries.delete_pending_registration(conn, registration_id)
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="registration.rejected",
                target_kind="registration", target_id=registration_id,
                payload={
                    "channel": pending.channel,
                    "external_id": pending.external_id,
                },
            )


# ---------------------------------------------------------------------------
# ACL — firewall rule list (see docs/acl.md)
# ---------------------------------------------------------------------------


class RuleView(BaseModel):
    rule_id: str
    ord: int
    name: str | None = None
    description: str | None = None
    effect: str
    user_level: str
    caller_pattern: str
    callee_pattern: str
    created_at: datetime


def _row_to_rule_view(row) -> RuleView:  # type: ignore[no-untyped-def]
    return RuleView(
        rule_id=row.rule_id,
        ord=row.ord,
        name=row.name,
        description=row.description,
        effect=row.effect,
        user_level=row.user_level,
        caller_pattern=row.caller_pattern,
        callee_pattern=row.callee_pattern,
        created_at=row.created_at,
    )


def _row_to_rule(row) -> Rule:  # type: ignore[no-untyped-def]
    return Rule(
        rule_id=row.rule_id,
        ord=row.ord,
        name=row.name,
        description=row.description,
        effect=row.effect,  # type: ignore[arg-type]
        user_level=row.user_level,
        caller_pattern=row.caller_pattern,
        callee_pattern=row.callee_pattern,
    )


class CreateRuleRequest(BaseModel):
    ord: int = Field(ge=0)
    name: str | None = None
    description: str | None = None
    effect: str
    user_level: str
    caller_pattern: str
    callee_pattern: str

    @field_validator("effect")
    @classmethod
    def _eff(cls, v: str) -> str:
        if v not in ("allow", "deny"):
            raise ValueError("effect must be allow|deny")
        return v

    @field_validator("user_level")
    @classmethod
    def _lvl(cls, v: str) -> str:
        if not is_valid_rule_user_level(v):
            raise ValueError("user_level must be * | admin | service | tierN")
        return v

    @field_validator("caller_pattern", "callee_pattern")
    @classmethod
    def _pat(cls, v: str) -> str:
        if not is_valid_pattern(v):
            raise ValueError("pattern must be <group>/<cap> or @<agent_id>")
        return v


class UpdateRuleRequest(BaseModel):
    ord: int | None = Field(default=None, ge=0)
    name: str | None = None
    description: str | None = None
    effect: str | None = None
    user_level: str | None = None
    caller_pattern: str | None = None
    callee_pattern: str | None = None

    @field_validator("effect")
    @classmethod
    def _eff(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("allow", "deny"):
            raise ValueError("effect must be allow|deny")
        return v

    @field_validator("user_level")
    @classmethod
    def _lvl(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not is_valid_rule_user_level(v):
            raise ValueError("user_level must be * | admin | service | tierN")
        return v

    @field_validator("caller_pattern", "callee_pattern")
    @classmethod
    def _pat(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not is_valid_pattern(v):
            raise ValueError("pattern must be <group>/<cap> or @<agent_id>")
        return v


class ReplaceRulesRequest(BaseModel):
    rules: list[CreateRuleRequest]


class ReorderRequest(BaseModel):
    new_ords: dict[str, int]


class SimulateRequest(BaseModel):
    caller_id: str
    callee_id: str
    user_level: str

    @field_validator("user_level")
    @classmethod
    def _lvl(cls, v: str) -> str:
        if not is_valid_level(v):
            raise ValueError("user_level must be admin | service | tierN")
        return v


class SimulateResponse(BaseModel):
    allow: bool
    rule_name: str | None
    trace: list[dict[str, Any]]


# Process-local lock serialising `_apply_rule_change`.
# Without this, two concurrent admins running mutating
# endpoints can interleave their DB-read + in-memory rule replace:
#   t0  admin A commits patch X
#   t1  admin B commits patch Y
#   t2  admin B's _apply_rule_change reads (sees X+Y), replaces cache
#   t3  admin A's _apply_rule_change reads (sees X+Y), replaces cache
# Final state is correct, but the OPPOSITE interleave (B reloads
# before B commits, etc.) leaves the cache lagging the DB until the
# next mutation. The single asyncio.Lock serialises the read+replace
# so the cache always reflects the latest commit.
#
# `RuleSet.replace` itself is already safe — `sorted(rules, key=...)`
# returns a new list and the attribute assignment is atomic in CPython.
# The lock is purely about the read-replace pair, not the rebind.
_apply_rule_change_lock = asyncio.Lock()

# Stable bigint key for `pg_advisory_xact_lock`. Used to extend the
# in-process serialisation above across multiple FastAPI workers
# that share a Postgres pool. Without this, a
# multi-worker deployment loses the read+replace serialisation as
# soon as the two contending coroutines live in different
# processes. The value is arbitrary-but-stable; picked from the
# top of the int64 range so it doesn't collide with anything else
# the codebase might advisory-lock in future. Encodes "ACL\0RELD"
# in ASCII for grep-ability.
_APPLY_RULE_CHANGE_PG_LOCK_KEY = 0x41434C0052454C44


async def _apply_rule_change(state) -> int:  # type: ignore[no-untyped-def]
    """Reload `state.rules` from `acl_rules`, then push `CatalogUpdate`
    to every connected agent so their cached catalog reflects any
    `callable_user_levels` shift.

    Serialised at TWO levels:

      - In-process: `_apply_rule_change_lock` (asyncio.Lock) so
        coroutines on the SAME worker can't interleave the
        read+replace.
      - Cross-process: `pg_advisory_xact_lock` so coroutines on
        DIFFERENT workers (multi-worker FastAPI deployment) also
        serialise on the same logical operation. The lock is held
        for the duration of the read transaction and auto-releases
        on commit, so the in-memory cache update happens once the
        lock is gone — the lock's only job is bounding the DB-read
        ordering across workers.
    """
    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415

    async with _apply_rule_change_lock:
        async with state.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _APPLY_RULE_CHANGE_PG_LOCK_KEY,
                )
                rows = await queries.list_acl_rules(conn)
        state.rules.replace([_row_to_rule(r) for r in rows])
        await push_catalog_update_to_all(state)
    return len(rows)


@router.get("/acl/rules", response_model=list[RuleView])
async def list_rules(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[RuleView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_acl_rules(conn)
    return [_row_to_rule_view(r) for r in rows]


@router.put("/acl/rules", response_model=list[RuleView])
async def replace_rules(
    req: ReplaceRulesRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[RuleView]:
    state = request.app.state.bp
    payload = [r.model_dump() for r in req.rules]
    async with state.db_pool.acquire() as conn:
        # Atomic rule swap + audit. Without
        # this, `replace_acl_rules` opens its own transaction and
        # commits separately from the audit append — a crash between
        # them leaves rules updated with no audit row.
        async with conn.transaction():
            await queries.replace_acl_rules(
                conn, payload, created_by=principal.user_id
            )
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="acl.rules_replaced",
                payload={"rule_count": len(payload)},
            )
            # Read-back inside the same transaction so the response
            # reflects exactly what was committed.
            rows = await queries.list_acl_rules(conn)
    await _apply_rule_change(state)
    return [_row_to_rule_view(r) for r in rows]


@router.post("/acl/rules", response_model=RuleView, status_code=201)
async def add_rule(
    req: CreateRuleRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> RuleView:
    import asyncpg  # noqa: PLC0415
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        # Atomic insert + audit. The
        # UniqueViolationError raise is BEFORE the transaction
        # commits, so it short-circuits cleanly without rollback.
        async with conn.transaction():
            try:
                row = await queries.insert_acl_rule(
                    conn,
                    ord=req.ord,
                    effect=req.effect,
                    user_level=req.user_level,
                    caller_pattern=req.caller_pattern,
                    callee_pattern=req.callee_pattern,
                    name=req.name,
                    description=req.description,
                    created_by=principal.user_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"ord {req.ord} already in use by another rule",
                ) from exc
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="acl.rule_added",
                target_kind="acl_rule",
                target_id=row.rule_id,
                payload=req.model_dump(),
            )
    await _apply_rule_change(state)
    return _row_to_rule_view(row)


_RULE_PATCHABLE_COLUMNS = frozenset(
    {"ord", "name", "description", "effect", "user_level", "caller_pattern", "callee_pattern"}
)


@router.patch("/acl/rules/{rule_id}", response_model=RuleView)
async def update_rule(
    rule_id: str,
    req: UpdateRuleRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> RuleView:
    import asyncpg  # noqa: PLC0415
    state = request.app.state.bp
    # `exclude_unset=True` (NOT `exclude_none`) so a PATCH with an
    # explicit JSON null clears the column, while a PATCH that omits
    # the field leaves it unchanged. The previous `exclude_none=True`
    # collapsed both into "omitted" — admins couldn't clear `name`
    # or `description` once set on an ACL rule.
    fields = req.model_dump(exclude_unset=True)
    # `update_acl_rule` enforces the column allowlist itself; this is a
    # defense-in-depth check for future call sites. Use a real raise
    # instead of `assert` — `assert` is stripped
    # under `python -O` so the safety net would silently disappear in
    # optimized deployments. Surface as a structured 500 rather than
    # a bare RuntimeError so the client gets a clean envelope and
    # operator metrics see a 500-coded response.
    bad = [c for c in fields if c not in _RULE_PATCHABLE_COLUMNS]
    if bad:
        raise HTTPException(
            status_code=500,
            detail="internal: rule column allowlist drift",
        )
    async with state.db_pool.acquire() as conn:
        # Atomic update + audit.
        async with conn.transaction():
            try:
                row = await queries.update_acl_rule(conn, rule_id, fields=fields)
            except asyncpg.UniqueViolationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"ord {fields.get('ord')} already in use by another rule",
                ) from exc
            except asyncpg.NotNullViolationError as exc:
                # `exclude_unset=True` lets admins send explicit null to
                # clear nullable columns. NOT NULL columns (`ord`,
                # `effect`, `user_level`, etc.) reject null at the DB
                # level — surface as 400 with the column name.
                column = getattr(exc, "column_name", None) or "<unknown>"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"column {column!r} cannot be set to null; "
                        "omit the field to leave it unchanged"
                    ),
                ) from exc
            if row is None:
                raise HTTPException(status_code=404, detail="rule not found")
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="acl.rule_updated",
                target_kind="acl_rule",
                target_id=rule_id,
                payload=fields,
            )
    await _apply_rule_change(state)
    return _row_to_rule_view(row)


@router.delete("/acl/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        existing = await queries.get_acl_rule(conn, rule_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="rule not found")
        # Atomic delete + audit.
        async with conn.transaction():
            await queries.delete_acl_rule(conn, rule_id)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="acl.rule_removed",
                target_kind="acl_rule",
                target_id=rule_id,
                payload={
                    "ord": existing.ord,
                    "effect": existing.effect,
                    "user_level": existing.user_level,
                    "caller_pattern": existing.caller_pattern,
                    "callee_pattern": existing.callee_pattern,
                },
            )
    await _apply_rule_change(state)


@router.post("/acl/rules/reorder", status_code=200)
async def reorder_rules(
    req: ReorderRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, int]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        # Atomic reorder + audit.
        async with conn.transaction():
            await queries.reorder_acl_rules(conn, new_ords=req.new_ords)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="acl.rules_reordered",
                payload={"changes": req.new_ords},
            )
    await _apply_rule_change(state)
    return {"updated": len(req.new_ords)}


@router.post("/acl/rules/simulate", response_model=SimulateResponse)
async def simulate_rule(
    req: SimulateRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> SimulateResponse:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        caller = await queries.get_agent(conn, req.caller_id)
        callee = await queries.get_agent(conn, req.callee_id)
    if caller is None:
        raise HTTPException(status_code=404, detail=f"caller {req.caller_id!r} not found")
    if callee is None:
        raise HTTPException(status_code=404, detail=f"callee {req.callee_id!r} not found")

    from bp_router.acl import _view  # noqa: PLC0415

    trace_steps: list[TraceStep] = []
    decision = is_allowed(
        state.rules.rules,
        caller=_view(caller.agent_id, caller.groups, caller.capabilities),
        callee=_view(callee.agent_id, callee.groups, callee.capabilities),
        user_level=req.user_level,
        trace=trace_steps,
    )
    return SimulateResponse(
        allow=decision.allow,
        rule_name=decision.rule_name,
        trace=[
            {
                "rule_id": s.rule_id,
                "rule_name": s.rule_name,
                "matched": s.matched,
                "skipped_reason": s.skipped_reason,
            }
            for s in trace_steps
        ],
    )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentSummaryView(BaseModel):
    agent_id: str
    kind: str
    status: str
    groups: list[str]
    capabilities: list[str]
    registered_at: datetime | None = None
    last_seen_at: datetime | None = None


class AgentDetailView(AgentSummaryView):
    """Agent summary plus everything an admin would want for inspection.
    `auth_token_hash` is deliberately excluded — admins shouldn't be
    able to fingerprint the hash via the API."""

    agent_info: dict[str, Any]
    public_key: str | None = None


def _agent_to_summary(row) -> AgentSummaryView:  # type: ignore[no-untyped-def]
    return AgentSummaryView(
        agent_id=row.agent_id,
        kind=row.kind,
        status=row.status,
        groups=row.groups,
        capabilities=row.capabilities,
        registered_at=row.registered_at,
        last_seen_at=row.last_seen_at,
    )


def _agent_to_detail(row) -> AgentDetailView:  # type: ignore[no-untyped-def]
    return AgentDetailView(
        agent_id=row.agent_id,
        kind=row.kind,
        status=row.status,
        groups=row.groups,
        capabilities=row.capabilities,
        registered_at=row.registered_at,
        last_seen_at=row.last_seen_at,
        agent_info=row.agent_info,
        public_key=row.public_key,
    )


@router.get("/agents", response_model=list[AgentSummaryView])
async def list_agents(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[AgentSummaryView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_agents(conn)
    return [_agent_to_summary(r) for r in rows]


@router.get("/agents/{agent_id}", response_model=AgentDetailView)
async def get_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> AgentDetailView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _agent_to_detail(agent)


@router.post("/agents/{agent_id}/suspend", status_code=202)
async def suspend_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # Atomic suspend + audit.
        async with conn.transaction():
            await queries.suspend_agent(conn, agent_id)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="agent.suspended",
                target_kind="agent",
                target_id=agent_id,
            )

    # Force-close the live socket if present.
    entry = state.socket_registry.get(agent_id)
    if entry is not None:
        try:
            await entry.websocket.close(code=4003, reason="agent_suspended")
        except Exception:  # noqa: BLE001
            pass
        entry.closed.set()

    # Fail any in-flight tasks.
    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    failed = await fail_inflight_for_agent(state, agent_id, reason="agent_suspended")
    await push_catalog_update_to_all(state)
    return {"agent_id": agent_id, "failed_tasks": failed}


@router.post("/agents/{agent_id}/unsuspend", status_code=202)
async def unsuspend_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Restore a suspended agent to `active`.

    Idempotent on already-active rows. Refuses to act on `removed`
    (eviction is terminal) or `pending` (reserved). After the status
    flip the agent's SDK reconnect supervisor will pick up on its
    next backoff tick; admit-time `agent_disconnected` covers the
    interim window.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.status == "active":
            return {"agent_id": agent_id, "failed_tasks": 0}
        if agent.status != "suspended":
            raise HTTPException(
                status_code=409,
                detail=f"cannot unsuspend agent in status {agent.status!r}",
            )
        # Atomic unsuspend + audit.
        async with conn.transaction():
            await queries.unsuspend_agent(conn, agent_id)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="agent.unsuspended",
                target_kind="agent",
                target_id=agent_id,
            )

    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415

    await push_catalog_update_to_all(state)
    return {"agent_id": agent_id, "failed_tasks": 0}


@router.post("/agents/{agent_id}/reset", status_code=202)
async def reset_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Force a registered agent off and require it to re-onboard before it
    can serve again — an admin "kick" lever.

    ROLE (post idempotent re-onboard): this is now an OPERATIONS action, not a
    recovery one. Recovery no longer needs `pending` — `POST /v1/onboard`
    accepts a valid invitation against an already-`active` row and re-onboards
    in place (see `api/onboard.py`). What `reset` still uniquely does:

      * force-close the agent's live socket and FAIL its in-flight tasks, and
      * block it (status `pending` → handshake refused) until it re-onboards.

    It's the reversible sibling of `suspend`: where an unsuspend needs a
    second admin action, a `reset` agent comes back on its own the moment it
    re-onboards with a valid invitation. Use it to evict a misbehaving agent's
    live session, or to force a credential rotation.

    Idempotent on `pending`. Refuses `removed` (eviction is terminal — a
    retired `agent_id` is never reusable). Re-onboard still requires an
    admin-issued invitation, so `reset` never frees the `agent_id` for silent
    reuse. To reset AND hand the operator a fresh invitation in one step, use
    `reprovision` instead.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.status == "pending":
            return {"agent_id": agent_id, "status": "pending", "failed_tasks": 0}
        if agent.status == "removed":
            raise HTTPException(
                status_code=409,
                detail="evicted agents are terminal and cannot be reset",
            )
        # Atomic reset + audit.
        async with conn.transaction():
            await queries.reset_agent_to_pending(conn, agent_id)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="agent.reset",
                target_kind="agent",
                target_id=agent_id,
            )

    # Drop any live socket + fail in-flight tasks — the agent is now pending
    # and must re-onboard before it can serve again.
    entry = state.socket_registry.get(agent_id)
    if entry is not None:
        try:
            await entry.websocket.close(code=4003, reason="agent_reset")
        except Exception:  # noqa: BLE001
            pass
        entry.closed.set()

    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    failed = await fail_inflight_for_agent(state, agent_id, reason="agent_reset")
    await push_catalog_update_to_all(state)
    return {"agent_id": agent_id, "status": "pending", "failed_tasks": failed}


@router.post(
    "/agents/{agent_id}/reprovision",
    response_model=AgentReprovisioned,
    status_code=202,
)
async def reprovision_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> AgentReprovisioned:
    """Reset an agent to `pending` AND mint a fresh invitation in one step —
    the admin "one-click recover" for a stuck agent.

    ROLE (post idempotent re-onboard): a fleet managed by `scripts/prod.sh`
    self-recovers — prod.sh ships a fresh invitation each launch and re-onboard
    reactivates the row, no admin step. `reprovision` is the path for an agent
    OUTSIDE that flow: a stuck one-off (token expired after >24h down, state
    dir wiped) that has no pending invitation waiting. It does what the
    operator would otherwise do by hand — mint an invitation (via
    `/invitations`) and hand it to the agent — but atomically and one-click.
    The `reset`-to-pending here also force-closes the live socket + fails
    in-flight tasks (see `reset`); the agent re-onboards with the returned
    token.

    Mirrors the original provisioning so the agent reconnects unchanged:
    `provisions_service_user` is set iff the agent has a co-located service
    principal (`usr_service_{agent_id}`) — restoring a channel agent's
    service refresh token on re-onboard. The invitation is issued at
    `tier1` (the conventional agent level; level is invitation metadata and
    does not propagate onto the agent).

    Refuses `removed` (eviction is terminal). The returned
    `invitation_token` is plaintext, shown ONCE.
    """
    state = request.app.state.bp
    token = _secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=_REPROVISION_INVITATION_TTL_S)

    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.status == "removed":
            raise HTTPException(
                status_code=409,
                detail="evicted agents are terminal and cannot be reprovisioned",
            )
        # Mirror the original provisioning: a co-located service principal
        # means this was a channel agent — the fresh invitation must re-mint
        # its service refresh token on re-onboard, or it reconnects without
        # its service identity.
        svc_id = service_user_id_for_agent(agent_id)
        provisions_service_user = (
            await queries.get_user_by_id(conn, svc_id)
        ) is not None

        # Atomic: reset (if needed) + mint invitation + audit. All-or-nothing
        # so we never ship a live invitation whose creation isn't audited,
        # nor reset an agent without giving it a way back in.
        async with conn.transaction():
            if agent.status != "pending":
                await queries.reset_agent_to_pending(conn, agent_id)
            await queries.insert_invitation(
                conn,
                token_hash=_hash(token),
                level="tier1",
                expires_at=expires_at,
                created_by=principal.user_id,
                idempotency_key=None,
                provisions_service_user=provisions_service_user,
            )
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="agent.reprovision",
                target_kind="agent",
                target_id=agent_id,
                payload={"provisions_service_user": provisions_service_user},
            )

    # Drop any live socket + fail in-flight tasks — the agent is now pending
    # and must re-onboard with the fresh invitation before serving again.
    entry = state.socket_registry.get(agent_id)
    if entry is not None:
        try:
            await entry.websocket.close(code=4003, reason="agent_reprovision")
        except Exception:  # noqa: BLE001
            pass
        entry.closed.set()

    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    failed = await fail_inflight_for_agent(state, agent_id, reason="agent_reprovision")
    await push_catalog_update_to_all(state)
    return AgentReprovisioned(
        agent_id=agent_id,
        status="pending",
        failed_tasks=failed,
        invitation_token=token,
        expires_at=expires_at,
        provisions_service_user=provisions_service_user,
    )


@router.post("/agents/{agent_id}/evict", status_code=202)
async def evict_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Permanently remove an agent from the catalog.

    Sets status to 'removed' (terminal — the agent never serves again) and
    then **renames the row's PK to a tombstone** (`deleted_<id>_<epoch>`) so
    the original `agent_id` is freed for a brand-new agent to onboard. The
    row (and all its `tasks` history) is preserved under the tombstone id via
    FK `ON UPDATE CASCADE`; the co-located service principal
    (`usr_service_<id>`) is tombstoned the same way so a CHANNEL agent's id is
    reusable too. Force-closes the live socket, fails in-flight tasks, and
    pushes a CatalogUpdate to remaining peers.

    Distinct from `/suspend` (reversible) and `/reset` (recover the SAME id):
    evict retires the agent and releases its id.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.status == "removed":
            return {"agent_id": agent_id, "failed_tasks": 0, "already_removed": True}
        # Atomic evict + audit.
        async with conn.transaction():
            await queries.evict_agent(conn, agent_id)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="agent.evicted",
                target_kind="agent",
                target_id=agent_id,
            )

    entry = state.socket_registry.get(agent_id)
    if entry is not None:
        try:
            await entry.websocket.close(code=4003, reason="agent_evicted")
        except Exception:  # noqa: BLE001
            pass
        entry.closed.set()

    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    # Fail in-flight tasks FIRST — they key off the live `agent_id`. Only
    # then rename the PK to a tombstone (the rename cascades onto the now-
    # terminal task rows, preserving history under the new id).
    failed = await fail_inflight_for_agent(state, agent_id, reason="agent_evicted")

    epoch = int(_now().timestamp())
    svc_id = service_user_id_for_agent(agent_id)
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            svc_exists = await queries.get_user_by_id(conn, svc_id) is not None
            new_agent_id, new_svc_id = await queries.rename_evicted_agent(
                conn, agent_id, epoch=epoch,
                service_user_id=svc_id if svc_exists else None,
            )
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="agent.id_released",
                target_kind="agent",
                target_id=agent_id,
                payload={
                    "tombstone_agent_id": new_agent_id,
                    "tombstone_service_user_id": new_svc_id,
                },
            )

    await push_catalog_update_to_all(state)
    return {
        "agent_id": agent_id,
        "failed_tasks": failed,
        "tombstone_agent_id": new_agent_id,
        "id_released": new_agent_id != agent_id,
    }


# ---------------------------------------------------------------------------
# Cross-user task views (admin)
# ---------------------------------------------------------------------------


class TaskSummaryView(BaseModel):
    task_id: str
    parent_task_id: str | None = None
    user_id: str
    session_id: str
    agent_id: str
    state: str
    status_code: int | None = None
    priority: str
    deadline: datetime | None = None
    created_at: datetime
    updated_at: datetime


def _task_to_summary(row) -> TaskSummaryView:  # type: ignore[no-untyped-def]
    return TaskSummaryView(
        task_id=row.task_id,
        parent_task_id=row.parent_task_id,
        user_id=row.user_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        state=row.state.value,
        status_code=row.status_code,
        priority=row.priority.value,
        deadline=row.deadline,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/agents/{agent_id}/tasks", response_model=list[TaskSummaryView])
async def list_agent_tasks(
    agent_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: SessionPrincipal = Depends(require_admin),
) -> list[TaskSummaryView]:
    """Cross-user view of recent tasks owned by `agent_id`.

    Bypasses the per-user `Scope` invariant by design — admin-only.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        existing = await queries.get_agent(conn, agent_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="agent not found")
        rows = await queries.list_tasks_by_agent(
            conn, agent_id, limit=limit, offset=offset
        )
    return [_task_to_summary(r) for r in rows]


@router.get("/users/{user_id}/tasks", response_model=list[TaskSummaryView])
async def list_user_tasks(
    user_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: SessionPrincipal = Depends(require_admin),
) -> list[TaskSummaryView]:
    """Cross-session view of recent tasks owned by `user_id`."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        existing = await queries.get_user_by_id(conn, user_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="user not found")
        rows = await queries.list_tasks_by_user(
            conn, user_id, limit=limit, offset=offset
        )
    return [_task_to_summary(r) for r in rows]


# ---------------------------------------------------------------------------
# Test task — admin-driven admit for spot-checking agents
# ---------------------------------------------------------------------------


class TestTaskRequest(BaseModel):
    destination_agent_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    act_as_user_id: str | None = None
    """When set (and `ROUTER_ADMIN_TEST_ALLOW_ACT_AS=true`), admit the
    task with this user's level. Defaults to the calling admin's
    own user_id."""
    session_id: str | None = None
    """Opaque pass-through. If supplied, used verbatim — the router
    does not validate ownership. If missing, the endpoint opens a
    fresh ephemeral session for the acting user; those sessions are
    GC'd after 30 days when they have no remaining tasks."""
    wait: bool = True
    timeout_s: float = Field(default=30.0, ge=0.1, le=300.0)


class TestTaskResponse(BaseModel):
    task_id: str
    session_id: str
    caller_user_id: str
    caller_user_level: str
    status: str | None = None
    status_code: int | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    duration_s: float | None = None


@router.post("/tasks/test", response_model=TestTaskResponse)
async def test_task(
    req: TestTaskRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> TestTaskResponse:
    """Admit a task as the synthetic `admin_console` agent and (when
    `wait=true`) return its terminal `Result`.

    Uses the same admit/dispatch path real spawns go through, so this
    exercises ACL evaluation, schema validation, delivery, and the
    state machine end-to-end. Use `/v1/admin/acl/rules/simulate` for
    pure ACL dry-runs.
    """
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    from bp_protocol.frames import NewTaskFrame  # noqa: PLC0415
    from bp_protocol.types import AgentOutput, TaskPriority, TaskStatus  # noqa: PLC0415
    from bp_router.tasks import AdmitError, admit_task  # noqa: PLC0415

    state = request.app.state.bp
    settings = state.settings

    # Acting user resolution.
    if req.act_as_user_id is not None:
        if not settings.admin_test_allow_act_as:
            raise HTTPException(
                status_code=403,
                detail=(
                    "act_as_user_id is disabled in this deployment "
                    "(set ROUTER_ADMIN_TEST_ALLOW_ACT_AS=true to enable)"
                ),
            )
        acting_user_id = req.act_as_user_id
    else:
        acting_user_id = principal.user_id

    async with state.db_pool.acquire() as conn:
        acting_user = await queries.get_user_by_id(conn, acting_user_id)
    if acting_user is None or acting_user.deleted_at is not None:
        raise HTTPException(
            status_code=404,
            detail=f"acting user {acting_user_id!r} not found",
        )
    if acting_user.suspended_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"acting user {acting_user_id!r} is suspended",
        )

    # Session pass-through. Open a fresh ephemeral session if not
    # supplied. The router validates session ownership and open-state
    # in admit_task itself (session_unknown / session_closed); the
    # `except AdmitError` block below surfaces those as 404 / 409.
    if req.session_id is not None:
        session_id = req.session_id
    else:
        async with state.db_pool.acquire() as conn:
            row = await queries.Scope.user(conn, acting_user_id).open_session(
                metadata={
                    "kind": "admin_test",
                    "admin_id": principal.user_id,
                },
            )
        session_id = row.session_id

    # Synthesize the spawn frame as if from `admin_console`.
    frame = NewTaskFrame(
        agent_id="admin_console",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id=None,
        parent_task_id=None,
        destination_agent_id=req.destination_agent_id,
        user_id=acting_user_id,
        session_id=session_id,
        priority=TaskPriority.NORMAL,
        payload=req.payload,
    )

    started = time.monotonic()
    try:
        # `.task_id` — R9: admit_task returns AdmitResult. This admin
        # "Test Task" path polls the DB for terminal state (not the
        # SDK pending_results path), so it is unaffected by the
        # idempotent-replay hang and ignores `.replay_result`.
        task_id = (
            await admit_task(state, frame, caller_agent_id="admin_console")
        ).task_id
    except AdmitError as exc:
        # Map known admit error codes to HTTP statuses. Defaults to
        # 400 (bad request) for caller-fixable errors.
        status_for_code = {
            "user_unknown": 404,
            "session_unknown": 404,
            "agent_not_found": 404,
            "session_closed": 409,
            "acl_denied": 403,
            "schema_mismatch": 400,
            "quota_exceeded": 429,
            "agent_disconnected": 503,
            "ack_timeout": 504,
            "rejected": 400,
            "internal_error": 500,
        }
        # `quota_exceeded` carries `retry_after_s` so admin clients
        # can back off rather than hot-loop. Round up to the next
        # whole second per RFC 7231 §7.1.3 (delta-seconds form).
        headers: dict[str, str] = {}
        if exc.code == "quota_exceeded" and exc.retry_after_s is not None:
            headers["Retry-After"] = str(max(1, int(exc.retry_after_s) + 1))
        raise HTTPException(
            status_code=status_for_code.get(exc.code, 400),
            detail={"code": exc.code, "message": exc.message},
            headers=headers,
        ) from exc

    async with state.db_pool.acquire() as conn:
        await queries.append_audit_event(
            conn,
            actor_kind="admin",
            actor_id=principal.user_id,
            event="task.test_dispatched",
            target_kind="task",
            target_id=task_id,
            payload={
                "destination": req.destination_agent_id,
                "session_id": session_id,
                "acted_as": acting_user_id,
                "wait": req.wait,
            },
        )

    if not req.wait:
        return TestTaskResponse(
            task_id=task_id,
            session_id=session_id,
            caller_user_id=acting_user_id,
            caller_user_level=acting_user.level,
        )

    # Wait for terminal state. Replaces a 50-ms
    # busy-poll that acquired a fresh DB connection per iteration —
    # worst case 6_000 acquires per request, which starved the
    # default 10-conn pool against legitimate user traffic. Now:
    #
    #   - Register an `asyncio.Event` BEFORE reading so we don't
    #     miss a notification that fires between the read and the
    #     register.
    #   - Read the row. If terminal, return.
    #   - Otherwise wait on the event (set by `complete_task` /
    #     `cancel_task` / `fail_task` after their commit) up to 1
    #     second OR the remaining deadline. The 1-s cap is the
    #     fallback poll cadence for multi-worker deployments where
    #     the terminal transition lands on a DIFFERENT worker — its
    #     event won't fire here, so we re-read the row periodically.
    #
    # Worst-case multi-worker request count: ~timeout_s polls (1/s)
    # vs. the prior 20·timeout_s. Same-worker case completes in ≤2
    # reads regardless of how long the agent takes.
    event = asyncio.Event()
    state.task_terminal_events[task_id] = event
    try:
        deadline = time.monotonic() + req.timeout_s
        while True:
            async with state.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT state, status_code, output, error
                    FROM tasks WHERE task_id = $1
                    """,
                    task_id,
                )
            if row is not None and row["state"] in (
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            ):
                duration_s = time.monotonic() - started
                status_map = {
                    "SUCCEEDED": TaskStatus.SUCCEEDED,
                    "FAILED":    TaskStatus.FAILED,
                    "CANCELLED": TaskStatus.CANCELLED,
                    "TIMED_OUT": TaskStatus.TIMED_OUT,
                }
                output = (
                    AgentOutput.model_validate(row["output"]).model_dump()
                    if row["output"]
                    else None
                )
                return TestTaskResponse(
                    task_id=task_id,
                    session_id=session_id,
                    caller_user_id=acting_user_id,
                    caller_user_level=acting_user.level,
                    status=status_map[row["state"]].value,
                    status_code=row["status_code"],
                    output=output,
                    error=row["error"],
                    duration_s=round(duration_s, 3),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(
                    status_code=504,
                    detail=f"test task {task_id} did not complete in {req.timeout_s}s",
                )
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=min(remaining, 1.0)
                )
            except TimeoutError:
                # Fallback poll cycle — either the wait window
                # elapsed without a notification (cross-worker
                # transition), or 1 s passed with no terminal yet.
                pass
            # Clear so a stale set from a prior iteration doesn't
            # short-circuit the next wait. Safe to clear an unset
            # event.
            event.clear()
    finally:
        # Defensive: `_notify_task_terminal` already pops, but if
        # the request was cancelled mid-wait or returned early the
        # entry could still be live. `pop(..., None)` is safe.
        state.task_terminal_events.pop(task_id, None)


# ---------------------------------------------------------------------------
# Invitation history
# ---------------------------------------------------------------------------


class InvitationView(BaseModel):
    """Public projection of an invitation row.

    `token_hash` is the SHA-256 of the original token; the original token
    itself is unrecoverable (see `bp_router/api/onboard.py`). Surfacing
    the hash is harmless and lets the admin UI key the row for revoke.
    """

    token_hash: str
    level: str
    expires_at: datetime
    used_at: datetime | None = None
    used_by: str | None = None
    created_by: str
    status: str  # "valid" | "used" | "expired"


def _invitation_to_view(row, *, now: datetime) -> InvitationView:  # type: ignore[no-untyped-def]
    if row.used_at is not None:
        status = "used"
    elif row.expires_at < now:
        status = "expired"
    else:
        status = "valid"
    return InvitationView(
        token_hash=row.token_hash,
        level=row.level,
        expires_at=row.expires_at,
        used_at=row.used_at,
        used_by=row.used_by,
        created_by=row.created_by,
        status=status,
    )


@router.get("/invitations", response_model=list[InvitationView])
async def list_invitations(
    request: Request,
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(valid|used|expired)$"
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: SessionPrincipal = Depends(require_admin),
) -> list[InvitationView]:
    """List issued invitations. Filter by `status` (`valid`, `used`,
    `expired`) when set; otherwise return all rows newest-first
    (sorted by `created_at DESC` with `token_hash` tie-break).

    Filtering happens IN-SQL so pagination
    boundaries are consistent — the previous code paginated first
    and filtered afterwards, which made `?status=valid&limit=100`
    return 0 rows when the first 100 rows happened to be all
    expired/used. Status comparison uses a single `now` timestamp
    pinned at the start of the request to
    avoid clock skew between the SQL `now()` server clock and the
    Python `_now()` admin-process clock when classifying rows."""
    state = request.app.state.bp
    now = _now()
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_invitations(
            conn,
            limit=limit,
            offset=offset,
            status_filter=status_filter,
            now=now,
        )
    return [_invitation_to_view(r, now=now) for r in rows]


@router.delete("/invitations/{token_hash}", status_code=204)
async def revoke_invitation(
    token_hash: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    """Revoke an unused invitation. Refuses if the invitation was
    already consumed — used invitations stay in the table for audit.
    """
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        existing = await queries.get_invitation(conn, token_hash)
        if existing is None:
            raise HTTPException(status_code=404, detail="invitation not found")
        if existing.used_at is not None:
            raise HTTPException(
                status_code=409,
                detail="invitation already consumed; cannot revoke",
            )
        # Atomic delete + audit. The race-detect
        # path below either raises 404/409 cleanly (transaction
        # rolls back the DELETE that returned 0 rows) or proceeds.
        async with conn.transaction():
            deleted = await queries.delete_invitation(conn, token_hash)
            if not deleted:
                # Raced with another revoke / consume between the
                # SELECT and the DELETE. Re-fetch to give a precise
                # error.
                after = await queries.get_invitation(conn, token_hash)
                if after is None:
                    raise HTTPException(
                        status_code=404, detail="invitation not found"
                    )
                raise HTTPException(
                    status_code=409,
                    detail="invitation already consumed; cannot revoke",
                )
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="invitation.revoked",
                target_kind="invitation",
                target_id=token_hash,
                payload={"level": existing.level},
            )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditQuery(BaseModel):
    since: datetime | None = None
    until: datetime | None = None
    event: str | None = None
    actor_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


@router.get("/audit")
async def get_audit_log(
    request: Request,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    event: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    principal: SessionPrincipal = Depends(require_admin),
) -> list[dict[str, Any]]:
    state = request.app.state.bp
    # Coerce naive datetimes to UTC. The
    # `audit_log.ts` column is `timestamptz`; comparing it against a
    # naive datetime makes asyncpg interpret the parameter in the
    # session's `TIMEZONE` setting, which silently shifts results
    # around DST boundaries. Anything without an explicit tzinfo is
    # treated as UTC — that's the convention every timestamp the
    # admin UI generates already follows.
    since = _ensure_utc(since)
    until = _ensure_utc(until)
    clauses: list[str] = []
    values: list[Any] = []

    def _add(clause: str, value: Any) -> None:
        values.append(value)
        clauses.append(clause.replace("?", f"${len(values)}"))

    if since is not None:
        _add("ts >= ?", since)
    if until is not None:
        _add("ts <= ?", until)
    if event is not None:
        _add("event = ?", event)
    if actor_id is not None:
        _add("actor_id = ?", actor_id)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT event_id, ts, actor_kind, actor_id, event,
               target_kind, target_id, payload, prev_hash, self_hash
        FROM audit_log
        {where}
        ORDER BY ts DESC, event_id DESC
        LIMIT ${len(values) + 1}
    """
    values.append(limit)

    async with state.db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *values)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# LLM presets
# ---------------------------------------------------------------------------


class LlmPresetView(BaseModel):
    """Public projection of an `llm_presets` row.

    `api_key` is NEVER surfaced. Instead we set `has_api_key=True`
    when an inline key is configured, so admins can see at a glance
    which presets ship their own key vs. resolve via `api_key_ref`.
    `base_url` IS surfaced — it's not a secret, just connection config.
    """

    name: str
    description: str | None = None
    provider: str
    concrete_model: str
    api_key_ref: str
    has_api_key: bool = False
    base_url: str | None = None
    min_user_level: str
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_provider_options: dict[str, Any] = {}
    fallback_preset: str | None = None
    max_retries: int = 0
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


def _preset_to_view(row) -> LlmPresetView:  # type: ignore[no-untyped-def]
    return LlmPresetView(
        name=row.name,
        description=row.description,
        provider=row.provider,
        concrete_model=row.concrete_model,
        api_key_ref=row.api_key_ref,
        has_api_key=bool(row.api_key),
        base_url=row.base_url,
        min_user_level=row.min_user_level,
        default_temperature=row.default_temperature,
        default_max_tokens=row.default_max_tokens,
        default_provider_options=dict(row.default_provider_options or {}),
        fallback_preset=row.fallback_preset,
        max_retries=row.max_retries,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


def _check_provider_base_url_consistency(
    *,
    provider: str,
    base_url: str | None,
    request: Request | None = None,
) -> None:
    """Cross-field invariant: base_url is REQUIRED for openai-compatible
    variants (no default endpoint to fall back to). It's OPTIONAL for
    hosted providers — when set, it overrides the SDK default endpoint
    (Azure OpenAI proxy, Bedrock-fronted Anthropic, Vertex / EU Gemini,
    LiteLLM / Portkey gateways, etc.); when blank the SDK default URL
    is used unchanged.

    Also runs SSRF checks via `validate_base_url`: blocks private /
    loopback / link-local / metadata hosts; requires https:// for
    hosted providers. Operators can carve exceptions via
    `ROUTER_BASE_URL_ALLOWED_HOSTS`.
    """
    from bp_router.llm.presets import (  # noqa: PLC0415
        provider_requires_base_url,
    )
    from bp_router.url_validation import (  # noqa: PLC0415
        BaseUrlValidationError,
        parse_allowed_hosts,
        validate_base_url,
    )

    if provider_requires_base_url(provider) and not base_url:
        raise HTTPException(
            status_code=400,
            detail=f"{provider} preset requires a base_url",
        )

    if not base_url:
        return

    allowed_hosts: frozenset[str] = frozenset()
    if request is not None:
        state = getattr(request.app.state, "bp", None)
        settings = getattr(state, "settings", None) if state else None
        raw = getattr(settings, "base_url_allowed_hosts", None) if settings else None
        allowed_hosts = parse_allowed_hosts(raw)

    try:
        validate_base_url(
            provider=provider,
            base_url=base_url,
            allowed_hosts=allowed_hosts,
        )
    except BaseUrlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_mcp_url_ssrf(url: str, request: Request | None) -> None:
    """SSRF guard for an MCP server `url`. The bridge connects to this URL
    and presents the configured `auth_value_ref` credential, so an
    admin-set (or, once Phase-10d narrows this to a service role, a
    service-set) URL pointing at the cloud-metadata endpoint or another
    internal service is a credential-exfil / SSRF vector. Mirrors the LLM
    preset `base_url` policy but as the `mcp` provider class: loopback /
    private are allowed (internal MCP servers are the norm), while
    link-local (169.254.169.254), cloud-metadata hostnames, and
    multicast/reserved are blocked. http is permitted; operators can
    allowlist hosts via `ROUTER_BASE_URL_ALLOWED_HOSTS`.
    """
    from bp_router.url_validation import (  # noqa: PLC0415
        BaseUrlValidationError,
        parse_allowed_hosts,
        validate_base_url,
    )

    allowed_hosts: frozenset[str] = frozenset()
    if request is not None:
        state = getattr(request.app.state, "bp", None)
        settings = getattr(state, "settings", None) if state else None
        raw = getattr(settings, "base_url_allowed_hosts", None) if settings else None
        allowed_hosts = parse_allowed_hosts(raw)

    try:
        validate_base_url(provider="mcp", base_url=url, allowed_hosts=allowed_hosts)
    except BaseUrlValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_preset_payload(*, name: str | None = None, **fields) -> None:
    """Per-field format / range checks. Cross-field invariants live in
    helpers like `_check_provider_base_url_consistency` so the patch
    path can compute effective values before checking.

    Range bounds (temperature, max_tokens, max_retries) come from
    `bp_router.llm.presets` so the admin webUI helper and this API
    can't drift on the boundary values.
    """
    from bp_router.llm.presets import (  # noqa: PLC0415
        MAX_RETRIES_MAX,
        MAX_RETRIES_MIN,
        TEMPERATURE_MAX,
        TEMPERATURE_MIN,
        is_valid_min_user_level,
        is_valid_preset_name,
        is_valid_provider,
        max_retries_in_range,
        max_tokens_in_range,
        temperature_in_range,
    )

    if name is not None and not is_valid_preset_name(name):
        raise HTTPException(
            status_code=400,
            detail="preset name must match ^[a-z][a-z0-9_-]{0,63}$",
        )
    provider = fields.get("provider")
    if provider is not None and not is_valid_provider(provider):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider: {provider!r}",
        )
    min_level = fields.get("min_user_level")
    if min_level is not None and not is_valid_min_user_level(min_level):
        raise HTTPException(
            status_code=400,
            detail="min_user_level must be '*' | 'admin' | 'service' | 'tierN'",
        )
    temp = fields.get("default_temperature")
    if temp is not None and not temperature_in_range(temp):
        raise HTTPException(
            status_code=400,
            detail=f"default_temperature must be in [{TEMPERATURE_MIN}, {TEMPERATURE_MAX}]",
        )
    max_t = fields.get("default_max_tokens")
    if max_t is not None and not max_tokens_in_range(max_t):
        raise HTTPException(
            status_code=400,
            detail="default_max_tokens must be a positive integer",
        )
    retries = fields.get("max_retries")
    if retries is not None and not max_retries_in_range(retries):
        raise HTTPException(
            status_code=400,
            detail=f"max_retries must be in [{MAX_RETRIES_MIN}, {MAX_RETRIES_MAX}]",
        )
    fallback = fields.get("fallback_preset")
    if fallback is not None and fallback != "" and not is_valid_preset_name(fallback):
        raise HTTPException(
            status_code=400,
            detail="fallback_preset must match the preset-name grammar",
        )
    # NOTE: full URL validation (scheme + SSRF blocklist) lives in
    # `_check_provider_base_url_consistency`, which is called separately
    # by create + patch with the EFFECTIVE provider+base_url (post-merge).
    # We only do here whatever check would fail closed regardless of
    # provider — currently nothing, since openai-compatible vs hosted
    # have different scheme rules.


class CreateLlmPresetRequest(BaseModel):
    name: str
    description: str | None = None
    provider: str
    concrete_model: str
    api_key_ref: str = ""
    api_key: str | None = None
    base_url: str | None = None
    min_user_level: str = "*"
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_provider_options: dict[str, Any] | None = None
    fallback_preset: str | None = None
    max_retries: int = 0


class UpdateLlmPresetRequest(BaseModel):
    description: str | None = None
    provider: str | None = None
    concrete_model: str | None = None
    api_key_ref: str | None = None
    # Inline secret. Set to a non-empty string to update; omit (None)
    # to leave unchanged. To unset, send `clear_api_key=true`.
    api_key: str | None = None
    clear_api_key: bool = False
    base_url: str | None = None
    min_user_level: str | None = None
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_provider_options: dict[str, Any] | None = None
    fallback_preset: str | None = None
    max_retries: int | None = None


# Single source of truth for the patchable column set lives in
# `bp_router.db.queries` — the layer that actually issues the dynamic
# UPDATE. Keeping a duplicated frozenset here would let the two drift
# (admin API allows column X, queries refuses, → a confusing 500).
from bp_router.db.queries import _PRESET_PATCHABLE_COLUMNS as _PRESET_PATCHABLE  # noqa: E402


async def _reload_presets(state) -> None:  # type: ignore[no-untyped-def]
    """Re-read the table into the in-memory preset map. Called after
    every mutation so subsequent LLM calls see the new shape."""
    async with state.db_pool.acquire() as conn:
        await state.llm_service.load_presets_from_db(conn)


# Advisory lock key for preset writes. Held for the duration of a
# create / patch / delete transaction so two concurrent writers can't
# race the post-write cycle check (each individually passing while
# their combined effect produces a cycle in the persisted state).
_PRESET_WRITE_LOCK_KEY = 0x4C4C_4D5F_5052_5354  # ascii "LLM_PRST"


async def _lock_preset_writes(conn) -> None:  # type: ignore[no-untyped-def]
    """Take the preset-write advisory lock. Held for the rest of the
    transaction; releases on commit / rollback. Call at the top of any
    create/patch/delete transaction *before* mutating rows."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1)", _PRESET_WRITE_LOCK_KEY
    )


async def _check_fallback_post_write(state, conn) -> None:  # type: ignore[no-untyped-def]
    """After a write, build the candidate preset map from the current
    DB state and verify the fallback graph is acyclic. Raises 400 if
    the just-saved row would introduce a cycle. Run inside the same
    transaction as the write so the cycle check is consistent."""
    from bp_router.llm.presets import (  # noqa: PLC0415
        Preset,
        PresetCycleError,
        detect_fallback_cycles,
    )

    rows = await queries.list_llm_presets(conn)
    candidate: dict[str, Preset] = {}
    for r in rows:
        candidate[r.name] = Preset(
            name=r.name,
            description=r.description,
            provider=r.provider,
            concrete_model=r.concrete_model,
            api_key_ref=r.api_key_ref,
            api_key=r.api_key,
            base_url=r.base_url,
            min_user_level=r.min_user_level,
            default_temperature=r.default_temperature,
            default_max_tokens=r.default_max_tokens,
            default_provider_options=dict(r.default_provider_options or {}),
            fallback_preset=r.fallback_preset,
            max_retries=r.max_retries,
        )
    try:
        detect_fallback_cycles(candidate)
    except PresetCycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/llm/presets", response_model=list[LlmPresetView])
async def list_llm_presets(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[LlmPresetView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_llm_presets(conn)
    return [_preset_to_view(r) for r in rows]


@router.post("/llm/presets", response_model=LlmPresetView, status_code=201)
async def create_llm_preset(
    req: CreateLlmPresetRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> LlmPresetView:
    import asyncpg  # noqa: PLC0415

    _validate_preset_payload(
        name=req.name,
        provider=req.provider,
        min_user_level=req.min_user_level,
        default_temperature=req.default_temperature,
        default_max_tokens=req.default_max_tokens,
        max_retries=req.max_retries,
        fallback_preset=req.fallback_preset,
        base_url=req.base_url,
    )
    # Empty-string fallback_preset is a UI artefact (unselected). Treat
    # as None so the FK accepts it.
    fallback = req.fallback_preset or None
    base_url = req.base_url or None
    _check_provider_base_url_consistency(
        provider=req.provider, base_url=base_url, request=request
    )
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        # Serialise the write + cycle-check across concurrent admins
        # so two simultaneous writes can't each individually pass the
        # cycle check while their combined effect introduces one.
        async with conn.transaction():
            await _lock_preset_writes(conn)
            try:
                row = await queries.insert_llm_preset(
                    conn,
                    name=req.name,
                    description=req.description,
                    provider=req.provider,
                    concrete_model=req.concrete_model,
                    api_key_ref=req.api_key_ref,
                    api_key=req.api_key or None,
                    base_url=base_url,
                    min_user_level=req.min_user_level,
                    default_temperature=req.default_temperature,
                    default_max_tokens=req.default_max_tokens,
                    default_provider_options=req.default_provider_options,
                    fallback_preset=fallback,
                    max_retries=req.max_retries,
                    created_by=principal.user_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"preset {req.name!r} already exists",
                ) from exc
            except asyncpg.ForeignKeyViolationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown fallback_preset: {fallback!r}",
                ) from exc
            await _check_fallback_post_write(state, conn)
            # Audit payload masks the inline api_key.
            audit_payload = req.model_dump()
            if audit_payload.get("api_key"):
                audit_payload["api_key"] = "***"
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="llm_preset.created",
                target_kind="llm_preset",
                target_id=req.name,
                payload=audit_payload,
            )
    await _reload_presets(state)
    return _preset_to_view(row)


@router.get("/llm/presets/{name}", response_model=LlmPresetView)
async def get_llm_preset(
    name: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> LlmPresetView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.get_llm_preset(conn, name)
    if row is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return _preset_to_view(row)


@router.patch("/llm/presets/{name}", response_model=LlmPresetView)
async def update_llm_preset(
    name: str,
    req: UpdateLlmPresetRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> LlmPresetView:
    import asyncpg  # noqa: PLC0415

    state = request.app.state.bp
    # `exclude_unset=True` (NOT `exclude_none`) so a PATCH with an
    # explicit JSON null clears the column, while a PATCH that omits
    # the field leaves it unchanged. The previous `exclude_none=True`
    # collapsed both into "omitted" — admins couldn't clear
    # `description`, `fallback_preset`, `base_url`, or any other
    # nullable column via the API.
    raw = req.model_dump(exclude_unset=True)
    # `clear_api_key` is a flag on the payload, not a column. Translate
    # it into an explicit `api_key=None` write and drop the flag.
    clear_api_key = raw.pop("clear_api_key", False)
    # Defense against the empty-string back-channel: an admin sending
    # `api_key=""` would otherwise slip past the truthy mask check
    # below and silently null out the column without `clear_api_key`.
    # Either drop the empty value (treat as "leave unchanged") or
    # require the explicit flag. We pick the former — empty string is
    # an artefact of HTML form submission, not an explicit intent.
    if raw.get("api_key") == "":
        raw.pop("api_key")
    if clear_api_key:
        if "api_key" in raw and raw["api_key"] is not None:
            raise HTTPException(
                status_code=400,
                detail="api_key and clear_api_key are mutually exclusive",
            )
        raw["api_key"] = None

    # An explicit empty string for fallback_preset means "clear the FK"
    # (the UI submits "" when the dropdown is on the no-fallback option).
    # Explicit JSON null is also accepted thanks to `exclude_unset=True`.
    if raw.get("fallback_preset") == "":
        raw["fallback_preset"] = None

    # Same convention for base_url — an empty submit clears the column
    # (e.g. when switching from openai-compatible to a hosted provider).
    if raw.get("base_url") == "":
        raw["base_url"] = None

    fields: dict[str, Any] = raw
    # Defense-in-depth allowlist check. `assert` would be stripped under
    # `python -O` — use a real raise so the safety
    # net survives optimized deployments. The same allowlist is enforced
    # inside `queries.update_llm_preset` (single source of truth lives
    # there); this redundant check fires earlier so a future code path
    # that bypasses the queries layer still hits the gate. Surface as
    # a structured 500 rather than bare RuntimeError so the client
    # sees a clean envelope and operator metrics see a 500-coded
    # response.
    bad = [c for c in fields if c not in _PRESET_PATCHABLE]
    if bad:
        raise HTTPException(
            status_code=500,
            detail="internal: preset column allowlist drift",
        )

    _validate_preset_payload(
        **{k: v for k, v in fields.items() if k not in ("api_key", "name")}
    )
    # Cross-field check needs the EVENTUAL provider/base_url after the
    # patch lands. Fetch the row and merge with `fields` so we surface
    # a 400 when the combination is invalid (e.g. switching provider to
    # openai-compatible without supplying a base_url).
    if "provider" in fields or "base_url" in fields:
        async with state.db_pool.acquire() as conn:
            existing = await queries.get_llm_preset(conn, name)
        if existing is not None:
            effective_provider = fields.get("provider", existing.provider)
            effective_base_url = (
                fields["base_url"] if "base_url" in fields
                else existing.base_url
            )
            _check_provider_base_url_consistency(
                provider=effective_provider,
                base_url=effective_base_url,
                request=request,
            )
    if fields.get("fallback_preset") == name:
        raise HTTPException(
            status_code=400,
            detail="a preset's fallback_preset cannot reference itself",
        )
    async with state.db_pool.acquire() as conn:
        # See create_llm_preset for the locking rationale.
        async with conn.transaction():
            await _lock_preset_writes(conn)
            try:
                row = await queries.update_llm_preset(conn, name, fields=fields)
            except asyncpg.ForeignKeyViolationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"unknown fallback_preset: {fields.get('fallback_preset')!r}"
                    ),
                ) from exc
            except asyncpg.NotNullViolationError as exc:
                # `exclude_unset=True` lets admins send explicit JSON null to clear nullable
                # columns. NOT NULL columns (`provider`, `concrete_model`,
                # `api_key_ref`, etc.) reject null at the DB level —
                # surface as 400 with the constraint name so the admin
                # sees which field can't be cleared.
                column = getattr(exc, "column_name", None) or "<unknown>"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"column {column!r} cannot be set to null; "
                        "omit the field to leave it unchanged"
                    ),
                ) from exc
            if row is None:
                raise HTTPException(status_code=404, detail="preset not found")
            await _check_fallback_post_write(state, conn)
            # Audit payload masks the inline api_key.
            audit_payload = dict(fields)
            if audit_payload.get("api_key"):
                audit_payload["api_key"] = "***"
            if clear_api_key:
                audit_payload["api_key_cleared"] = True
                audit_payload.pop("api_key", None)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="llm_preset.updated",
                target_kind="llm_preset",
                target_id=name,
                payload=audit_payload,
            )
    await _reload_presets(state)
    return _preset_to_view(row)


@router.delete("/llm/presets/{name}", status_code=204)
async def delete_llm_preset(
    name: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            await _lock_preset_writes(conn)
            existing = await queries.get_llm_preset(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="preset not found")
            await queries.delete_llm_preset(conn, name)
            await queries.append_audit_event(
                conn,
                actor_kind="admin",
                actor_id=principal.user_id,
                event="llm_preset.deleted",
                target_kind="llm_preset",
                target_id=name,
                payload={
                    "provider": existing.provider,
                    "concrete_model": existing.concrete_model,
                },
            )
    await _reload_presets(state)


# ---------------------------------------------------------------------------
# Phase 10a: MCP servers — admin-managed bridge configurations
# ---------------------------------------------------------------------------


_MCP_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")
_MCP_AUTH_VALUE_REF_RE = re.compile(r"^(env|secret)://.+$")
_MCP_TRANSPORTS = ("sse", "streamable_http")
_MCP_AUTH_KINDS = ("none", "bearer", "header")


class McpServerCreate(BaseModel):
    """Create-time payload for `POST /v1/admin/mcp-servers`.

    Validators are deliberately strict at the boundary — bad values
    are caught before any DB write, and the DB CHECK constraints
    serve as a defense-in-depth fallback."""

    server_id: str
    description: str = ""
    url: str
    transport: str
    auth_kind: str = "none"
    auth_value_ref: str | None = None
    auth_header_name: str | None = None
    groups: list[str] = Field(default_factory=list)
    expose_to_llm: bool = True

    @field_validator("server_id")
    @classmethod
    def _server_id_shape(cls, v: str) -> str:
        if not _MCP_SERVER_ID_RE.match(v):
            raise ValueError(
                "server_id must match ^[a-z][a-z0-9_]+$ "
                "(lowercase letters / digits / underscores, "
                "must not start with a digit)"
            )
        return v

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        lower = v.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            raise ValueError(
                "url must start with http:// or https:// — other schemes "
                "are XSS vectors when rendered in admin links"
            )
        return v

    @field_validator("transport")
    @classmethod
    def _transport_known(cls, v: str) -> str:
        if v not in _MCP_TRANSPORTS:
            raise ValueError(f"transport must be one of {_MCP_TRANSPORTS}")
        return v

    @field_validator("auth_kind")
    @classmethod
    def _auth_kind_known(cls, v: str) -> str:
        if v not in _MCP_AUTH_KINDS:
            raise ValueError(f"auth_kind must be one of {_MCP_AUTH_KINDS}")
        return v

    @field_validator("auth_value_ref")
    @classmethod
    def _auth_value_ref_shape(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _MCP_AUTH_VALUE_REF_RE.match(v):
            raise ValueError(
                "auth_value_ref must use env:// or secret:// schemes "
                "(raw secrets in the database are refused — store the "
                "value in your env / secret store and reference it here)"
            )
        return v

    @field_validator("groups")
    @classmethod
    def _groups_grammar(cls, v: list[str]) -> list[str]:
        from bp_protocol.types import GROUP_NAME_PATTERN  # noqa: PLC0415
        for g in v:
            if not GROUP_NAME_PATTERN.match(g):
                raise ValueError(
                    f"group {g!r} must match [a-z][a-z0-9_:.\\-]{{0,63}}"
                )
        return v

    def _check_auth_consistency(self) -> None:
        """Cross-field check: auth_kind drives which other fields are
        required / forbidden. Mirrors the DB CHECK constraint."""
        if self.auth_kind == "none":
            if self.auth_value_ref is not None:
                raise ValueError(
                    "auth_value_ref must be null when auth_kind='none'"
                )
            if self.auth_header_name is not None:
                raise ValueError(
                    "auth_header_name must be null when auth_kind='none'"
                )
        elif self.auth_kind == "bearer":
            if self.auth_value_ref is None:
                raise ValueError(
                    "auth_value_ref is required when auth_kind='bearer'"
                )
            if self.auth_header_name is not None:
                raise ValueError(
                    "auth_header_name must be null when auth_kind='bearer' "
                    "(implicit `Authorization: Bearer <value>`)"
                )
        elif self.auth_kind == "header":
            if self.auth_value_ref is None:
                raise ValueError(
                    "auth_value_ref is required when auth_kind='header'"
                )
            if self.auth_header_name is None:
                raise ValueError(
                    "auth_header_name is required when auth_kind='header'"
                )


class McpServerUpdate(BaseModel):
    """PATCH payload — every field optional. `auth_kind` carries
    auth_value_ref + auth_header_name together (changing the kind
    implies a fresh credential set)."""

    description: str | None = None
    url: str | None = None
    transport: str | None = None
    auth_kind: str | None = None
    auth_value_ref: str | None = None
    auth_header_name: str | None = None
    groups: list[str] | None = None
    expose_to_llm: bool | None = None

    # Re-use the create-time field validators by name. Pydantic v2
    # applies validators per-field independently, so PATCH inherits
    # the same boundary checks for non-None fields.
    _url_scheme = field_validator("url")(McpServerCreate._url_scheme.__func__)
    _transport_known = field_validator("transport")(
        McpServerCreate._transport_known.__func__
    )
    _auth_kind_known = field_validator("auth_kind")(
        McpServerCreate._auth_kind_known.__func__
    )
    _auth_value_ref_shape = field_validator("auth_value_ref")(
        McpServerCreate._auth_value_ref_shape.__func__
    )
    _groups_grammar = field_validator("groups")(
        McpServerCreate._groups_grammar.__func__
    )


class McpServerView(BaseModel):
    """Response model. `tools_cache` is exposed verbatim — admin UI
    renders it to show the per-row tool count."""

    server_id: str
    description: str
    url: str
    transport: str
    auth_kind: str
    auth_value_ref: str | None = None
    auth_header_name: str | None = None
    groups: list[str] = []
    expose_to_llm: bool = True
    tools_cache: dict[str, Any] | None = None
    refresh_requested_at: datetime | None = None
    created_at: datetime
    last_connected_at: datetime | None = None
    created_by: str | None = None


def _mcp_row_to_view(row) -> McpServerView:  # type: ignore[no-untyped-def]
    return McpServerView(
        server_id=row.server_id,
        description=row.description,
        url=row.url,
        transport=row.transport,
        auth_kind=row.auth_kind,
        auth_value_ref=row.auth_value_ref,
        auth_header_name=row.auth_header_name,
        groups=list(row.groups or []),
        expose_to_llm=row.expose_to_llm,
        tools_cache=row.tools_cache,
        refresh_requested_at=row.refresh_requested_at,
        created_at=row.created_at,
        last_connected_at=row.last_connected_at,
        created_by=row.created_by,
    )


@router.get("/mcp-servers", response_model=list[McpServerView])
async def list_mcp_servers(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[McpServerView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_mcp_servers(conn)
    return [_mcp_row_to_view(r) for r in rows]


@router.get("/mcp-servers/{server_id}", response_model=McpServerView)
async def get_mcp_server(
    server_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> McpServerView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.get_mcp_server(conn, server_id)
    if row is None:
        raise HTTPException(404, "mcp server not found")
    return _mcp_row_to_view(row)


@router.post(
    "/mcp-servers", response_model=McpServerView, status_code=201
)
async def create_mcp_server(
    req: McpServerCreate,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> McpServerView:
    """Create an MCP server config. Phase 10a ships config only —
    the bridge process (Phase 10b/c) will pick it up and onboard
    derived per-tool agents at runtime."""
    req._check_auth_consistency()
    _check_mcp_url_ssrf(req.url, request)
    state = request.app.state.bp
    import asyncpg  # noqa: PLC0415
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            try:
                row = await queries.insert_mcp_server(
                    conn,
                    server_id=req.server_id,
                    description=req.description,
                    url=req.url,
                    transport=req.transport,
                    auth_kind=req.auth_kind,
                    auth_value_ref=req.auth_value_ref,
                    auth_header_name=req.auth_header_name,
                    groups=req.groups,
                    expose_to_llm=req.expose_to_llm,
                    created_by=principal.user_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise HTTPException(
                    409, f"server_id {req.server_id!r} already exists"
                ) from exc
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="mcp_server.created",
                target_kind="mcp_server", target_id=req.server_id,
                payload={
                    "transport": req.transport,
                    "auth_kind": req.auth_kind,
                    "expose_to_llm": req.expose_to_llm,
                },
            )
    return _mcp_row_to_view(row)


@router.patch("/mcp-servers/{server_id}", response_model=McpServerView)
async def update_mcp_server(
    server_id: str,
    req: McpServerUpdate,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> McpServerView:
    """PATCH — only the fields the admin set are written.

    If `auth_kind` is changed, the matching `auth_value_ref` /
    `auth_header_name` must be sent in the same request (the
    new kind dictates the new credential surface; mixing old +
    new is refused by the DB CHECK)."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        existing = await queries.get_mcp_server(conn, server_id)
        if existing is None:
            raise HTTPException(404, "mcp server not found")

        # Build a synthesized full record so we can run the
        # cross-field consistency check before hitting the DB.
        merged_kind = req.auth_kind if req.auth_kind is not None else existing.auth_kind
        merged_ref = (
            req.auth_value_ref if req.auth_kind is not None
            else existing.auth_value_ref
        )
        merged_header = (
            req.auth_header_name if req.auth_kind is not None
            else existing.auth_header_name
        )
        synthetic = McpServerCreate(
            server_id=existing.server_id,
            description=req.description or existing.description,
            url=req.url or existing.url,
            transport=req.transport or existing.transport,
            auth_kind=merged_kind,
            auth_value_ref=merged_ref,
            auth_header_name=merged_header,
            groups=req.groups if req.groups is not None else list(existing.groups),
            expose_to_llm=(
                req.expose_to_llm if req.expose_to_llm is not None
                else existing.expose_to_llm
            ),
        )
        synthetic._check_auth_consistency()
        _check_mcp_url_ssrf(synthetic.url, request)

        async with conn.transaction():
            row = await queries.update_mcp_server(
                conn, server_id,
                description=req.description,
                url=req.url,
                transport=req.transport,
                auth_kind=req.auth_kind,
                auth_value_ref=(
                    req.auth_value_ref if req.auth_kind is not None else None
                ),
                auth_header_name=(
                    req.auth_header_name if req.auth_kind is not None else None
                ),
                groups=req.groups,
                expose_to_llm=req.expose_to_llm,
            )
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="mcp_server.updated",
                target_kind="mcp_server", target_id=server_id,
                payload={
                    k: getattr(req, k)
                    for k in (
                        "description", "url", "transport", "auth_kind",
                        "groups", "expose_to_llm",
                    )
                    if getattr(req, k) is not None
                },
            )
    assert row is not None
    return _mcp_row_to_view(row)


@router.delete("/mcp-servers/{server_id}", status_code=204)
async def delete_mcp_server(
    server_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> None:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await queries.get_mcp_server(conn, server_id)
            if existing is None:
                raise HTTPException(404, "mcp server not found")
            await queries.delete_mcp_server(conn, server_id)
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="mcp_server.deleted",
                target_kind="mcp_server", target_id=server_id,
                payload={"transport": existing.transport},
            )


@router.post("/mcp-servers/{server_id}/refresh-tools", status_code=202)
async def refresh_mcp_server_tools(
    server_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Signal the bridge to re-fetch `tools/list` from the upstream.

    Sets `mcp_servers.refresh_requested_at = now()`; the bridge
    (Phase 10b/c) picks it up on its next poll, re-fetches tools,
    re-publishes the catalog, and clears the timestamp. Phase 10a
    sets the signal but no bridge consumes it yet — visible to
    operators via `last_connected_at` not changing."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            ok = await queries.mark_mcp_server_refresh_requested(conn, server_id)
            if not ok:
                raise HTTPException(404, "mcp server not found")
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="mcp_server.refresh_requested",
                target_kind="mcp_server", target_id=server_id,
            )
    return {"status": "refresh_requested"}


class McpToolsRefreshedRequest(BaseModel):
    """Bridge → router signal that an upstream `tools/list` refresh
    completed. The router atomically writes `tools_cache`, stamps
    `last_connected_at = now()`, and clears `refresh_requested_at`
    so the admin UI's "Refresh tools" click + the bridge's
    response form a complete loop."""

    tools_cache: dict[str, Any] = Field(default_factory=dict)
    """Verbatim MCP `tools/list` response shape — typically
    `{"tools": [{...}, ...]}`. The admin UI reads `tools_cache.tools`
    to render per-row tool counts; the router doesn't otherwise
    inspect the payload."""


@router.post("/mcp-servers/{server_id}/tools-refreshed", status_code=200)
async def record_mcp_tools_refreshed(
    server_id: str,
    req: McpToolsRefreshedRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Bridge callback after a successful upstream tools/list.

    Phase 10c's bridge calls this when:
      * It first connects to an MCP server (initial tools/list).
      * It observes `refresh_requested_at` non-null on its poll
        (admin clicked "Refresh tools" in the UI) and successfully
        re-fetches the list.

    The endpoint is admin-only for Phase 10c; Phase 10d will scope
    this to a dedicated service-level role with narrower
    permissions than full admin (the bridge currently needs admin
    for invitation issuance too, so the bigger surface lives at
    the bridge anyway)."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            ok = await queries.record_mcp_server_tools_refreshed(
                conn, server_id, tools_cache=req.tools_cache,
            )
            if not ok:
                raise HTTPException(404, "mcp server not found")
            await queries.append_audit_event(
                conn, actor_kind="admin", actor_id=principal.user_id,
                event="mcp_server.tools_refreshed",
                target_kind="mcp_server", target_id=server_id,
                payload={
                    "tool_count": len(
                        req.tools_cache.get("tools") or []
                    ),
                },
            )
    return {"status": "recorded"}
