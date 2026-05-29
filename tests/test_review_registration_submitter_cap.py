"""Registration submit is bounded per submitting principal, not just per
`(channel, external_id)`.

Pre-release review: the rate limit keyed only on `registration:{channel}:
{external_id}`, so one authenticated caller could enumerate distinct
`external_id`s — each getting a fresh bucket — to create unbounded
`pending_user_registrations` rows (a table-growth / admin-queue-flood DoS).

Fix: a second bucket keyed on the submitting principal
(`registration_submitter:{user_id}`) is checked FIRST, bounding the aggregate
rate one principal can create registrations at across all external_ids.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import pytest

from bp_router.api import registrations
from bp_router.security.rate_limit import Decision

# --------------------------------------------------------------------------- #
# Source pins
# --------------------------------------------------------------------------- #


def test_submitter_bucket_checked_before_external() -> None:
    src = inspect.getsource(registrations.submit_registration)
    sub_idx = src.find('f"registration_submitter:{principal.user_id}"')
    ext_idx = src.find('f"registration:{req.channel}:{req.external_id}"')
    assert sub_idx > 0, "per-submitter bucket must exist"
    assert ext_idx > 0
    assert sub_idx < ext_idx, (
        "the per-submitter aggregate cap must be checked BEFORE the "
        "per-external one, so the enumerate-external_ids flood is rejected "
        "before touching the DB"
    )
    assert "registration_rate_limit_per_submitter_per_s" in src
    assert "registration_rate_limit_per_submitter_burst" in src


def test_settings_expose_per_submitter_knobs() -> None:
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "registration_rate_limit_per_submitter_per_s" in fields
    assert "registration_rate_limit_per_submitter_burst" in fields
    assert fields["registration_rate_limit_per_submitter_per_s"].default >= 0.0
    assert fields["registration_rate_limit_per_submitter_burst"].default >= 1


# --------------------------------------------------------------------------- #
# Behavioural: the per-submitter cap bounds distinct-external enumeration
# --------------------------------------------------------------------------- #


class _SubmitterCapQuota:
    """Fake `login_quota`: the per-submitter bucket allows `burst` calls then
    denies; the per-external bucket always allows (so denial can only come
    from the aggregate cap)."""

    def __init__(self, *, burst: int) -> None:
        self._left = burst

    async def try_consume(self, key: str, *, rate_per_s, burst) -> Decision:  # type: ignore[no-untyped-def]
        if key.startswith("registration_submitter:"):
            if self._left > 0:
                self._left -= 1
                return Decision(allowed=True, retry_after_s=0.0, tokens_remaining=self._left)
            return Decision(allowed=False, retry_after_s=42.0, tokens_remaining=0.0)
        return Decision(allowed=True, retry_after_s=0.0, tokens_remaining=99.0)


def test_per_submitter_cap_blocks_external_id_enumeration(test_db_url: str) -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.security.jwt import SessionPrincipal

    async def _init(conn: asyncpg.Connection) -> None:
        # The router pool registers a jsonb codec; mirror it so dict params
        # (registration metadata, audit payloads) serialize.
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    async def _drive() -> None:
        pool = await asyncpg.create_pool(test_db_url, init=_init)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE pending_user_registrations, registration_attempts, "
                    "audit_log RESTART IDENTITY CASCADE"
                )

            # Only the four rate fields are read by the handler; the fake
            # quota ignores the values, so a minimal stand-in is enough.
            settings = SimpleNamespace(
                registration_rate_limit_per_submitter_per_s=1.0,
                registration_rate_limit_per_submitter_burst=2,
                registration_rate_limit_per_external_per_s=999.0,
                registration_rate_limit_per_external_burst=999,
            )
            state = SimpleNamespace(
                settings=settings,
                login_quota=_SubmitterCapQuota(burst=2),
                db_pool=pool,
            )
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(bp=state)))
            # tier0 (not service) → submitted_by_service_user_id stays NULL,
            # so no users-FK row is needed for the test.
            principal = SessionPrincipal(
                user_id="usr_attacker",
                level="tier0",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                jti="j",
            )

            def _req(ext: str):  # type: ignore[no-untyped-def]
                return registrations.RegistrationSubmitRequest(
                    channel="telegram", external_id=ext
                )

            # First two DISTINCT external_ids succeed (per-external never trips
            # in this fake — only the aggregate cap can deny).
            await registrations.submit_registration(_req("ext_0"), request, principal)
            await registrations.submit_registration(_req("ext_1"), request, principal)

            # The third distinct external_id is refused by the aggregate cap.
            with pytest.raises(HTTPException) as ei:
                await registrations.submit_registration(_req("ext_2"), request, principal)
            assert ei.value.status_code == 429

            # Only the two allowed submissions created rows — enumeration is
            # bounded, not unbounded.
            async with pool.acquire() as conn:
                n = await conn.fetchval("SELECT count(*) FROM pending_user_registrations")
            assert n == 2
        finally:
            await pool.close()

    asyncio.run(_drive())
