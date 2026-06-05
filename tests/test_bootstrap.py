"""bp_agents.bootstrap — register invitations + apply ACL (fake router)."""

from __future__ import annotations

import asyncio
import pathlib

import bp_agents.bootstrap as bs


def test_compose_x_anchor_is_not_a_service() -> None:
    """The `x-suite-agent` block is a top-level YAML extension field that only
    EXISTS to hold the `&suite-agent` / `&suite-env` anchors. If it drifts
    UNDER `services:` (a one-line indentation slip), Compose instantiates it as
    a real container running the image default `SUITE_AGENT=orchestrator` with
    NO invitation token → it crash-loops on onboard ("no auth_token and no
    invitation_token"). Pin it out of the service set."""
    import yaml  # noqa: PLC0415

    repo = pathlib.Path(__file__).resolve().parent.parent
    d = yaml.safe_load((repo / "docker-compose.prod.yml").read_text())
    assert "x-suite-agent" not in d["services"], (
        "x-suite-agent is under services: — Compose will launch it as a "
        "container. Move it back to a TOP-LEVEL x- key (anchors still resolve)."
    )
    # Sanity: it must still exist somewhere as the anchor source.
    assert "x-suite-agent" in d, "x-suite-agent anchor block disappeared"


def test_compose_every_suite_agent_sets_name_and_token() -> None:
    """Every suite agent service must set BOTH `SUITE_AGENT: <its-name>` and an
    `AGENT_INVITATION_TOKEN` in its `environment:`. The shared `*suite-env`
    anchor carries NEITHER (it can't — they're per-agent), so a service that
    forgets its own `environment:` override (as `sandbox` once did) inherits
    only the anchor, falls back to the image default SUITE_AGENT=orchestrator
    with an empty token, and crash-loops on onboard."""
    import yaml  # noqa: PLC0415

    repo = pathlib.Path(__file__).resolve().parent.parent
    d = yaml.safe_load((repo / "docker-compose.prod.yml").read_text())
    svcs = d["services"]
    roster_names = {name for name, _var, _prov in bs._ROSTER}
    for name in roster_names:
        env = svcs[name].get("environment", {})
        assert isinstance(env, dict), f"{name}: no environment: block"
        assert env.get("SUITE_AGENT") == name, (
            f"{name}: SUITE_AGENT is {env.get('SUITE_AGENT')!r}, expected "
            f"{name!r} — without it the container runs the image default "
            "(orchestrator)."
        )
        assert "AGENT_INVITATION_TOKEN" in env, (
            f"{name}: no AGENT_INVITATION_TOKEN — it will onboard with an "
            "empty token and crash-loop."
        )


def test_bootstrap_compose_env_covers_full_roster() -> None:
    """The `bootstrap` service in docker-compose.prod.yml must pass EVERY
    agent's `*_INVITATION` env var. It's a hand-maintained copy of the roster,
    and `WEBAPP_INVITATION` once drifted out → bootstrap logged
    `skip webapp: WEBAPP_INVITATION unset`, registered only 11, and the webapp
    onboarded with an unregistered token → 403. Cross-check the compose env
    against `bp_agents.bootstrap._ROSTER` so a future add/rename can't silently
    drop one again."""
    import yaml  # noqa: PLC0415

    repo = pathlib.Path(__file__).resolve().parent.parent
    d = yaml.safe_load((repo / "docker-compose.prod.yml").read_text())
    boot_env = set(d["services"]["bootstrap"]["environment"])
    roster_vars = {var for _name, var, _prov in bs._ROSTER}
    missing = roster_vars - boot_env
    assert not missing, (
        f"bootstrap service env is missing roster invitation var(s): {missing}. "
        "Add them to the bootstrap `environment:` block or bootstrap will skip "
        "those agents and they'll 403 on onboard."
    )


class _Resp:
    def __init__(self, status: int = 201, payload=None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400 and self.status_code != 409:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *a, existing_rules=None, **k) -> None:
        self.calls: list[tuple] = []
        # Rules the fake router already has (for the ACL merge GET).
        self.existing_rules = existing_rules if existing_rules is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/v1/auth/login"):
            return _Resp(200, {"access_token": "tok"})
        return _Resp(201, {})

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if url.endswith("/v1/admin/acl/rules"):
            return _Resp(200, self.existing_rules)
        return _Resp(200, {})

    async def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        # Echo back the applied rule list so callers can count it.
        rules = kw.get("json", {}).get("rules", []) if url.endswith(
            "/v1/admin/acl/rules"
        ) else []
        return _Resp(200, rules)


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
    assert len(invites) == 12
    # The chatbot — and only the chatbot — provisions its service principal.
    provisioning = [c for c in invites if c[2]["json"].get("provisions_service_user")]
    assert len(provisioning) == 1
    assert all(c[2]["json"]["level"] == "tier1" for c in invites)
    # Each carries its pre-supplied token.
    assert all(c[2]["json"]["token"] == "z" * 44 for c in invites)
    # Short TTL (10 min default): tokens are minted fresh per launch and
    # consumed within seconds, so a long-lived invitation is needless risk.
    assert bs._INVITATION_TTL_S == 600
    assert all(c[2]["json"]["expires_in_s"] == bs._INVITATION_TTL_S for c in invites)
    # ACL applied once, and read first (the merge GET) so it's non-destructive.
    puts = [c for c in calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][1].endswith("/v1/admin/acl/rules")
    gets = [c for c in calls if c[0] == "GET" and c[1].endswith("/v1/admin/acl/rules")]
    assert len(gets) == 1


