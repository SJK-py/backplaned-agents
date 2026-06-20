"""Integration tests for the self-service webapp registration + later
channel-link flow (router side), driven over HTTP against a live router.

Covers:
  * `POST /v1/registrations/public` — anonymous self-service signup that
    stores the chosen password hash and attributes NO service submitter.
  * approval seeds that password (so `initial_password` is None) and grants
    NO `serviced_by`; the user can log in immediately with what they chose.
  * `POST /v1/auth/link-tokens` — a logged-in user mints a single-use link
    token for themselves.
  * `POST /v1/auth/link-channel` — a service principal consumes it and gains
    `serviced_by`; single-use; refused over a privileged target.
"""

from __future__ import annotations

import asyncio

import httpx

from bp_router.db import queries
from bp_router.security.jwt import issue_session_token
from bp_sdk.testing import TestRouter


def _session_token(router: TestRouter, *, user_id: str, level: str) -> str:
    s = router._app.state.bp.settings
    tok, _exp, _jti = issue_session_token(
        user_id=user_id,
        level=level,
        secret=s.jwt_secret.get_secret_value(),
        ttl_s=s.session_jwt_ttl_s,
        key_version=s.jwt_key_version,
        algorithm=s.jwt_algorithm,
    )
    return tok


def test_web_signup_approval_login_and_channel_link(test_db_url: str) -> None:
    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10.0
            ) as client:
                # 1) Anonymous self-service signup (no auth header).
                r = await client.post(
                    "/v1/registrations/public",
                    json={
                        "email": "Web.User@Example.com",
                        "password": "hunter2hunter",
                        "display_name": "Web User",
                    },
                )
                assert r.status_code == 201, r.text
                reg_id = r.json()["registration_id"]

                # Pending row: email lower-cased + used as external_id, the
                # chosen password stored HASHED, and NO service submitter (so
                # approval won't auto-grant serviced_by).
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM pending_user_registrations "
                        "WHERE registration_id = $1::uuid",
                        reg_id,
                    )
                assert row["channel"] == "webapp"
                assert row["external_id"] == "web.user@example.com"
                assert row["requested_email"] == "web.user@example.com"
                assert row["submitted_by_service_user_id"] is None
                assert row["requested_password_hash"] is not None
                assert row["requested_password_hash"] != "hunter2hunter"

                # 2) Admin approves.
                admin = await router.create_user(level="admin", email="admin@x.io")
                admin_tok = _session_token(
                    router, user_id=admin.user_id, level="admin"
                )
                r = await client.post(
                    f"/v1/admin/registrations/{reg_id}/approve",
                    headers={"Authorization": f"Bearer {admin_tok}"},
                    json={"level": "tier0"},
                )
                assert r.status_code == 200, r.text
                body = r.json()
                # The user chose their own password — nothing to reveal.
                assert body["initial_password"] is None
                new_user_id = body["user_id"]

                # No serviced_by on a self-service web signup.
                async with pool.acquire() as conn:
                    u = await queries.get_user_by_id(conn, new_user_id)
                assert u.serviced_by == []
                assert u.email == "web.user@example.com"

                # 3) The user logs in with the password they chose at signup.
                r = await client.post(
                    "/v1/auth/login",
                    json={"email": "web.user@example.com",
                          "password": "hunter2hunter"},
                )
                assert r.status_code == 200, r.text
                access = r.json()["access_token"]

                # 4) Self-service link-token mint (the user's own token).
                r = await client.post(
                    "/v1/auth/link-tokens",
                    headers={"Authorization": f"Bearer {access}"},
                )
                assert r.status_code == 201, r.text
                link_token = r.json()["link_token"]

                # 5) A channel service principal consumes it → gains
                #    serviced_by over the user.
                svc = await router.create_user(level="service", email="svc@chan.io")
                svc_tok = _session_token(
                    router, user_id=svc.user_id, level="service"
                )
                r = await client.post(
                    "/v1/auth/link-channel",
                    headers={"Authorization": f"Bearer {svc_tok}"},
                    json={"token": link_token},
                )
                assert r.status_code == 200, r.text
                assert r.json()["user_id"] == new_user_id

                async with pool.acquire() as conn:
                    u = await queries.get_user_by_id(conn, new_user_id)
                assert svc.user_id in u.serviced_by

                # 6) Single-use: replay is refused.
                r = await client.post(
                    "/v1/auth/link-channel",
                    headers={"Authorization": f"Bearer {svc_tok}"},
                    json={"token": link_token},
                )
                assert r.status_code == 401

    asyncio.run(_drive())


