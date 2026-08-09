"""Agent host, merged init, env reference —
`docs/design/deployment-agent-host.md`.

The design's thesis is that the deployment unit should be an **isolation
boundary, not an agent**. These tests protect the two halves of that: the
grouping must never dissolve a real boundary, and the host must not make one
agent's crash everyone's problem.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from typing import Any

import pytest

from bp_agents.host import (
    GROUPS,
    NEVER_HOSTED,
    _Supervised,
    resolve_agents,
    run_host,
)

# ---------------------------------------------------------------------------
# Grouping — the isolation boundaries
# ---------------------------------------------------------------------------


def test_sandbox_and_bridge_are_never_hosted() -> None:
    """The sandbox runs as root with CAP_SETUID, no-new-privileges, a
    root-owned state dir and no DB network. Those capabilities and that
    network position ARE the isolation — co-hosting anything with it hands
    that agent the same. The bridge is the same argument in miniature."""
    assert NEVER_HOSTED == {"sandbox", "mcp_bridge"}
    for group, names in GROUPS.items():
        assert not NEVER_HOSTED.intersection(names), group


def test_naming_a_never_hosted_agent_fails_loudly() -> None:
    """A config typo that would co-host the untrusted sandbox must stop the
    process, not silently drop the name."""
    with pytest.raises(SystemExit) as exc:
        resolve_agents(None, "orchestrator,sandbox")
    assert "sandbox" in str(exc.value)
    assert "own container" in str(exc.value)


def test_groups_cover_the_hostable_roster() -> None:
    hosted = {name for names in GROUPS.values() for name in names}
    assert hosted == {
        "orchestrator",
        "deep_reasoning",
        "research",
        "computer_use",
        "knowledge_base",
        "memory",
        "history_summarizer",
        "md_converter",
        "config",
        "chatbot",
        "webapp",
    }
    # channels is its own group: the only agents on the `edge` network, and
    # the only ones whose restart a user notices.
    assert set(GROUPS["channels"]) == {"chatbot", "webapp"}


def test_group_and_agents_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        resolve_agents("suite-core", "orchestrator")
    with pytest.raises(SystemExit):
        resolve_agents(None, None)
    with pytest.raises(SystemExit):
        resolve_agents("no-such-group", None)


def test_every_group_member_is_importable() -> None:
    """A group naming an agent module that doesn't exist would fail at
    container start, in production, with the roster half-provisioned."""
    import importlib

    for names in GROUPS.values():
        for name in names:
            importlib.import_module(f"bp_agents.agents.{name}.agent")


def test_load_agent_returns_the_module_level_agent() -> None:
    from bp_agents.host import load_agent

    agent = load_agent("orchestrator")
    assert agent.info.agent_id == "orchestrator"


def test_hosted_agents_do_not_share_a_credentials_file(monkeypatch: Any) -> None:
    """Credentials live at `state_dir/credentials.json`. Agents sharing one
    process share `AGENT_STATE_DIR`, so without a per-agent subdirectory
    nine agents would overwrite each other's tokens and, on restart, load
    someone else's — every one of them 403ing."""
    from bp_agents.host import load_agent

    monkeypatch.setenv("SUITE_ROSTER_TOKEN", "roster")
    paths = {name: load_agent(name).config.state_dir for name in GROUPS["suite-core"]}
    assert len(set(paths.values())) == len(paths), paths
    for name, path in paths.items():
        assert path.name == name, (name, path)


def test_each_hosted_agent_gets_its_own_invitation(monkeypatch: Any) -> None:
    """`AGENT_INVITATION_TOKEN` is ONE process-wide variable. Sharing it
    would have the first agent to onboard consume it and the rest fail —
    and it would hand the chatbot's `provisions_service_user` credential to
    whoever onboarded first."""
    from bp_agents.host import agent_invitation

    monkeypatch.setenv("SUITE_ROSTER_TOKEN", "roster")
    monkeypatch.setenv("CHATBOT_INVITATION", "service-user-invite")
    # An agent-specific var wins: the service-user invitation must not be
    # replaced by the roster.
    assert agent_invitation("chatbot") == "service-user-invite"
    # Everyone else draws from the roster.
    assert agent_invitation("webapp") == "roster"
    assert agent_invitation("orchestrator") == "roster"


def test_host_agents_carry_the_token_the_host_resolved(monkeypatch: Any) -> None:
    from bp_agents.host import load_agent

    monkeypatch.setenv("SUITE_ROSTER_TOKEN", "roster")
    monkeypatch.setenv("CHATBOT_INVITATION", "service-user-invite")
    assert load_agent("chatbot").config.invitation_token == "service-user-invite"
    assert load_agent("webapp").config.invitation_token == "roster"


