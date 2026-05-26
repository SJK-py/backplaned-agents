"""bp_router.api.onboard — Agent registration and token rotation."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.types import AGENT_ID_PATTERN, AgentInfo
from bp_router.db import queries
from bp_router.principals import service_user_id_for_agent
from bp_router.security.jwt import (
    TokenError,
    extract_bearer,
    is_jti_revoked,
    issue_agent_token,
    revoke_jti,
    verify_agent_token,
)
from bp_router.visibility import available_destinations

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class OnboardRequest(BaseModel):
    invitation_token: str
    agent_info: AgentInfo
    public_key: str | None = None


class OnboardResponse(BaseModel):
    agent_id: str
    auth_token: str
    expires_at: datetime
    available_destinations: dict[str, Any]
    # Populated only when the consumed invitation had
    # `provisions_service_user=true`. The agent persists these and uses
    # the refresh token (redeemable at `/v1/auth/refresh`) to act as its
    # co-located `level=service` principal for HTTP control-plane ops.
    service_user_id: str | None = None
    service_refresh_token: str | None = None
    service_token_expires_at: datetime | None = None


class RefreshAgentTokenRequest(BaseModel):
    agent_id: str


class RefreshAgentTokenResponse(BaseModel):
    agent_id: str
    auth_token: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/onboard", response_model=OnboardResponse)
async def onboard(req: OnboardRequest, request: Request) -> OnboardResponse:
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    invitation_hash = _hash_token(req.invitation_token)

    # 1. agent_id grammar enforcement — before duplicate detection so an
    #    illegal id never reaches the DB CHECK. AgentInfo's Pydantic
    #    validator already enforces this on inbound parsing; the explicit
    #    check here gives a clearer 400 error than relying on parser
    #    failure at frame edges.
    if not AGENT_ID_PATTERN.match(req.agent_info.agent_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "agent_id must match [A-Za-z_][A-Za-z0-9_-]{0,63} — "
                "see docs/acl.md §10"
            ),
        )

    # Populated only when a service-provisioning invitation is consumed.
    service_user_id: str | None = None
    service_refresh_token: str | None = None
    service_token_expires_at: datetime | None = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE so concurrent onboards of the same agent_id
            # can't both pass.
            existing = await queries.get_agent_for_update(
                conn, req.agent_info.agent_id
            )
            if existing is not None and existing.status != "pending":
                # Audit, but DO NOT consume the invitation token —
                # the admin's invitation should remain usable.
                await queries.append_audit_event(
                    conn,
                    actor_kind="agent",
                    actor_id=req.agent_info.agent_id,
                    event="agent.onboard_rejected",
                    target_kind="agent",
                    target_id=req.agent_info.agent_id,
                    payload={
                        "reason": "already_registered",
                        "status": existing.status,
                    },
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"agent {req.agent_info.agent_id!r} already registered",
                )

            # Now safe to consume the invitation — only burns it on a
            # request that would otherwise have created the agent.
            invitation = await queries.consume_invitation(
                conn,
                token_hash=invitation_hash,
                used_by=req.agent_info.agent_id,
            )
            if invitation is None:
                # Wrong / used / expired token. Audit so admins can
                # investigate stolen-token scenarios.
                await queries.append_audit_event(
                    conn,
                    actor_kind="agent",
                    actor_id=req.agent_info.agent_id,
                    event="auth.invitation_rejected",
                    payload={"agent_id": req.agent_info.agent_id},
                )
                raise HTTPException(
                    status_code=403, detail="invalid or used invitation token"
                )

            # AgentInfo carries identity only (groups + capabilities); the
            # invitation's `level` is metadata about who issued the
            # invitation and does not propagate onto the agent.
            if existing is None:
                agent_row = await queries.insert_agent(
                    conn,
                    agent_id=req.agent_info.agent_id,
                    kind="external",
                    capabilities=req.agent_info.capabilities,
                    groups=list(req.agent_info.groups),
                    agent_info=req.agent_info.model_dump(),
                    public_key=req.public_key,
                )
            else:
                # status == 'pending' fall-through: keep the row.
                agent_row = existing

            await queries.append_audit_event(
                conn,
                actor_kind="agent",
                actor_id=req.agent_info.agent_id,
                event="agent.onboarded",
                target_kind="agent",
                target_id=req.agent_info.agent_id,
                payload={"invitation_level": invitation.get("level")},
            )

            # Co-located service principal (`usr_service_{agent_id}`),
            # provisioned in the SAME transaction as the agent insert so
            # the two are atomic. The runtime privilege boundary is
            # unchanged: this is an ordinary `level=service` user, so the
            # `serviced_by` mint endpoint (with its rate limits and
            # privileged-target refusal) still gates everything it can do.
            if invitation.get("provisions_service_user"):
                service_user_id = service_user_id_for_agent(agent_row.agent_id)
                existing_svc = await queries.get_user_by_id(
                    conn, service_user_id
                )
                if existing_svc is None:
                    await queries.insert_user(
                        conn,
                        user_id=service_user_id,
                        email=None,
                        level="service",
                        auth_kind="api_key",
                        auth_secret_hash=None,
                    )
                elif (
                    existing_svc.level != "service"
                    or existing_svc.deleted_at is not None
                ):
                    # A non-service or soft-deleted row under the reserved
                    # name is a conflict we refuse to silently reuse.
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"co-located service principal {service_user_id!r} "
                            "exists in a conflicting state"
                        ),
                    )
                # Mint a fresh refresh token. Recovery-safe: a re-onboard
                # re-mints rather than resurrecting a stale credential.
                service_refresh_token = secrets.token_urlsafe(32)
                service_token_expires_at = datetime.now(UTC) + timedelta(
                    seconds=settings.refresh_token_ttl_s
                )
                await queries.insert_refresh_token(
                    conn,
                    token_hash=_hash_token(service_refresh_token),
                    user_id=service_user_id,
                    expires_at=service_token_expires_at,
                )
                await queries.append_audit_event(
                    conn,
                    actor_kind="agent",
                    actor_id=agent_row.agent_id,
                    event="agent.service_principal_provisioned",
                    target_kind="user",
                    target_id=service_user_id,
                    payload={"reused": existing_svc is not None},
                )

    # Issue the agent JWT.
    token, expires_at, jti = issue_agent_token(
        agent_id=agent_row.agent_id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.agent_token_ttl_s,
        key_version=settings.jwt_key_version,
        protocol_version=PROTOCOL_VERSION,
        algorithm=settings.jwt_algorithm,
    )

    # Compute initial visible catalog.
    async with pool.acquire() as conn:
        all_agents = await queries.list_agents(conn)
    catalog = available_destinations(
        agent_row,
        all_agents,
        state.rules.rules,
        max_tier=settings.acl_max_tier,
    )

    # Push CatalogUpdate to currently-connected peers so they see the
    # new agent without waiting for a reconnect.
    from bp_router.catalog import push_catalog_update_to_all  # noqa: PLC0415

    await push_catalog_update_to_all(state)

    logger.info(
        "agent_onboarded",
        extra={"event": "agent_onboarded", "bp.agent_id": agent_row.agent_id},
    )
    return OnboardResponse(
        agent_id=agent_row.agent_id,
        auth_token=token,
        expires_at=expires_at,
        available_destinations=catalog,
        service_user_id=service_user_id,
        service_refresh_token=service_refresh_token,
        service_token_expires_at=service_token_expires_at,
    )


@router.post("/agent/refresh-token", response_model=RefreshAgentTokenResponse)
async def refresh_agent_token(
    req: RefreshAgentTokenRequest,
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
) -> RefreshAgentTokenResponse:
    """Rotate an agent's auth token. Requires the current valid token."""
    token = extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")

    state = request.app.state.bp
    settings = state.settings

    try:
        principal = verify_agent_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    # Revocation check uses per-jti EXISTS.
    if await is_jti_revoked(state.redis, principal.jti):
        raise HTTPException(status_code=401, detail="invalid token")

    if principal.agent_id != req.agent_id:
        raise HTTPException(status_code=403, detail="agent_id mismatch")

    # An evicted/suspended agent's JWT is technically still valid until
    # natural expiry; do not let it rotate. The handshake guard alone is
    # not enough — refresh-token paths must also reject non-active rows.
    pool = state.db_pool
    async with pool.acquire() as conn:
        agent_row = await queries.get_agent(conn, principal.agent_id)
    if agent_row is None or agent_row.status != "active":
        raise HTTPException(
            status_code=403,
            detail=f"agent not active (status={agent_row.status if agent_row else 'unknown'})",
        )

    # Issue a new token.
    new_token, expires_at, _jti = issue_agent_token(
        agent_id=principal.agent_id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.agent_token_ttl_s,
        key_version=settings.jwt_key_version,
        protocol_version=PROTOCOL_VERSION,
        algorithm=settings.jwt_algorithm,
    )

    # Revoke the OLD jti so a leaked agent token can't keep talking
    # to the router after rotation. TTL = `max(remaining, agent_token_ttl_s)`
    # — defence-in-depth, NOT just "remaining lifetime".
    # The previous comment claimed "Redis evicts
    # naturally when the JWT would have expired anyway", but the
    # actual `max(...)` form keeps the entry revoked at LEAST for
    # the default TTL even when the token only had seconds left.
    # That's strictly more conservative — a future maintainer should
    # not "fix" this to `min(...)`, which would shrink the
    # revocation window for almost-expired tokens.
    if state.redis is not None:
        # `principal.expires_at` is tz-aware (UTC). Use a tz-aware
        # `now` so the subtraction is well-defined and we don't get
        # a `TypeError: can't subtract offset-naive and offset-aware`.
        ttl_s = max(
            int(
                (principal.expires_at - datetime.now(UTC))
                .total_seconds()
            ),
            settings.agent_token_ttl_s,
        )
        await revoke_jti(state.redis, principal.jti, ttl_s=ttl_s)

    # Force-close any live WS socket authenticated under the old jti.
    # Without this, the agent's existing socket
    # keeps working until natural JWT expiry — defeating the whole
    # point of rotation. The agent's reconnect loop will re-handshake
    # with the new token within seconds.
    socket_registry = getattr(state, "socket_registry", None)
    if socket_registry is not None:
        entry = socket_registry.get(principal.agent_id)
        if entry is not None and getattr(entry, "auth_jti", None) == principal.jti:
            try:
                await entry.websocket.close(
                    code=4001, reason="auth_token_rotated"
                )
            except Exception:  # noqa: BLE001
                # Close is best-effort — the next heartbeat round
                # will detect the dead socket and clean up.
                logger.debug(
                    "socket_close_after_rotate_failed",
                    extra={"event": "socket_close_after_rotate_failed",
                           "bp.agent_id": principal.agent_id},
                    exc_info=True,
                )
            entry.closed.set()

    return RefreshAgentTokenResponse(
        agent_id=principal.agent_id,
        auth_token=new_token,
        expires_at=expires_at,
    )