def test_link_channel_refuses_privileged_target(test_db_url: str) -> None:
    """A service principal must never gain serviced_by over an admin/service
    account — even holding a valid link token for it (same escalation guard
    as the F8/F9 mint endpoints). The refusal rolls back the consume, so the
    token stays valid but is permanently useless (every attempt 403s)."""

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10.0
            ) as client:
                # An admin mints a link token for THEMSELVES (self-service).
                victim = await router.create_user(
                    level="admin", email="priv@x.io"
                )
                victim_tok = _session_token(
                    router, user_id=victim.user_id, level="admin"
                )
                r = await client.post(
                    "/v1/auth/link-tokens",
                    headers={"Authorization": f"Bearer {victim_tok}"},
                )
                assert r.status_code == 201, r.text
                token = r.json()["link_token"]

                svc = await router.create_user(level="service", email="s2@chan.io")
                svc_tok = _session_token(
                    router, user_id=svc.user_id, level="service"
                )
                r = await client.post(
                    "/v1/auth/link-channel",
                    headers={"Authorization": f"Bearer {svc_tok}"},
                    json={"token": token},
                )
                assert r.status_code == 403, r.text

                # Not granted, and a replay is still refused (403) — the
                # token can never be turned into a grant over a privileged
                # target.
                async with pool.acquire() as conn:
                    u = await queries.get_user_by_id(conn, victim.user_id)
                assert svc.user_id not in u.serviced_by
                r = await client.post(
                    "/v1/auth/link-channel",
                    headers={"Authorization": f"Bearer {svc_tok}"},
                    json={"token": token},
                )
                assert r.status_code == 403

    asyncio.run(_drive())


def test_link_channel_verify_only_binds_without_grant(test_db_url: str) -> None:
    """grant_service=False is the verify-only mode: it consumes the token and
    returns the user_id (a bind) but grants NO serviced_by — and the
    privilege guard doesn't apply since nothing is granted."""

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10.0
            ) as client:
                user = await router.create_user(level="tier0", email="vo@x.io")
                user_tok = _session_token(
                    router, user_id=user.user_id, level="tier0"
                )
                r = await client.post(
                    "/v1/auth/link-tokens",
                    headers={"Authorization": f"Bearer {user_tok}"},
                )
                token = r.json()["link_token"]

                svc = await router.create_user(level="service", email="vo-s@x.io")
                svc_tok = _session_token(
                    router, user_id=svc.user_id, level="service"
                )
                r = await client.post(
                    "/v1/auth/link-channel",
                    headers={"Authorization": f"Bearer {svc_tok}"},
                    json={"token": token, "grant_service": False},
                )
                assert r.status_code == 200, r.text
                assert r.json()["user_id"] == user.user_id

                async with pool.acquire() as conn:
                    u = await queries.get_user_by_id(conn, user.user_id)
                assert svc.user_id not in u.serviced_by  # no grant

    asyncio.run(_drive())


def test_link_tokens_requires_authentication(test_db_url: str) -> None:
    """The self-service mint is gated on the caller's own session — an
    anonymous request can't mint a link token for anyone."""

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10.0
            ) as client:
                r = await client.post("/v1/auth/link-tokens")
                assert r.status_code in (401, 403), r.text

    asyncio.run(_drive())
