"""Phase 1b — OIDC settings validation + the `OidcProvider` primitives.

Pure unit tests: the provider runs against an `httpx.MockTransport` fake OP,
and `id_token`s are really signed (RS256) with a throwaway key so signature /
claim validation is exercised end-to-end without network or a real IdP.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time

import pytest

# ===========================================================================
# Settings validation
# ===========================================================================


def _base_kwargs() -> dict:
    return dict(
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
        metrics_token="m" * 32,
    )


def _settings(monkeypatch, tmp_path, **overrides):
    pytest.importorskip("pydantic_settings")
    monkeypatch.chdir(tmp_path)  # ignore any repo .env
    from bp_router.settings import Settings

    return Settings(**{**_base_kwargs(), **overrides})  # type: ignore[arg-type]


def test_oidc_disabled_by_default(monkeypatch, tmp_path) -> None:
    cfg = _settings(monkeypatch, tmp_path)
    assert cfg.oidc_enabled is False


def test_oidc_enabled_complete_is_ok(monkeypatch, tmp_path) -> None:
    cfg = _settings(
        monkeypatch, tmp_path,
        oidc_enabled=True,
        oidc_issuer="https://op.example",
        oidc_client_id="cid",
        oidc_client_secret="sek",
        oidc_allowed_redirect_uris=["https://app.test/auth/sso/callback"],
        oidc_group_to_level={"admins": "admin", "staff": "tier0"},
    )
    assert cfg.oidc_enabled and cfg.oidc_client_id == "cid"


@pytest.mark.parametrize("drop", ["oidc_client_secret", "oidc_client_id", "oidc_issuer"])
def test_oidc_enabled_requires_essentials(monkeypatch, tmp_path, drop) -> None:
    from pydantic import ValidationError

    kwargs = dict(
        oidc_enabled=True,
        oidc_issuer="https://op.example",
        oidc_client_id="cid",
        oidc_client_secret="sek",
        oidc_allowed_redirect_uris=["https://app.test/cb"],
    )
    kwargs.pop(drop)
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, **kwargs)


def test_oidc_enabled_requires_redirect_allowlist(monkeypatch, tmp_path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(
            monkeypatch, tmp_path, oidc_enabled=True,
            oidc_issuer="https://op.example", oidc_client_id="c",
            oidc_client_secret="s", oidc_allowed_redirect_uris=[],
        )


def test_oidc_issuer_must_be_https(monkeypatch, tmp_path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(
            monkeypatch, tmp_path, oidc_enabled=True,
            oidc_issuer="http://op.example", oidc_client_id="c",
            oidc_client_secret="s",
            oidc_allowed_redirect_uris=["https://app.test/cb"],
        )


def test_oidc_bad_level_rejected_even_when_disabled(monkeypatch, tmp_path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, oidc_default_level="superuser")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, oidc_group_to_level={"g": "wheel"})


# ===========================================================================
# OidcProvider — against a signed-token fake OP
# ===========================================================================

_ISSUER = "https://op.example"
_CLIENT_ID = "test-client"


def _rsa_op():
    """Build a throwaway RSA key + its JWK (kid='k1'), and a token factory."""
    pytest.importorskip("cryptography")
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update(kid="k1", alg="RS256", use="sig")

    def make_token(claims: dict, *, key=priv, kid="k1", alg="RS256") -> str:
        return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})

    return jwk, make_token, priv


def _provider(jwk: dict, *, token_status: int = 200, token_body=None):
    import httpx

    from bp_router.security.oidc import OidcProvider

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": _ISSUER,
                "authorization_endpoint": f"{_ISSUER}/authorize",
                "token_endpoint": f"{_ISSUER}/token",
                "jwks_uri": f"{_ISSUER}/jwks",
                "end_session_endpoint": f"{_ISSUER}/logout",
            })
        if path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [jwk]})
        if path.endswith("/token"):
            return httpx.Response(token_status, json=token_body or {})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OidcProvider(
        issuer=_ISSUER, client_id=_CLIENT_ID, client_secret="shh",
        scopes="openid email profile", http=client,
    )


def _good_claims(**over) -> dict:
    now = int(time.time())
    return {
        "iss": _ISSUER, "aud": _CLIENT_ID, "sub": "subject-1",
        "iat": now, "exp": now + 300, "nonce": "N",
        "email": "a@b.c", "email_verified": True, "groups": ["admins"],
        **over,
    }


def test_pkce_challenge_is_s256_of_verifier() -> None:
    from bp_router.security.oidc import generate_pkce

    verifier, challenge = generate_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_carries_pkce_and_state() -> None:
    jwk, _mk, _ = _rsa_op()
    p = _provider(jwk)

    async def _drive():
        url = await p.authorize_url(
            redirect_uri="https://app.test/cb", state="S", nonce="N",
            code_challenge="CH",
        )
        return url

    url = asyncio.run(_drive())
    for frag in ("response_type=code", "client_id=test-client",
                 "code_challenge=CH", "code_challenge_method=S256",
                 "state=S", "nonce=N", "scope=openid"):
        assert frag in url


def test_exchange_and_validate_happy_path() -> None:
    jwk, make_token, _ = _rsa_op()

    async def _drive():
        token = make_token(_good_claims())
        p = _provider(jwk, token_body={"id_token": token, "access_token": "a"})
        body = await p.exchange_code(
            code="c", code_verifier="v", redirect_uri="https://app.test/cb"
        )
        claims = await p.validate_id_token(body["id_token"], nonce="N")
        assert claims["sub"] == "subject-1"
        assert claims["email"] == "a@b.c"

    asyncio.run(_drive())


def test_exchange_rejects_on_op_error() -> None:
    from bp_router.security.oidc import OidcError

    jwk, _mk, _ = _rsa_op()
    p = _provider(jwk, token_status=400, token_body={"error": "invalid_grant"})

    async def _drive():
        with pytest.raises(OidcError):
            await p.exchange_code(
                code="bad", code_verifier="v", redirect_uri="https://app.test/cb"
            )

    asyncio.run(_drive())


@pytest.mark.parametrize("claims_over,nonce", [
    ({"nonce": "WRONG"}, "N"),                 # nonce mismatch
    ({"aud": "someone-else"}, "N"),            # wrong audience
    ({"iss": "https://evil"}, "N"),            # wrong issuer
    ({"exp": int(time.time()) - 10}, "N"),     # expired
])
def test_validate_rejects_bad_claims(claims_over, nonce) -> None:
    from bp_router.security.oidc import OidcError

    jwk, make_token, _ = _rsa_op()
    p = _provider(jwk)

    async def _drive():
        token = make_token(_good_claims(**claims_over))
        with pytest.raises(OidcError):
            await p.validate_id_token(token, nonce=nonce)

    asyncio.run(_drive())


def test_validate_rejects_wrong_signing_key() -> None:
    """Token signed by a key NOT in the OP's JWKS (kid collides) → rejected."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    from bp_router.security.oidc import OidcError

    jwk, _mk, _ = _rsa_op()
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    import jwt as _jwt

    p = _provider(jwk)

    async def _drive():
        forged = _jwt.encode(
            _good_claims(), attacker, algorithm="RS256", headers={"kid": "k1"}
        )
        with pytest.raises(OidcError):
            await p.validate_id_token(forged, nonce="N")

    asyncio.run(_drive())


def test_validate_rejects_unsigned_alg_none() -> None:
    import jwt as _jwt

    from bp_router.security.oidc import OidcError

    jwk, _mk, _ = _rsa_op()
    p = _provider(jwk)

    async def _drive():
        unsigned = _jwt.encode(_good_claims(), key=None, algorithm="none")
        with pytest.raises(OidcError):
            await p.validate_id_token(unsigned, nonce="N")

    asyncio.run(_drive())


def test_discovery_issuer_mismatch_rejected() -> None:
    import httpx

    from bp_router.security.oidc import OidcError, OidcProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issuer": "https://impostor"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = OidcProvider(
        issuer=_ISSUER, client_id=_CLIENT_ID, client_secret="s",
        scopes="openid", http=client,
    )

    async def _drive():
        with pytest.raises(OidcError):
            await p.discovery()

    asyncio.run(_drive())
