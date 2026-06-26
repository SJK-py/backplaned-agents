"""Second-pass ops/deploy hardening (PR C).

B1 — graceful shutdown: the router gets a bounded uvicorn
      `timeout_graceful_shutdown` (settings.shutdown_grace_s) and the prod
      compose gives router + suite agents a `stop_grace_period` that exceeds
      it, so the lifespan/SDK drain isn't SIGKILLed mid-flight on every
      restart.
B2 — the `bp_suite` Postgres role is created NOLOGIN (no committed
      'change-me-suite' login password — a dormant known-credential account).
M2 — the third-party `seaweedfs` (file store) and `searxng` images are pinned
      to immutable tags, not `:latest`.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_COMPOSE = (_REPO / "docker-compose.prod.yml").read_text()
_INIT_SQL = (_REPO / "deploy/postgres-init/01-create-suite-db.sql").read_text()


# --- B1: uvicorn graceful timeout -----------------------------------------


def test_shutdown_grace_setting_default_and_bound() -> None:
    from bp_router.settings import Settings

    field = Settings.model_fields["shutdown_grace_s"]
    assert field.default == 25.0
    # Negative grace is rejected (ge=0 constraint).
    meta = repr(field.metadata)
    assert "Ge(ge=0" in meta or "ge=0" in meta


def test_main_passes_graceful_shutdown_timeout() -> None:
    from bp_router import __main__ as entry

    src = inspect.getsource(entry.main)
    assert "timeout_graceful_shutdown" in src
    assert "settings.shutdown_grace_s" in src


# --- B1: compose stop_grace_period exceeds the uvicorn timeout -------------


def test_compose_router_and_agents_have_stop_grace_period() -> None:
    # Both the router service and the x-suite-agent anchor (inherited by the
    # webapp + every suite agent) declare a stop_grace_period.
    assert _COMPOSE.count("stop_grace_period: 30s") >= 2
    # And it exceeds the router's uvicorn graceful timeout (25s) so the drain
    # completes before SIGKILL.
    from bp_router.settings import Settings

    assert Settings.model_fields["shutdown_grace_s"].default < 30.0


# --- B2: suite role is NOLOGIN, no committed password ----------------------


def test_suite_role_is_nologin_without_committed_password() -> None:
    assert "CREATE ROLE bp_suite NOLOGIN;" in _INIT_SQL
    assert "change-me-suite" not in _INIT_SQL
    assert "LOGIN PASSWORD" not in _INIT_SQL
    # The database + ownership are still created for the future wiring.
    assert "CREATE DATABASE bp_suite OWNER bp_suite;" in _INIT_SQL


# --- M2: third-party images pinned, not :latest ---------------------------


@pytest.mark.parametrize("image_prefix", ["chrislusf/seaweedfs:", "searxng/searxng:"])
def test_third_party_images_are_pinned(image_prefix: str) -> None:
    line = next(
        (ln.strip() for ln in _COMPOSE.splitlines()
         if image_prefix in ln and ln.strip().startswith("image:")),
        None,
    )
    assert line is not None, f"{image_prefix} image line not found"
    tag = line.split(image_prefix, 1)[1].strip()
    assert tag and tag != "latest", f"{image_prefix} must pin a real tag, got {tag!r}"


# --- sandbox per-user-uid posture: root + minimal caps + no-new-privs ------


def test_sandbox_runs_root_with_minimal_caps_for_uid_drop() -> None:
    """The sandbox must run as root with EXACTLY SETUID/SETGID/CHOWN added AND
    keep no-new-privileges (so untrusted code can't regain privilege).
    SETUID+SETGID drop each user's bash to its own uid; CHOWN hands that uid
    ownership of its workspace (without it the chown EPERMs, the workspace
    stays root-owned, and — cap_drop ALL having removed DAC_OVERRIDE — the
    dropped uid can't write it, so every bash command fails). Dropping all caps
    without these, or not running as root, collapses every user onto one uid —
    no isolation. More caps than these three is over-privileged."""
    import yaml  # noqa: PLC0415

    d = yaml.safe_load(_COMPOSE)
    sb = d["services"]["sandbox"]
    assert str(sb.get("user")) in ("0:0", "0", "root"), (
        "sandbox must run as root or the uid drop silently no-ops"
    )
    assert "no-new-privileges:true" in sb.get("security_opt", []), (
        "no-new-privileges must stay on — it blocks privilege REGAIN, not the drop"
    )
    assert sb.get("cap_drop") == ["ALL"]
    assert set(sb.get("cap_add", [])) == {"SETUID", "SETGID", "CHOWN"}, (
        "exactly SETUID+SETGID (uid drop) + CHOWN (workspace ownership) — "
        "more is over-privileged, fewer breaks the drop or the workspace write"
    )


def test_sandbox_state_dir_is_root_owned_not_the_10002_state() -> None:
    """Regression: the sandbox runs as root with cap_drop: ALL → no
    CAP_DAC_OVERRIDE, so it can ONLY write a dir it OWNS. The image's default
    /state is chown'd to uid 10002 (for the other, non-root agents), so the
    sandbox got `PermissionError: '/state/credentials.json.tmp'` → crash →
    re-onboard a consumed token → router 403. It must instead use the
    root-owned /sandbox-state (created in Dockerfile.suite) for BOTH its
    AGENT_STATE_DIR and its volume mount. Pin all three (env, mount,
    Dockerfile) so the EACCES can't silently return."""
    import yaml  # noqa: PLC0415

    d = yaml.safe_load(_COMPOSE)
    sb = d["services"]["sandbox"]
    assert sb["environment"]["AGENT_STATE_DIR"] == "/sandbox-state", (
        "sandbox must NOT use the uid-10002-owned /state — root with "
        "cap_drop: ALL can't write it (no CAP_DAC_OVERRIDE)"
    )
    # The persisted volume must mount where AGENT_STATE_DIR points, else the
    # credential isn't on a volume and is lost on every recreate.
    mounts = [v.split(":")[-1] for v in sb["volumes"]]
    assert "/sandbox-state" in mounts, (
        f"sandbox volume must mount at /sandbox-state, got {sb['volumes']}"
    )
    # Dockerfile must create that dir root-owned 0700 (root owns it by default;
    # 0700 also hides the router token from the per-user uid the bash drops to).
    dockerfile = (_REPO / "Dockerfile.suite").read_text()
    assert "/sandbox-state" in dockerfile and "chmod 700 /sandbox-state" in dockerfile, (
        "Dockerfile.suite must `mkdir -p /sandbox-state && chmod 700 "
        "/sandbox-state` so the root sandbox owns a writable state dir"
    )


# --- Operator custom env (env_file), scoped to the secret-ref resolvers -----


def _custom_env_file(service: dict) -> bool:
    """True if `service` loads deploy/.env.prod.custom as an OPTIONAL env_file."""
    for entry in service.get("env_file", []) or []:
        if isinstance(entry, dict) and entry.get("path", "").endswith(
            "deploy/.env.prod.custom"
        ):
            # Must be optional so a deploy without the file still boots.
            assert entry.get("required") is False, (
                "the custom env_file must be required: false (skipped if absent)"
            )
            return True
    return False


def test_custom_env_file_wired_into_router_and_mcp_bridge_only() -> None:
    """Operators inject custom env (e.g. a preset api_key_ref `env://…`) via
    deploy/.env.prod.custom WITHOUT editing the compose file. It must reach the
    only two services that resolve secret refs — router + mcp_bridge — and NOT
    the suite agents or the UNTRUSTED sandbox (least privilege: don't hand
    operator secrets to containers that never use them)."""
    import yaml  # noqa: PLC0415

    d = yaml.safe_load(_COMPOSE)
    assert _custom_env_file(d["services"]["router"]), (
        "router must load deploy/.env.prod.custom (it resolves api_key_ref / "
        "OIDC secret refs)"
    )
    assert _custom_env_file(d["services"]["mcp_bridge"]), (
        "mcp_bridge must load deploy/.env.prod.custom (it resolves MCP "
        "auth_value_ref from its own env)"
    )
    # Must NOT leak into the untrusted sandbox or the channel/worker agents.
    for svc in ("sandbox", "chatbot", "webapp", "orchestrator"):
        assert not _custom_env_file(d["services"][svc]), (
            f"{svc} must NOT load the operator custom env file (it never "
            "resolves secret refs; least-privilege)"
        )


def test_custom_env_example_committed_and_real_file_gitignored() -> None:
    assert (_REPO / "deploy/.env.prod.custom.example").is_file()
    gitignore = (_REPO / ".gitignore").read_text()
    assert "deploy/.env.prod.custom" in gitignore
    # prod.sh seeds the real file from the example so it's discoverable.
    prod_sh = (_REPO / "scripts/prod.sh").read_text()
    assert 'CUSTOM_ENV="deploy/.env.prod.custom"' in prod_sh