# ---------------------------------------------------------------------------
# Supervision — the cost of hosting
# ---------------------------------------------------------------------------


class _FakeAgent:
    """An agent whose `run_async` fails a set number of times."""

    def __init__(self, failures: int, exc: Exception | None = None) -> None:
        self.calls = 0
        self._failures = failures
        self._exc = exc or RuntimeError("boom")
        self.info = type("info", (), {"agent_id": "fake"})()

    async def run_async(self) -> None:
        self.calls += 1
        if self.calls <= self._failures:
            raise self._exc
        await asyncio.sleep(3600)  # "running"


def test_a_crashing_agent_is_restarted(monkeypatch: Any) -> None:
    """One agent's unhandled exception must not take its neighbours down.
    This is strictly better than the container-per-agent status quo, where
    the same fault drops every in-flight task in the container."""

    async def go() -> None:
        import bp_agents.host as host_mod

        monkeypatch.setattr(host_mod, "_BACKOFF_START_S", 0.01)
        monkeypatch.setattr(host_mod, "_BACKOFF_MAX_S", 0.02)
        agent = _FakeAgent(failures=3)
        sup = _Supervised("fake", agent)  # type: ignore[arg-type]
        stopping = asyncio.Event()
        task = asyncio.create_task(sup.run(stopping))
        await asyncio.sleep(0.3)
        stopping.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.calls > 3, "supervisor should have restarted past the failures"
        assert sup.restarts >= 3

    asyncio.run(go())


def test_permanent_transport_failure_stops_restarting(monkeypatch: Any) -> None:
    """A permanently-dead transport must not spin. The SDK raises this
    exactly so a supervisor can see the difference between "crashed" and
    "will never work"."""

    async def go() -> None:
        from bp_sdk.errors import TransportPermanentlyFailed

        agent = _FakeAgent(failures=99, exc=TransportPermanentlyFailed("gone"))
        sup = _Supervised("fake", agent)  # type: ignore[arg-type]
        await asyncio.wait_for(sup.run(asyncio.Event()), timeout=2)
        assert agent.calls == 1, "must not retry a permanently failed transport"
        assert sup.permanently_failed is True

    asyncio.run(go())


def test_host_exits_non_zero_when_every_agent_is_permanently_dead(
    monkeypatch: Any,
) -> None:
    """Otherwise a supervisor sees a healthy-looking container full of dead
    agents — the failure mode `Agent.run`'s SystemExit(1) exists to avoid."""

    async def go() -> int:
        import bp_agents.host as host_mod
        from bp_sdk.errors import TransportPermanentlyFailed

        monkeypatch.setattr(
            host_mod,
            "load_agent",
            lambda name: _FakeAgent(failures=99, exc=TransportPermanentlyFailed("x")),
        )
        return await run_host(["a", "b"])

    assert asyncio.run(go()) == 1


def test_host_exits_zero_on_a_clean_shutdown(monkeypatch: Any) -> None:
    async def go() -> int:
        import bp_agents.host as host_mod

        class _Clean:
            info = type("info", (), {"agent_id": "clean"})()

            async def run_async(self) -> None:
                return None

        monkeypatch.setattr(host_mod, "load_agent", lambda name: _Clean())
        return await asyncio.wait_for(run_host(["a"]), timeout=5)

    assert asyncio.run(go()) == 0


def test_host_forwards_shutdown_to_every_agent() -> None:
    """SIGTERM must reach all of them so each drains in-flight tasks inside
    the container's grace period, exactly as it does standalone."""
    src = inspect.getsource(run_host)
    assert "signal.SIGTERM" in src
    assert "stopping.set()" in src


# ---------------------------------------------------------------------------
# init — the merged one-shot
# ---------------------------------------------------------------------------


def test_init_runs_the_three_steps_in_order() -> None:
    from bp_agents.init import STEPS

    assert STEPS == ("router-schema", "suite-schema", "acl")


def test_init_stops_at_the_first_failure(monkeypatch: Any) -> None:
    """A half-migrated schema with a bootstrapped ACL is harder to reason
    about than a clean stop."""
    import bp_agents.init as init_mod

    calls: list[str] = []

    def _ok(name: str) -> Any:
        def run() -> int:
            calls.append(name)
            return 0

        return run

    def _fail(name: str) -> Any:
        def run() -> int:
            calls.append(name)
            return 3

        return run

    monkeypatch.setattr(
        init_mod,
        "_RUNNERS",
        {
            "router-schema": _ok("router-schema"),
            "suite-schema": _fail("suite-schema"),
            "acl": _ok("acl"),
        },
    )
    assert init_mod.main([]) == 3
    assert calls == ["router-schema", "suite-schema"], "must not run acl after a failure"


