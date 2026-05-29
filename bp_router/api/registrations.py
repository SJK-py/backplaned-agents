"""bp_router.api.registrations — Channel-agnostic user-registration queue (F7).

Channel agents (webapp signup, Telegram bot, Discord bot, SMS shim,
etc.) submit pending registrations here on behalf of an
unauthenticated chat. The framework knows about `channel` as an
opaque slug and the per-(channel, external_id) idempotency / rate
shape; it knows NOTHING about specific channels.

Per-channel routing-index wire-up (e.g. "Telegram bot looks up
user_id by chat_id on every message") is operator-side work — read
the `registration.approved` audit event or add a downstream hook;
don't add `POST /v1/admin/suite-mappings` to the framework.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from bp_router.db import queries
from bp_router.security.jwt import SessionPrincipal, require_authenticated

logger = logging.getLogger(__name__)
router = APIRouter()


# Channel slug shape — DNS-friendly so it routes cleanly through URL
# segments and bucket keys. Mirrors the CHECK on
# `pending_user_registrations.channel`.
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_EXTERNAL_ID_MAX_LEN = 256
_DISPLAY_NAME_MAX_LEN = 256


class RegistrationSubmitRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=_EXTERNAL_ID_MAX_LEN)
    display_name: str | None = Field(default=None, max_length=_DISPLAY_NAME_MAX_LEN)
    requested_email: EmailStr | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("channel")
    @classmethod
    def _channel_shape(cls, v: str) -> str:
        if not _CHANNEL_RE.match(v):
            raise ValueError(
                "channel must match [a-z][a-z0-9_-]{0,31} "
                "(e.g. 'telegram', 'discord', 'sms-twilio')"
            )
        return v


class RegistrationSubmitResponse(BaseModel):
    registration_id: str
    status: str  # always "pending" today; reserved for future workflow states
    attempts: int


@router.post("", response_model=RegistrationSubmitResponse, status_code=201)
async def submit_registration(
    req: RegistrationSubmitRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_authenticated),
) -> RegistrationSubmitResponse:
    """Submit a pending registration. Idempotent on
    `(channel, external_id)` — re-submitting bumps `attempts` and
    refreshes `last_attempt_at`.

    Auth: any authenticated principal. Typically a service-level
    channel agent calling on behalf of an unauthenticated chat (the
    user being registered doesn't have an account yet). When the
    caller is `level="service"`, their `user_id` is recorded on the
    pending row as `submitted_by_service_user_id` — the F8 hook.
    Admin approval then auto-grants the channel agent servicing
    rights on the newly-created user (`users.serviced_by`).

    Rate-limit (two buckets via `state.login_quota`): a per-submitting-
    principal aggregate cap (bounds the enumerate-distinct-external_ids
    flood) AND the per-`(channel, external_id)` cap. Denial returns 429 +
    Retry-After; audited as `registration.rate_limited` with a `scope`.
    """
    state = request.app.state.bp
    settings = state.settings

    async def _deny(decision: Any, *, scope: str) -> None:
        """Audit + raise 429 for a rate-limit denial. `scope` distinguishes
        the per-submitter aggregate cap from the per-external one."""
        retry_after_s = max(decision.retry_after_s, 1.0)
        retry_after = max(int(retry_after_s + 0.999), 1)
        async with state.db_pool.acquire() as conn:
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=principal.user_id,
                event="registration.rate_limited",
                payload={
                    "channel": req.channel,
                    "external_id": req.external_id,
                    "retry_after_s": retry_after,
                    "scope": scope,
                },
            )
        raise HTTPException(
            status_code=429,
            detail="too many registration submissions; retry later",
            headers={"Retry-After": str(retry_after)},
        )

    # Aggregate cap per submitting principal FIRST — bounds the
    # enumerate-distinct-external_ids flood before it touches the DB.
    submitter_decision = await state.login_quota.try_consume(
        f"registration_submitter:{principal.user_id}",
        rate_per_s=settings.registration_rate_limit_per_submitter_per_s,
        burst=settings.registration_rate_limit_per_submitter_burst,
    )
    if not submitter_decision.allowed:
        await _deny(submitter_decision, scope="per_submitter")

    bucket_key = f"registration:{req.channel}:{req.external_id}"
    decision = await state.login_quota.try_consume(
        bucket_key,
        rate_per_s=settings.registration_rate_limit_per_external_per_s,
        burst=settings.registration_rate_limit_per_external_burst,
    )
    if not decision.allowed:
        await _deny(decision, scope="per_external")

    # Capture submitter's user_id ONLY if they're a service principal.
    # Admins / regular users submitting on behalf of someone are
    # legitimate but DON'T get auto-servicing rights — they'd have to
    # be added explicitly via the F8 admin grant endpoint.
    submitter_service_id = (
        principal.user_id if principal.level == "service" else None
    )

    async with state.db_pool.acquire() as conn:
        async with conn.transaction():
            # Rolling-window log (durable; cron sweep > 30d in
            # operator land).
            await queries.log_registration_attempt(
                conn, channel=req.channel, external_id=req.external_id,
            )
            pending = await queries.upsert_pending_registration(
                conn,
                channel=req.channel,
                external_id=req.external_id,
                display_name=req.display_name,
                requested_email=req.requested_email,
                metadata=req.metadata,
                submitted_by_service_user_id=submitter_service_id,
            )
            await queries.append_audit_event(
                conn, actor_kind="user", actor_id=principal.user_id,
                event="registration.submitted",
                target_kind="registration",
                target_id=pending.registration_id,
                payload={
                    "channel": req.channel,
                    "external_id": req.external_id,
                    "attempts": pending.attempts,
                },
            )

    return RegistrationSubmitResponse(
        registration_id=pending.registration_id,
        status="pending",
        attempts=pending.attempts,
    )
