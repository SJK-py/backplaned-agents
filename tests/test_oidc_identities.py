"""Phase 1a — the `user_oidc_identities` table + its query layer.

`(issuer, sub) → user_id` resolution, multi-IdP per account, the
one-identity-one-account uniqueness guard, the reverse list/unlink, and
PII erasure on purge. Driven against a live router DB.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from bp_router.db import queries
from bp_sdk.testing import TestRouter

# --- shape pins (no DB) ----------------------------------------------------


def test_oidc_identity_row_model() -> None:
    from bp_router.db.models import OidcIdentityRow

    fields = OidcIdentityRow.model_fields
    for name in ("issuer", "sub", "user_id", "email_at_link",
                 "created_at", "last_login_at"):
        assert name in fields


def test_purge_user_erases_oidc_identities() -> None:
    """Purge must drop the linked-identity rows (issuer/sub/email are PII)."""
    src = inspect.getsource(queries.purge_user)
    assert "DELETE FROM user_oidc_identities WHERE user_id" in src


# --- behaviour (DB) --------------------------------------------------------


def test_link_resolve_list_unlink_roundtrip(test_db_url: str) -> None:
    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with pool.acquire() as conn:
                u = await queries.insert_user(
                    conn, level="tier1", auth_kind="oidc",
                    auth_secret_hash=None, email="u@x.io",
                )

                # Unknown subject → None.
                assert await queries.get_user_by_oidc_sub(
                    conn, issuer="https://op", sub="abc"
                ) is None

                # Link two different OPs to the SAME account.
                await queries.link_oidc_identity(
                    conn, issuer="https://op", sub="abc",
                    user_id=u.user_id, email_at_link="u@x.io",
                )
                await queries.link_oidc_identity(
                    conn, issuer="https://op2", sub="xyz", user_id=u.user_id,
                )

                # Both resolve back to the one user.
                r1 = await queries.get_user_by_oidc_sub(
                    conn, issuer="https://op", sub="abc"
                )
                r2 = await queries.get_user_by_oidc_sub(
                    conn, issuer="https://op2", sub="xyz"
                )
                assert r1 is not None and r1.user_id == u.user_id
                assert r2 is not None and r2.user_id == u.user_id

                # Reverse list shows both.
                ids = await queries.list_oidc_identities_for_user(conn, u.user_id)
                assert {(i.issuer, i.sub) for i in ids} == {
                    ("https://op", "abc"), ("https://op2", "xyz")
                }

                # Unlink one (scoped to the owner) — the other survives.
                assert await queries.unlink_oidc_identity(
                    conn, user_id=u.user_id, issuer="https://op", sub="abc"
                ) is True
                assert await queries.get_user_by_oidc_sub(
                    conn, issuer="https://op", sub="abc"
                ) is None
                assert await queries.get_user_by_oidc_sub(
                    conn, issuer="https://op2", sub="xyz"
                ) is not None
                # Unlinking again is a no-op.
                assert await queries.unlink_oidc_identity(
                    conn, user_id=u.user_id, issuer="https://op", sub="abc"
                ) is False

    asyncio.run(_drive())


def test_same_subject_two_accounts_is_rejected(test_db_url: str) -> None:
    """`(issuer, sub)` maps to exactly one account: a second account
    claiming the same subject is refused (PK / WHERE-guard), so a service
    can't hijack a subject by re-linking it."""

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with pool.acquire() as conn:
                a = await queries.insert_user(
                    conn, level="tier1", auth_kind="oidc",
                    auth_secret_hash=None, email="a@x.io",
                )
                b = await queries.insert_user(
                    conn, level="tier1", auth_kind="oidc",
                    auth_secret_hash=None, email="b@x.io",
                )
                await queries.link_oidc_identity(
                    conn, issuer="https://op", sub="dup", user_id=a.user_id
                )
                with pytest.raises(Exception):  # noqa: B017,PT011
                    await queries.link_oidc_identity(
                        conn, issuer="https://op", sub="dup", user_id=b.user_id
                    )

    asyncio.run(_drive())


def test_relink_same_user_is_idempotent(test_db_url: str) -> None:
    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with pool.acquire() as conn:
                u = await queries.insert_user(
                    conn, level="tier1", auth_kind="oidc",
                    auth_secret_hash=None, email="u@x.io",
                )
                await queries.link_oidc_identity(
                    conn, issuer="https://op", sub="s", user_id=u.user_id
                )
                # Same (issuer, sub, user) again — refreshes, no error/dup.
                row = await queries.link_oidc_identity(
                    conn, issuer="https://op", sub="s", user_id=u.user_id,
                    email_at_link="u@x.io",
                )
                assert row.user_id == u.user_id
                assert row.last_login_at is not None
                ids = await queries.list_oidc_identities_for_user(conn, u.user_id)
                assert len(ids) == 1

    asyncio.run(_drive())


def test_purge_user_removes_identities(test_db_url: str) -> None:
    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with pool.acquire() as conn:
                async with conn.transaction():
                    u = await queries.insert_user(
                        conn, level="tier1", auth_kind="oidc",
                        auth_secret_hash=None, email="purge@x.io",
                    )
                    await queries.link_oidc_identity(
                        conn, issuer="https://op", sub="p", user_id=u.user_id
                    )
                    await queries.purge_user(conn, u.user_id, actor_id=None)
                # The identity is gone → SSO can't resolve to the tombstone.
                assert await queries.get_user_by_oidc_sub(
                    conn, issuer="https://op", sub="p"
                ) is None

    asyncio.run(_drive())
