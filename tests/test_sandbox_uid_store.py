"""bp_agents.agents.sandbox.uid_store — local per-user uid allocation.

The sandbox owns the `user_id → uid` map in a JSON file on its state volume
(it's network-isolated from the suite DB). uids are allocated sequentially
from a base; a returning user keeps its uid; the range is bounded. This is the
allocation half that was missing — `user_config.sandbox_uid` was never written
by anything, so the per-user uid drop never engaged.
"""

from __future__ import annotations

import json

from bp_agents.agents.sandbox.uid_store import UidStore


def _store(tmp_path, base=100_000, maximum=100_010):  # type: ignore[no-untyped-def]
    return UidStore(state_dir=tmp_path, base=base, maximum=maximum)


def test_first_user_gets_base(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.uid_for("usr_a") == 100_000


def test_sequential_distinct_uids(tmp_path) -> None:
    s = _store(tmp_path)
    a = s.uid_for("usr_a")
    b = s.uid_for("usr_b")
    c = s.uid_for("usr_c")
    assert [a, b, c] == [100_000, 100_001, 100_002]
    assert len({a, b, c}) == 3  # the whole point: never collide


def test_returning_user_keeps_uid(tmp_path) -> None:
    s = _store(tmp_path)
    first = s.uid_for("usr_a")
    s.uid_for("usr_b")
    assert s.uid_for("usr_a") == first  # stable across calls


def test_persists_across_instances(tmp_path) -> None:
    s1 = _store(tmp_path)
    a = s1.uid_for("usr_a")
    b = s1.uid_for("usr_b")
    # A fresh instance (≈ agent restart) reads the same file.
    s2 = _store(tmp_path)
    assert s2.uid_for("usr_a") == a
    assert s2.uid_for("usr_b") == b
    # And a NEW user continues the sequence, not restart from base.
    assert s2.uid_for("usr_c") == b + 1


def test_range_exhaustion_returns_none(tmp_path) -> None:
    # base..max inclusive = 3 slots.
    s = _store(tmp_path, base=100_000, maximum=100_002)
    assert s.uid_for("u0") == 100_000
    assert s.uid_for("u1") == 100_001
    assert s.uid_for("u2") == 100_002
    # 4th user has no free uid → None (caller runs without a drop, never a
    # colliding uid).
    assert s.uid_for("u3") is None


def test_skips_already_taken_values(tmp_path) -> None:
    # A hand-edited / gapped file: base is taken by a non-sequential entry.
    path = tmp_path / "sandbox_uids.json"
    path.write_text(json.dumps({"manual": 100_000}))
    s = _store(tmp_path)
    # Next allocation must skip 100000 (taken) → 100001.
    assert s.uid_for("usr_a") == 100_001


def test_corrupt_store_starts_fresh(tmp_path) -> None:
    path = tmp_path / "sandbox_uids.json"
    path.write_text("{not valid json")
    s = _store(tmp_path)
    assert s.uid_for("usr_a") == 100_000  # didn't crash; reallocated


def test_written_file_shape(tmp_path) -> None:
    s = _store(tmp_path)
    s.uid_for("usr_a")
    s.uid_for("usr_b")
    data = json.loads((tmp_path / "sandbox_uids.json").read_text())
    assert data == {"usr_a": 100_000, "usr_b": 100_001}