def test_bootstrap_acl_preserves_admin_rules(monkeypatch) -> None:
    """Prod path regression: the `bootstrap` service re-applies the suite ACL
    on every boot. It must MERGE — an admin-added rule (e.g. an MCP grant at
    ord 25) survives instead of being wiped by the suite reset (ord 0..N)."""
    from bp_agents.acl import suite_rule_names

    monkeypatch.setenv("ROUTER_URL", "http://router:8000")
    monkeypatch.setenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", "a@example.com")
    monkeypatch.setenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", "pw")
    for _name, var, _prov in bs._ROSTER:
        monkeypatch.setenv(var, "z" * 44)

    admin_rule = {
        "rule_id": "rule_admin", "ord": 25, "name": "channel→mcp_minimax",
        "description": "admin-added MCP grant",
        "effect": "allow", "user_level": "*",
        "caller_pattern": "channel/*", "callee_pattern": "@mcp_minimax",
    }
    captured: dict = {}

    def _factory(*a, **k):
        captured["client"] = _FakeClient(existing_rules=[admin_rule])
        return captured["client"]

    monkeypatch.setattr(bs.httpx, "AsyncClient", _factory)
    assert asyncio.run(bs._main()) == 0

    put = next(c for c in captured["client"].calls if c[0] == "PUT")
    applied = put[2]["json"]["rules"]
    names = [r["name"] for r in applied]
    assert "channel→mcp_minimax" in names               # preserved
    assert suite_rule_names() <= set(names)              # suite still applied
    # Admin rule kept HIGHER priority (lower ord) than the suite allows.
    admin_ord = next(r["ord"] for r in applied if r["name"] == "channel→mcp_minimax")
    suite_ords = [r["ord"] for r in applied if r["name"] in suite_rule_names()]
    assert admin_ord < min(suite_ords)


def test_bootstrap_sends_no_idempotency_key(monkeypatch) -> None:
    """Regression: a per-name `Idempotency-Key` (`register-<name>`) made the
    router return the EXISTING invitation row for a relaunch's FRESH token,
    silently ignoring it → the agent then presented an unregistered token →
    403. The token itself is the dedup key (token-hash PK → 409 on a true
    repeat), so NO Idempotency-Key header must be sent on invitation registers."""
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

    invites = [
        c for c in captured["client"].calls
        if c[0] == "POST" and c[1].endswith("/v1/admin/invitations")
    ]
    assert len(invites) == 12
    for c in invites:
        headers = c[2].get("headers", {})
        assert "Idempotency-Key" not in headers, (
            "invitation register must NOT carry an Idempotency-Key — it pins "
            "the row to the first token and drops fresh ones"
        )


def test_bootstrap_surfaces_register_failure(monkeypatch) -> None:
    """A 403/4xx on register (other than the 409 'already registered') must
    NOT be masked — bootstrap must fail so the operator sees it."""
    monkeypatch.setenv("ROUTER_URL", "http://router:8000")
    monkeypatch.setenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", "a@example.com")
    monkeypatch.setenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", "pw")
    for _name, var, _prov in bs._ROSTER:
        monkeypatch.setenv(var, "z" * 44)

    class _Failing(_FakeClient):
        async def post(self, url, **kw):
            self.calls.append(("POST", url, kw))
            if url.endswith("/v1/auth/login"):
                return _Resp(200, {"access_token": "tok"})
            if url.endswith("/v1/admin/invitations"):
                return _Resp(403, {"detail": "invalid or used invitation token"})
            return _Resp(201, {})

    captured: dict = {}

    def _factory(*a, **k):
        captured["client"] = _Failing()
        return captured["client"]

    monkeypatch.setattr(bs.httpx, "AsyncClient", _factory)
    # raise_for_status() raises on 403 → _main propagates (non-zero / raise).
    import pytest

    with pytest.raises(RuntimeError):
        asyncio.run(bs._main())


def test_bootstrap_missing_admin_creds_returns_2(monkeypatch) -> None:
    for v in ("ROUTER_BOOTSTRAP_ADMIN_EMAIL", "ROUTER_BOOTSTRAP_ADMIN_PASSWORD",
              "BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(bs, "_env", lambda _name: None)
    assert asyncio.run(bs._main()) == 2
