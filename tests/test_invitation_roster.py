"""Invitation rosters — `docs/design/deployment-agent-host.md` §3.

One token that may onboard a listed set of agent names, once each, replacing
one single-use token per agent. The property worth protecting is that this
is *tighter* than what it replaces: today's unbound invitation can onboard as
ANY name, because `POST /v1/onboard` reads the name from the agent's own
`agent_info` and `invitations` has no name column at all.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _admin(conn: asyncpg.Connection) -> str:
    """A fresh admin to own the invitations. Invitations go first —
    `invitations.created_by` has no ON DELETE CASCADE, so the user row
    cannot go while one references it."""
    await conn.execute("DELETE FROM invitations WHERE created_by = 'u_rosteradmin'")
    await conn.execute("DELETE FROM users WHERE user_id = 'u_rosteradmin'")
    await conn.execute(
        "INSERT INTO users (user_id, level, auth_kind) "
        "VALUES ('u_rosteradmin','admin','password')"
    )
    return "u_rosteradmin"


def test_unbound_invitation_still_burns_on_first_use(test_db_url: str) -> None:
    """The existing shape must be untouched: no roster → single use, any
    name."""

    async def go() -> None:
        from bp_router.db import queries

        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            admin = await _admin(conn)
            token = "tok_unbound"
            await queries.insert_invitation(
                conn,
                token_hash=_hash(token),
                level="tier1",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_by=admin,
            )
            first = await queries.consume_invitation(
                conn, token_hash=_hash(token), used_by="anything_at_all"
            )
            assert first is not None and first["level"] == "tier1"
            second = await queries.consume_invitation(
                conn, token_hash=_hash(token), used_by="another"
            )
            assert second is None, "unbound invitations stay single-use"
            await conn.execute(
                "DELETE FROM invitations WHERE created_by = $1", admin
            )
            await conn.execute("DELETE FROM users WHERE user_id = $1", admin)
        await pool.close()

    _run(go())


def test_roster_token_serves_each_listed_name_once(test_db_url: str) -> None:
    async def go() -> None:
        from bp_router.db import queries

        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            admin = await _admin(conn)
            token = "tok_roster"
            await queries.insert_invitation(
                conn,
                token_hash=_hash(token),
                level="tier1",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_by=admin,
                agent_ids=["orchestrator", "research", "memory"],
            )
            # Each listed name onboards once, off ONE token.
            for name in ("orchestrator", "research", "memory"):
                claims = await queries.consume_invitation(
                    conn, token_hash=_hash(token), used_by=name
                )
                assert claims is not None, name

            # ...and not twice.
            assert (
                await queries.consume_invitation(
                    conn, token_hash=_hash(token), used_by="research"
                )
                is None
            )
            # Exhausted rosters are stamped `used_at` so the existing GC
            # sweep reaps them like any other spent invitation.
            used_at = await conn.fetchval(
                "SELECT used_at FROM invitations WHERE token_hash = $1", _hash(token)
            )
            assert used_at is not None
            await conn.execute(
                "DELETE FROM invitations WHERE created_by = $1", admin
            )
            await conn.execute("DELETE FROM users WHERE user_id = $1", admin)
        await pool.close()

    _run(go())


def test_roster_refuses_a_name_it_does_not_list(test_db_url: str) -> None:
    """The tightening. An unbound token can onboard as any name; a roster
    token can only produce what the operator listed."""

    async def go() -> None:
        from bp_router.db import queries

        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            admin = await _admin(conn)
            token = "tok_tight"
            await queries.insert_invitation(
                conn,
                token_hash=_hash(token),
                level="tier1",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_by=admin,
                agent_ids=["orchestrator"],
            )
            assert (
                await queries.consume_invitation(
                    conn, token_hash=_hash(token), used_by="sandbox"
                )
                is None
            ), "a roster token must not produce an unlisted agent"
            # The refused attempt must not have consumed anything.
            assert (
                await queries.consume_invitation(
                    conn, token_hash=_hash(token), used_by="orchestrator"
                )
                is not None
            )
            await conn.execute(
                "DELETE FROM invitations WHERE created_by = $1", admin
            )
            await conn.execute("DELETE FROM users WHERE user_id = $1", admin)
        await pool.close()

    _run(go())


def test_roster_stays_live_until_exhausted(test_db_url: str) -> None:
    """A partially-provisioned group must heal on restart: agents holding
    credentials skip onboarding, agents without take their remaining slot."""

    async def go() -> None:
        from bp_router.db import queries

        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            admin = await _admin(conn)
            token = "tok_partial"
            await queries.insert_invitation(
                conn,
                token_hash=_hash(token),
                level="tier1",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_by=admin,
                agent_ids=["a1", "a2"],
            )
            assert await queries.consume_invitation(
                conn, token_hash=_hash(token), used_by="a1"
            )
            # Still usable — this is what one-token-per-agent could not do.
            used_at = await conn.fetchval(
                "SELECT used_at FROM invitations WHERE token_hash = $1", _hash(token)
            )
            assert used_at is None
            assert await queries.consume_invitation(
                conn, token_hash=_hash(token), used_by="a2"
            )
            await conn.execute(
                "DELETE FROM invitations WHERE created_by = $1", admin
            )
            await conn.execute("DELETE FROM users WHERE user_id = $1", admin)
        await pool.close()

    _run(go())


def test_expired_roster_is_refused(test_db_url: str) -> None:
    async def go() -> None:
        from bp_router.db import queries

        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            admin = await _admin(conn)
            token = "tok_expired"
            await queries.insert_invitation(
                conn,
                token_hash=_hash(token),
                level="tier1",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                created_by=admin,
                agent_ids=["a1"],
            )
            assert (
                await queries.consume_invitation(
                    conn, token_hash=_hash(token), used_by="a1"
                )
                is None
            )
            await conn.execute(
                "DELETE FROM invitations WHERE created_by = $1", admin
            )
            await conn.execute("DELETE FROM users WHERE user_id = $1", admin)
        await pool.close()

    _run(go())


def test_admin_endpoint_accepts_a_roster() -> None:
    from bp_router.api.admin import IssueInvitationRequest

    req = IssueInvitationRequest(level="tier1", agent_ids=["a", "b"])
    assert req.agent_ids == ["a", "b"]
    # Absent by default — every existing caller is unaffected.
    assert IssueInvitationRequest(level="tier1").agent_ids is None


def test_onboard_still_derives_the_name_from_agent_info() -> None:
    """The roster narrows WHICH names a token may produce; it does not change
    where the name comes from. If that ever inverts, the roster check is
    checking the wrong thing."""
    import inspect

    pytest.importorskip("fastapi")
    from bp_router.api import onboard

    src = inspect.getsource(onboard)
    assert "used_by=req.agent_info.agent_id" in src
