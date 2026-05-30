"""The sandbox agent maps a task's user to a uid via the LOCAL store (no DB).

Regression context: the sandbox is network-isolated from Postgres, and
`user_config.sandbox_uid` was never assigned by anything, so the per-user uid
drop never engaged. The agent now allocates uids itself via `UidStore` on its
state volume.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from bp_agents.agents.sandbox.uid_store import UidStore

# `bp_agents.agents.sandbox.agent` re-exports an `Agent` instance named
# `agent`, and the package `__init__` rebinds the name — so a plain
# `import ... .agent as sb` yields the INSTANCE, not the module. Load the
# module object explicitly.
sb = importlib.import_module("bp_agents.agents.sandbox.agent")


def _ctx(user_id: str):  # type: ignore[no-untyped-def]
    return SimpleNamespace(user_id=user_id)


def test_user_uid_none_before_startup(monkeypatch) -> None:
    monkeypatch.setattr(sb, "_uid_store", None)
    assert sb._user_uid(_ctx("usr_a")) is None


def test_user_uid_allocates_via_store(monkeypatch, tmp_path) -> None:
    store = UidStore(state_dir=tmp_path, base=100_000, maximum=100_010)
    monkeypatch.setattr(sb, "_uid_store", store)
    a = sb._user_uid(_ctx("usr_a"))
    b = sb._user_uid(_ctx("usr_b"))
    assert a == 100_000 and b == 100_001
    # Stable for a returning user.
    assert sb._user_uid(_ctx("usr_a")) == 100_000
    # Persisted on the state volume.
    assert (tmp_path / "sandbox_uids.json").exists()


def test_agent_uses_no_db(monkeypatch) -> None:
    """The sandbox must not import/use the suite DB pool anymore."""
    import inspect

    src = inspect.getsource(sb)
    assert "open_pool" not in src
    assert "get_user_config" not in src
    assert "UidStore" in src


def test_run_bash_chowns_workspace_when_dropping() -> None:
    """Source pin: run_bash chowns the workspace to the uid before running, so
    the dropped command can write to its (root-created) workspace dir."""
    import inspect

    src = inspect.getsource(sb.run_bash)
    assert "os.chown" in src
    chown = src.find("os.chown")
    subproc = src.find("create_subprocess_shell")
    assert 0 < chown < subproc, "chown must happen before the subprocess starts"