def test_init_can_run_one_step_for_debugging(monkeypatch: Any) -> None:
    """Collapsing three services must not cost debuggability."""
    import bp_agents.init as init_mod

    calls: list[str] = []
    monkeypatch.setattr(
        init_mod,
        "_RUNNERS",
        {name: (lambda n=name: (calls.append(n), 0)[1]) for name in init_mod.STEPS},
    )
    assert init_mod.main(["--step", "acl"]) == 0
    assert calls == ["acl"]


def test_init_holds_the_admin_credential_and_the_host_does_not() -> None:
    """The split is the point (design §3): an admin-authenticated one-shot
    mints the roster and applies the ACL; the host holds only a credential
    that can produce the agents it was given."""
    import bp_agents.host as host_mod
    import bp_agents.init as init_mod

    assert "bootstrap" in inspect.getsource(init_mod)
    host_src = inspect.getsource(host_mod)
    for admin_marker in ("BOOTSTRAP_ADMIN", "ADMIN_PASSWORD", "/v1/admin"):
        assert admin_marker not in host_src, admin_marker


# ---------------------------------------------------------------------------
# Env reference
# ---------------------------------------------------------------------------


def test_env_reference_is_current() -> None:
    """Generated, so it cannot drift — which is the property `.env.example`
    was reaching for and does not have (it documents 24 of 104 router
    settings while the README calls it complete)."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "scripts/gen_env_reference.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_env_reference_covers_every_router_setting() -> None:
    from pathlib import Path

    from bp_router.settings import Settings

    doc = Path("docs/env-reference.md").read_text()
    missing = [
        name for name in Settings.model_fields if f"ROUTER_{name.upper()}" not in doc
    ]
    assert not missing, missing


def test_env_reference_documents_the_dict_shaped_quotas() -> None:
    """The ones an operator could previously only find by reading
    settings.py."""
    from pathlib import Path

    doc = Path("docs/env-reference.md").read_text()
    for name in (
        "ROUTER_FILE_STORAGE_QUOTA_BYTES",
        "ROUTER_SESSION_STORE_QUOTA_BYTES",
        "ROUTER_LLM_DEFAULT_PRESETS",
    ):
        assert name in doc, name


# ---------------------------------------------------------------------------
# Compose — the shape the design produces
# ---------------------------------------------------------------------------


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    from pathlib import Path

    return yaml.safe_load(Path("docker-compose.prod.yml").read_text())


def test_compose_collapsed_to_group_services() -> None:
    svc = set(_compose()["services"])
    # The twelve per-agent services are gone...
    for agent in ("orchestrator", "research", "memory", "webapp", "chatbot"):
        assert agent not in svc, agent
    # ...replaced by groups, with the isolation boundaries still standalone.
    assert {"suite-core", "channels", "sandbox", "init"} <= svc
    assert len(svc) == 11, sorted(svc)


def test_compose_keeps_the_sandbox_hardening() -> None:
    """The capabilities and network position ARE the isolation. If this ever
    relaxes, the sandbox stops being one."""
    sandbox = _compose()["services"]["sandbox"]
    assert sandbox["user"] == "0:0"
    assert sandbox["cap_drop"] == ["ALL"]
    assert set(sandbox["cap_add"]) == {"SETUID", "SETGID", "CHOWN"}
    assert "no-new-privileges:true" in sandbox["security_opt"]
    # No database, no Valkey, no web — the router WS and nothing else.
    assert sandbox["networks"] == ["agents"]


def test_compose_needs_two_credential_vars_not_twelve() -> None:
    import re
    from pathlib import Path

    text = Path("docker-compose.prod.yml").read_text()
    creds = set(re.findall(r"\$\{([A-Z_]*(?:INVITATION|ROSTER_TOKEN))", text))
    assert creds == {"SUITE_ROSTER_TOKEN", "CHATBOT_INVITATION"}, sorted(creds)


def test_compose_every_depends_on_target_exists() -> None:
    compose = _compose()
    names = set(compose["services"])
    dangling = [
        (n, t)
        for n, s in compose["services"].items()
        for t in (s.get("depends_on") or {})
        if t not in names
    ]
    assert not dangling, dangling


def test_compose_groups_run_the_host() -> None:
    svc = _compose()["services"]
    assert svc["suite-core"]["command"][:3] == ["python", "-m", "bp_agents.host"]
    assert svc["channels"]["command"][:3] == ["python", "-m", "bp_agents.host"]
    assert svc["init"]["command"] == ["python", "-m", "bp_agents.init"]


def test_compose_group_membership_matches_the_host() -> None:
    """A group named in compose that the host does not know would start a
    container that immediately exits."""
    svc = _compose()["services"]
    for name in ("suite-core", "channels"):
        group = svc[name]["command"][-1]
        assert group in GROUPS, group
