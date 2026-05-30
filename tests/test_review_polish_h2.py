"""Second-pass low-priority polish — H2.

F3 — the registration rate-limit DENIAL path wrote one audit_log row per
     rejected request. A capped attacker hammering at full rate just moved the
     unbounded-growth DoS from pending_user_registrations onto the
     hash-chained audit_log (whose append serializes on a global advisory
     lock). The denial-audit write is now dampened per actor (the same gate
     the admin denial audits use); the 429 always fires.

F5 — a recv-loop death that happened DURING the shutdown drain (transport
     dies mid-grace) was masked by the `finally` cancel, so run_until returned
     cleanly instead of surfacing TransportPermanentlyFailed. It is now
     re-checked after the drain.
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

# --- source pins ------------------------------------------------------------


def test_deny_gates_audit_write_behind_dampener() -> None:
    src = inspect.getsource(registrations.submit_registration)
    # The audit append must sit behind the per-actor dampener gate.
    assert "_denial_audit_allowed(" in src
    gate = src.find("_denial_audit_allowed(")
    append = src.find("append_audit_event", gate)
    assert 0 < gate < append, "audit append must be guarded by the dampener"


def test_run_until_rechecks_recv_after_drain() -> None:
    from bp_sdk import dispatch

    src = inspect.getsource(dispatch.Dispatcher.run_until)
    drain = src.find("_drain_in_flight(")
    recheck = src.find("recv_loop.done()", drain)
    raise_idx = src.find("raise recv_death")
    assert 0 < drain < recheck < raise_idx, (
        "a recv-loop death during the drain must be captured after "
        "_drain_in_flight and surfaced via `raise recv_death`"
    )


# --- F3 behavioural: denial audit is bounded, 429 always fires --------------


class _DampenerQuota:
    """Per-submitter bucket always denies (so every call hits `_deny`); the
    audit-dampener bucket allows `audit_burst` writes then denies."""

    def __init__(self, *, audit_burst: int) -> None:
        self._audit_left = audit_burst

    async def try_consume(self, key: str, *, rate_per_s, burst) -> Decision:  # type: ignore[no-untyped-def]
        if key.startswith("audit_denial:"):
            if self._audit_left > 0:
                self._audit_left -= 1
                return Decision(allowed=True, retry_after_s=0.0, tokens_remaining=self._audit_left)
            return Decision(allowed=False, retry_after_s=5.0, tokens_remaining=0.0)
        # registration_submitter:* (and any other) → deny → forces _deny.
        return Decision(allowed=False, retry_after_s=42.0, tokens_remaining=0.0)


def test_denial_audit_is_dampened(test_db_url: str) -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.security.jwt import SessionPrincipal

    async def _init(conn: asyncpg.Connection) -> None:
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
            settings = SimpleNamespace(
                registration_rate_limit_per_submitter_per_s=1.0,
                registration_rate_limit_per_submitter_burst=1,
                registration_rate_limit_per_external_per_s=999.0,
                registration_rate_limit_per_external_burst=999,
            )
            audit_burst = 3
            state = SimpleNamespace(
                settings=settings,
                login_quota=_DampenerQuota(audit_burst=audit_burst),
                db_pool=pool,
            )
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(bp=state))
            )
            principal = SessionPrincipal(
                user_id="usr_flooder",
                level="tier0",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                jti="j",
            )
            req = registrations.RegistrationSubmitRequest(
                channel="telegram", external_id="ext"
            )

            calls = 8
            denied = 0
            for _ in range(calls):
                with pytest.raises(HTTPException) as ei:
                    await registrations.submit_registration(req, request, principal)
                assert ei.value.status_code == 429  # 429 ALWAYS fires
                denied += 1
            assert denied == calls

            async with pool.acquire() as conn:
                n = await conn.fetchval(
                    "SELECT count(*) FROM audit_log "
                    "WHERE event = 'registration.rate_limited'"
                )
            # Audit rows are bounded by the dampener burst, NOT one-per-request.
            assert n == audit_burst, f"expected {audit_burst} audit rows, got {n}"
        finally:
            await pool.close()

    asyncio.run(_drive())
