"""memory `purge_user_data` — the privileged cross-user LanceDB erase and its
service-principal guard (defence-in-depth on top of the firewall ACL)."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

mem = importlib.import_module("bp_agents.agents.memory.agent")
from bp_agents.common.payloads import PurgeUserData  # noqa: E402
from bp_agents.lance.base import user_db_path  # noqa: E402
from bp_sdk.errors import PermissionDeniedError  # noqa: E402


def _settings(tmp: Path, *, pin: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(lance_root=str(tmp), memory_purge_allowed_principal=pin)


def _ctx(*, level: str, user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user_level=level, user_id=user_id)


def _make_user_dir(tmp: Path, user_id: str) -> Path:
    path = user_db_path(str(tmp), user_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "table.lance").write_text("data")
    return path


def test_guard_rejects_non_service_caller(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mem, "_settings", _settings(tmp_path))
    target = _make_user_dir(tmp_path, "usr_target")
    with pytest.raises(PermissionDeniedError):
        asyncio.run(mem.purge_user_data_mode(
            _ctx(level="tier0", user_id="usr_attacker"),
            PurgeUserData(user_id="usr_target"),
        ))
    assert target.exists()  # not touched


def test_guard_rejects_wrong_principal_when_pinned(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mem, "_settings", _settings(tmp_path, pin="usr_svc_a"))
    target = _make_user_dir(tmp_path, "usr_target")
    # A service principal, but not the pinned one.
    with pytest.raises(PermissionDeniedError):
        asyncio.run(mem.purge_user_data_mode(
            _ctx(level="service", user_id="usr_svc_b"),
            PurgeUserData(user_id="usr_target"),
        ))
    assert target.exists()


def test_service_caller_erases_target_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mem, "_settings", _settings(tmp_path, pin="usr_svc_a"))
    target = _make_user_dir(tmp_path, "usr_target")
    other = _make_user_dir(tmp_path, "usr_bystander")
    asyncio.run(mem.purge_user_data_mode(
        _ctx(level="service", user_id="usr_svc_a"),
        PurgeUserData(user_id="usr_target"),
    ))
    assert not target.exists()       # erased
    assert other.exists()            # only the named target


def test_idempotent_when_dir_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mem, "_settings", _settings(tmp_path))
    # No dir for usr_ghost — must succeed (no-op), not raise.
    asyncio.run(mem.purge_user_data_mode(
        _ctx(level="service", user_id="usr_svc"),
        PurgeUserData(user_id="usr_ghost"),
    ))


def test_acl_grants_chatbot_service_to_memory_purge() -> None:
    import bp_agents.acl as acl

    rules = acl.suite_acl_rules()
    assert any(
        r.get("callee_pattern") == "l3/memory.purge"
        and r.get("user_level") == "service"
        for r in rules
    ), "missing service-only chatbot → memory.purge ACL rule"
