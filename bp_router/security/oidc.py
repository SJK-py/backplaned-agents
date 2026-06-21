"""bp_router.security.oidc — OpenID Connect Relying-Party primitives.

The router is the OIDC RP/identity authority (see
`docs/design/oidc-webapp.md`): discovery, the authorization-code redirect
URL, code→token exchange (with the confidential-client secret), and
`id_token` validation all live here. User provisioning + first-party
`TokenPair` issuance happen in the endpoint layer (`api/auth.py`); the
browser redirects + transient `state`/`nonce`/PKCE live in the frontend
BFF.

Standards: Authorization Code flow + **PKCE (S256)**, `state` (CSRF) and
`nonce` (id_token replay), `id_token` signature verified against the OP's
JWKS with `iss` / `aud` / `exp` / `iat` / `nonce` checks. Discovery-driven
so any compliant OP (Authelia, Keycloak, Google, Microsoft) works as config.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

# id_token signature algorithms we accept. Asymmetric only — never `none`,
# never an HMAC alg (which would let anyone holding the *public* JWKS forge
# a token). RS256 is near-universal; the others cover OPs configured for
# stronger curves/hashes.
_ALLOWED_ALGS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
)


class OidcError(Exception):
    """Any failure in the OIDC exchange/validation. The endpoint maps it to
    a 4xx (bad/relevant token) or 502 (OP unreachable / malformed)."""


# ---------------------------------------------------------------------------
# Per-request transient values (held by the frontend BFF between redirects)
# ---------------------------------------------------------------------------


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


def generate_pkce() -> tuple[str, str]:
    """Return `(code_verifier, code_challenge)` for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass
class _Cached:
    value: dict[str, Any]
    fetched_at: float


class OidcProvider:
    """Stateless-ish OIDC client for one OP. Caches the discovery document
    and JWKS for `cache_ttl_s`. Construct once (app lifespan) and share —
    `httpx.AsyncClient` is reused. Injectable `http` keeps it unit-testable
    against an `httpx.MockTransport`."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        scopes: str,
        http: httpx.AsyncClient,
        cache_ttl_s: int = 3600,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._http = http
        self._cache_ttl_s = cache_ttl_s
        self._discovery: _Cached | None = None
        self._jwks: _Cached | None = None

    # -- discovery / jwks ----------------------------------------------

    def _fresh(self, c: _Cached | None) -> bool:
        return c is not None and (time.monotonic() - c.fetched_at) < self._cache_ttl_s

    async def discovery(self) -> dict[str, Any]:
        if self._fresh(self._discovery):
            return self._discovery.value  # type: ignore[union-attr]
        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            doc = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"OIDC discovery failed: {exc}") from exc
        # Guard the spoof where `iss` in the document disagrees with where we
        # fetched it from (OIDC discovery §spec requires they match).
        if doc.get("issuer", "").rstrip("/") != self._issuer:
            raise OidcError("OIDC discovery issuer mismatch")
        self._discovery = _Cached(doc, time.monotonic())
        return doc

    async def jwks(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._fresh(self._jwks):
            return self._jwks.value  # type: ignore[union-attr]
        disco = await self.discovery()
        uri = disco.get("jwks_uri")
        if not uri:
            raise OidcError("OIDC discovery has no jwks_uri")
        try:
            resp = await self._http.get(uri)
            resp.raise_for_status()
            keys = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"OIDC JWKS fetch failed: {exc}") from exc
        self._jwks = _Cached(keys, time.monotonic())
        return keys

    # -- redirect / exchange / validate --------------------------------

    async def authorize_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        disco = await self.discovery()
        endpoint = disco.get("authorization_endpoint")
        if not endpoint:
            raise OidcError("OIDC discovery has no authorization_endpoint")
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": self._scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, Any]:
        disco = await self.discovery()
        endpoint = disco.get("token_endpoint")
        if not endpoint:
            raise OidcError("OIDC discovery has no token_endpoint")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,  # client_secret_post
            "code_verifier": code_verifier,
        }
        try:
            resp = await self._http.post(endpoint, data=data)
        except httpx.HTTPError as exc:
            raise OidcError(f"OIDC token endpoint unreachable: {exc}") from exc
        if resp.status_code >= 400:
            # The OP echoes `error`/`error_description` on a bad/expired code.
            raise OidcError(f"OIDC token exchange rejected ({resp.status_code})")
        try:
            body = resp.json()
        except ValueError as exc:
            raise OidcError("OIDC token response was not JSON") from exc
        if "id_token" not in body:
            raise OidcError("OIDC token response missing id_token")
        return body

    async def validate_id_token(
        self, id_token: str, *, nonce: str
    ) -> dict[str, Any]:
        """Verify the `id_token` signature + standard claims and return them.
        Raises `OidcError` on any failure."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError(f"malformed id_token header: {exc}") from exc
        alg = header.get("alg")
        if alg not in _ALLOWED_ALGS:
            raise OidcError(f"unsupported id_token alg {alg!r}")
        kid = header.get("kid")

        key = await self._signing_key(kid, alg)
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=[alg],
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub"], "verify_aud": True},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"id_token validation failed: {exc}") from exc
        # nonce binds the id_token to THIS login attempt (replay defence) —
        # PyJWT doesn't check it.
        if not nonce or claims.get("nonce") != nonce:
            raise OidcError("id_token nonce mismatch")
        return claims

    async def _signing_key(self, kid: str | None, alg: str) -> Any:
        # Try the cached JWKS; on a kid miss, refetch once (key rotation).
        for force in (False, True):
            jwks = await self.jwks(force=force)
            jwk = _select_jwk(jwks.get("keys", []), kid)
            if jwk is not None:
                return _key_from_jwk(jwk, alg)
            if not force:
                continue
        raise OidcError(f"no JWKS key matches id_token kid {kid!r}")

    async def end_session_url(
        self, *, id_token_hint: str | None = None,
        post_logout_redirect_uri: str | None = None,
    ) -> str | None:
        """RP-initiated logout URL, or None if the OP doesn't advertise one."""
        disco = await self.discovery()
        endpoint = disco.get("end_session_endpoint")
        if not endpoint:
            return None
        params: dict[str, str] = {}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = post_logout_redirect_uri
        if not params:
            return endpoint
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}{urlencode(params)}"


def _select_jwk(keys: list[dict[str, Any]], kid: str | None) -> dict[str, Any] | None:
    if not keys:
        return None
    if kid is not None:
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None
    # No kid in the header — only unambiguous when the OP publishes one key.
    return keys[0] if len(keys) == 1 else None


def _key_from_jwk(jwk: dict[str, Any], alg: str) -> Any:
    raw = json.dumps(jwk)
    if alg.startswith("RS"):
        return RSAAlgorithm.from_jwk(raw)
    if alg.startswith("ES"):
        return ECAlgorithm.from_jwk(raw)
    raise OidcError(f"unsupported id_token alg {alg!r}")
