"""Phase 2 — webapp SSO browser flow.

The webapp drives the OIDC browser redirects and holds the transient
state/nonce/PKCE verifier in its signed session cookie; the router (faked
here) does the crypto. Covers the SSO button, the login redirect, the
state-checked callback, and the failure paths.
"""

from __future__ import annotations

import base64
import json

import pytest


def _access_jwt(sub: str) -> str:
    """Minimal JWT-shaped token whose payload carries `sub` (store_login
    reads the sub to scope the session; it does not verify the signature)."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub}).encode()
    ).rstrip(b"=").decode()
    return f"h.{payload}.s"


class _Upstream:
    def __init__(self) -> None:
        self.authorize_calls: list[str] = []
        self.exchanges: list[dict] = []
        self.exchange_error: Exception | None = None

    async def oidc_authorize(self, *, redirect_uri: str) -> dict:
        self.authorize_calls.append(redirect_uri)
        return {
            "authorize_url": "https://op.example/authorize?client_id=x",
            "state": "STATE-1", "nonce": "NONCE-1", "code_verifier": "VERIFIER-1",
        }

    async def oidc_exchange(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str
    ) -> dict:
        self.exchanges.append({
            "code": code, "code_verifier": code_verifier,
            "nonce": nonce, "redirect_uri": redirect_uri,
        })
        if self.exchange_error is not None:
            raise self.exchange_error
        return {
            "access_token": _access_jwt("usr_sso"), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def aclose(self) -> None:
        pass


def _build_app(upstream: object, *, sso_enabled: bool = True):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr

    from bp_agents.agents.webapp.app import create_app
    from bp_agents.agents.webapp.config import WebappConfig

    cfg = WebappConfig(
        session_secret=SecretStr("x" * 32), session_cookie_secure=False,
        sso_enabled=sso_enabled,
        public_base_url="https://app.test" if sso_enabled else None,
    )
    return create_app(cfg, upstream=upstream, pool=None, core=None)


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


# --- config validation -----------------------------------------------------


def test_sso_enabled_requires_public_base_url() -> None:
    pytest.importorskip("fastapi")
    from pydantic import SecretStr, ValidationError

    from bp_agents.agents.webapp.config import WebappConfig

    with pytest.raises(ValidationError):
        WebappConfig(
            session_secret=SecretStr("x" * 32), sso_enabled=True,
            public_base_url=None,
        )


# --- button visibility -----------------------------------------------------


def test_login_shows_sso_button_when_enabled() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream(), sso_enabled=True)) as c:
        html = c.get("/login").text
    assert "Sign in with SSO" in html
    assert 'href="/auth/sso/login"' in html


def test_login_hides_sso_button_when_disabled() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream(), sso_enabled=False)) as c:
        html = c.get("/login").text
    assert "Sign in with SSO" not in html


# --- flow ------------------------------------------------------------------


def test_sso_login_redirects_to_op_with_callback_uri() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.get("/auth/sso/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://op.example/authorize?client_id=x"
    # The router was handed the webapp's own absolute callback URL.
    assert up.authorize_calls == ["https://app.test/auth/sso/callback"]


def test_sso_full_flow_authenticates() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        # 1) start → stores flow in the session cookie
        c.get("/auth/sso/login", follow_redirects=False)
        # 2) OP bounces back with the matching state
        r = c.get(
            "/auth/sso/callback",
            params={"code": "CODE", "state": "STATE-1"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # The router exchange got our stashed verifier + nonce.
    assert up.exchanges == [{
        "code": "CODE", "code_verifier": "VERIFIER-1", "nonce": "NONCE-1",
        "redirect_uri": "https://app.test/auth/sso/callback",
    }]


def test_sso_callback_state_mismatch_is_rejected() -> None:
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        c.get("/auth/sso/login", follow_redirects=False)
        r = c.get(
            "/auth/sso/callback",
            params={"code": "CODE", "state": "WRONG"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=sso"
    assert up.exchanges == []  # never exchanged a forged state


def test_sso_callback_without_flow_is_rejected() -> None:
    """A callback with no prior /auth/sso/login (no cookie flow) is refused."""
    pytest.importorskip("fastapi")
    up = _Upstream()
    with _client(_build_app(up)) as c:
        r = c.get(
            "/auth/sso/callback",
            params={"code": "CODE", "state": "STATE-1"},
            follow_redirects=False,
        )
    assert r.headers["location"] == "/login?error=sso"
    assert up.exchanges == []


def test_sso_exchange_failure_is_friendly() -> None:
    pytest.importorskip("fastapi")
    from bp_agents.agents.webapp.upstream import UpstreamError

    up = _Upstream()
    up.exchange_error = UpstreamError(401, "OIDC authentication failed")
    with _client(_build_app(up)) as c:
        c.get("/auth/sso/login", follow_redirects=False)
        r = c.get(
            "/auth/sso/callback",
            params={"code": "CODE", "state": "STATE-1"},
            follow_redirects=False,
        )
    assert r.headers["location"] == "/login?error=sso"


def test_sso_routes_noop_when_disabled() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream(), sso_enabled=False)) as c:
        r = c.get("/auth/sso/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_sso_error_message() -> None:
    pytest.importorskip("fastapi")
    with _client(_build_app(_Upstream())) as c:
        html = c.get("/login?error=sso").text
    assert "Single sign-on failed" in html
