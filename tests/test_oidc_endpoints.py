"""Phase 1c — the router OIDC back-channel endpoints + provisioning.

Boots a real router with SSO enabled (env), then injects an `OidcProvider`
backed by an `httpx.MockTransport` fake OP that returns really-signed
id_tokens. Exercises authorize, exchange→provision→issue, JIT vs
match-only, group→level mapping, the redirect-URI allowlist, and gated
email auto-link.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from bp_router.db import queries
from bp_sdk.testing import TestRouter

_ISSUER = "https://op.example"
_CLIENT = "test-client"
_REDIRECT = "https://app.test/cb"


def _op():
    pytest.importorskip("cryptography")
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update(kid="k1", alg="RS256", use="sig")

    def make(claims: dict) -> str:
        return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "k1"})

    return jwk, make


def _fake_provider(jwk: dict, id_token: str):
    from bp_router.security.oidc import OidcProvider

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": _ISSUER,
                "authorization_endpoint": f"{_ISSUER}/authorize",
                "token_endpoint": f"{_ISSUER}/token",
                "jwks_uri": f"{_ISSUER}/jwks",
            })
        if path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [jwk]})
        if path.endswith("/token"):
            return httpx.Response(200, json={"id_token": id_token, "access_token": "a"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OidcProvider(
        issuer=_ISSUER, client_id=_CLIENT, client_secret="s",
        scopes="openid email profile groups", http=client,
    )


def _enable(monkeypatch, **over) -> None:
    monkeypatch.setenv("ROUTER_OIDC_ENABLED", "true")
    monkeypatch.setenv("ROUTER_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("ROUTER_OIDC_CLIENT_ID", _CLIENT)
    monkeypatch.setenv("ROUTER_OIDC_CLIENT_SECRET", "sek")
    monkeypatch.setenv("ROUTER_OIDC_ALLOWED_REDIRECT_URIS", json.dumps([_REDIRECT]))
    for k, v in over.items():
        monkeypatch.setenv(k, v)


def _claims(**over) -> dict:
    now = int(time.time())
    return {
        "iss": _ISSUER, "aud": _CLIENT, "sub": "sub-1", "iat": now,
        "exp": now + 300, "nonce": "N", "email": "a@b.c",
        "email_verified": True, "groups": ["admins"], **over,
    }


def _exchange_body(nonce: str = "N") -> dict:
    return {"code": "x", "code_verifier": "v", "nonce": nonce,
            "redirect_uri": _REDIRECT}


def test_exchange_provisions_with_group_level_then_logs_in(
    test_db_url: str, monkeypatch
) -> None:
    _enable(monkeypatch, ROUTER_OIDC_GROUP_TO_LEVEL=json.dumps({"admins": "tier0"}))

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(jwk, make(_claims()))
            pool = router._app.state.bp.db_pool
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post("/v1/auth/oidc/exchange", json=_exchange_body())
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["level"] == "tier0"  # group→level mapping won
                assert body["access_token"] and body["refresh_token"]

                # Linked + provisioned.
                async with pool.acquire() as conn:
                    u = await queries.get_user_by_oidc_sub(
                        conn, issuer=_ISSUER, sub="sub-1"
                    )
                    assert u is not None and u.level == "tier0"
                    n_users = await conn.fetchval("SELECT count(*) FROM users")

                # Second login = same account, no new user.
                r2 = await c.post("/v1/auth/oidc/exchange", json=_exchange_body())
                assert r2.status_code == 200
                async with pool.acquire() as conn:
                    assert await conn.fetchval("SELECT count(*) FROM users") == n_users

    asyncio.run(_drive())


def test_default_level_when_no_group_match(test_db_url: str, monkeypatch) -> None:
    _enable(monkeypatch, ROUTER_OIDC_DEFAULT_LEVEL="tier2")

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(
                jwk, make(_claims(groups=[]))
            )
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post("/v1/auth/oidc/exchange", json=_exchange_body())
                assert r.status_code == 200, r.text
                assert r.json()["level"] == "tier2"

    asyncio.run(_drive())


def test_match_only_rejects_unknown_subject(test_db_url: str, monkeypatch) -> None:
    _enable(monkeypatch, ROUTER_OIDC_JIT_PROVISIONING="false")

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(jwk, make(_claims()))
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post("/v1/auth/oidc/exchange", json=_exchange_body())
                assert r.status_code == 403, r.text

    asyncio.run(_drive())


def test_unverified_email_not_adopted_as_account_email(
    test_db_url: str, monkeypatch
) -> None:
    _enable(monkeypatch)

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(
                jwk, make(_claims(email_verified=False))
            )
            pool = router._app.state.bp.db_pool
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post("/v1/auth/oidc/exchange", json=_exchange_body())
                assert r.status_code == 200, r.text
            async with pool.acquire() as conn:
                u = await queries.get_user_by_oidc_sub(
                    conn, issuer=_ISSUER, sub="sub-1"
                )
                assert u is not None and u.email is None  # not adopted

    asyncio.run(_drive())


def test_auto_link_by_verified_email(test_db_url: str, monkeypatch) -> None:
    _enable(monkeypatch, ROUTER_OIDC_AUTO_LINK_BY_VERIFIED_EMAIL="true")

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with pool.acquire() as conn:
                existing = await queries.insert_user(
                    conn, level="tier1", auth_kind="password",
                    auth_secret_hash="x", email="a@b.c",
                )
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(jwk, make(_claims()))
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post("/v1/auth/oidc/exchange", json=_exchange_body())
                assert r.status_code == 200, r.text
            # Linked onto the EXISTING account — no duplicate user.
            async with pool.acquire() as conn:
                u = await queries.get_user_by_oidc_sub(
                    conn, issuer=_ISSUER, sub="sub-1"
                )
                assert u is not None and u.user_id == existing.user_id

    asyncio.run(_drive())


def test_redirect_uri_must_be_allowlisted(test_db_url: str, monkeypatch) -> None:
    _enable(monkeypatch)

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(jwk, make(_claims()))
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post("/v1/auth/oidc/exchange", json={
                    "code": "x", "code_verifier": "v", "nonce": "N",
                    "redirect_uri": "https://evil.test/cb",
                })
                assert r.status_code == 400, r.text

    asyncio.run(_drive())


def test_bad_nonce_is_401(test_db_url: str, monkeypatch) -> None:
    _enable(monkeypatch)

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(jwk, make(_claims()))
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post(
                    "/v1/auth/oidc/exchange", json=_exchange_body(nonce="WRONG")
                )
                assert r.status_code == 401, r.text

    asyncio.run(_drive())


def test_authorize_returns_redirect_url(test_db_url: str, monkeypatch) -> None:
    _enable(monkeypatch)

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            jwk, make = _op()
            router._app.state.bp.oidc_provider = _fake_provider(jwk, make(_claims()))
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post(
                    "/v1/auth/oidc/authorize", json={"redirect_uri": _REDIRECT}
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["authorize_url"].startswith(f"{_ISSUER}/authorize")
                assert body["state"] and body["nonce"] and body["code_verifier"]
                assert "code_challenge=" in body["authorize_url"]

    asyncio.run(_drive())


def test_disabled_returns_404(test_db_url: str, monkeypatch) -> None:
    # OIDC not enabled → provider is None → 404 (not a 500).
    monkeypatch.delenv("ROUTER_OIDC_ENABLED", raising=False)

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            async with httpx.AsyncClient(
                base_url=router.public_url, timeout=10
            ) as c:
                r = await c.post(
                    "/v1/auth/oidc/authorize", json={"redirect_uri": _REDIRECT}
                )
                assert r.status_code == 404, r.text

    asyncio.run(_drive())
