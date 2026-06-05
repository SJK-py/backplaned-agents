"""Regression: the bridge self-heals its state-dir ownership at startup.

The bridge runs as root with cap_drop: ALL (no CAP_DAC_OVERRIDE), so it is
subject to the state dir's mode bits. A named volume seeded by an earlier
image keeps its old (non-root) owner, and `state_dir.mkdir()` for the
per-server agent dir then fails with EACCES on /mcp-state/<agent> — the live
`PermissionError: [Errno 13] Permission denied: '/mcp-state/mcp_minimax'`.
`_ensure_state_dir` re-owns the tree (root holds CAP_CHOWN) before any persist.
"""

from __future__ import annotations

import os
from pathlib import Path

from bp_mcp_bridge.__main__ import _ensure_state_dir


def test_ensure_state_dir_creates_dir(tmp_path: Path) -> None:
    target = tmp_path / "mcp-state"
    _ensure_state_dir(target)
    assert target.is_dir()
    # Idempotent — a second call on an existing tree must not raise.
    _ensure_state_dir(target)
    assert target.is_dir()


def test_ensure_state_dir_reowns_tree_when_root(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When running as root, re-own the dir + every inherited entry to self
    and lock the top dir to 0700. Mock the privileged syscalls so the test
    runs unprivileged but still exercises the root branch."""
    state = tmp_path / "mcp-state"
    state.mkdir()
    (state / "service_token.json").write_text("{}")
    agent = state / "mcp_minimax"
    agent.mkdir()
    (agent / "credentials.json").write_text("{}")

    chowned: list[tuple[str, int, int]] = []
    chmodded: list[tuple[str, int]] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getegid", lambda: 0)
    monkeypatch.setattr(
        os, "chown", lambda p, u, g: chowned.append((str(p), u, g)),
    )
    monkeypatch.setattr(os, "chmod", lambda p, m: chmodded.append((str(p), m)))

    _ensure_state_dir(state)

    owned = {p for p, _, _ in chowned}
    assert str(state) in owned
    assert str(agent) in owned                       # inherited subdir
    assert str(agent / "credentials.json") in owned  # inherited file
    assert str(state / "service_token.json") in owned
    assert all(u == 0 and g == 0 for _, u, g in chowned)
    assert (str(state), 0o700) in chmodded


def test_ensure_state_dir_skips_chown_when_not_root(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Unprivileged (dev / rootless) — never touch ownership; just ensure
    the dir exists."""
    state = tmp_path / "mcp-state"
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("chown must not be called when not root")

    monkeypatch.setattr(os, "chown", _boom)
    _ensure_state_dir(state)
    assert state.is_dir()
