"""bp_agents.bootstrap — register invitations + apply ACL (fake router)."""

from __future__ import annotations

import asyncio

import bp_agents.bootstrap as bs


class _Resp:
    def __init__(self, status: int = 201, payload=None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400 and self.status_code != 409:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *a, **k) -> None:
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/v1/auth/login"):
            return _Resp(200, {"access_token": "tok"})
        return _Resp(201, {})

    async def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        return _Resp(200, [{}] * 17)


def test_bootstrap_registers_all_and_applies_acl(monkeypatch) -> None:
    monkeypatch.setenv("ROUTER_URL", "http://router:8000")
    monkeypatch.setenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", "a@example.com")
    monkeypatch.setenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", "pw")
    for _name, var, _prov in bs._ROSTER:
        monkeypatch.setenv(var, "z" * 44)

    captured: dict = {}

    def _factory(*a, **k):
        captured["client"] = _FakeClient()
        return captured["client"]

    monkeypatch.setattr(bs.httpx, "AsyncClient", _factory)
    assert asyncio.run(bs._main()) == 0

    calls = captured["client"].calls
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][1].endswith("/v1/auth/login")
    invites = [c for c in posts if c[1].endswith("/v1/admin/invitations")]
    assert len(invites) == 11
    # The chatbot — and only the chatbot — provisions its service principal.
    provisioning = [c for c in invites if c[2]["json"].get("provisions_service_user")]
    assert len(provisioning) == 1
    assert all(c[2]["json"]["level"] == "tier1" for c in invites)
    # Each carries its pre-supplied token.
    assert all(c[2]["json"]["token"] == "z" * 44 for c in invites)
    # ACL applied once.
    puts = [c for c in calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][1].endswith("/v1/admin/acl/rules")


def test_bootstrap_missing_admin_creds_returns_2(monkeypatch) -> None:
    for v in ("ROUTER_BOOTSTRAP_ADMIN_EMAIL", "ROUTER_BOOTSTRAP_ADMIN_PASSWORD",
              "BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(bs, "_env", lambda _name: None)
    assert asyncio.run(bs._main()) == 2
