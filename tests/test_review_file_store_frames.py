"""Router-managed file store — Phase 2: the FileStore / FileFetch /
FileManage frames + their router handlers.

Implements `docs/design/router-managed-file-store.md` §4. Additive —
new frame types + handlers; no existing path touched.

Coverage:
  * Frame shapes + the discriminated `command` union + the
    FileResult reply.
  * Pure name helpers (split / scope / display / dedup-candidate).
  * `_allocate_name` dedup policy (new / idempotent / error /
    overwrite / append_count) against an in-memory fake Scope.
  * `_quota_ok` ceiling gate.
  * Handler source pins: identity DERIVED from the task row +
    active-executor check (never asserted), audit on mutations.

DB-integration (real pool / conn / audit chain) runs in the
integration suite.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

# ===========================================================================
# Frames
# ===========================================================================


def test_file_frames_parse_and_join_union() -> None:
    from bp_protocol.frames import (
        FileFetchFrame,
        FileManageFrame,
        FileResultFrame,
        FileStoreFrame,
        parse_frame,
    )

    base = {"agent_id": "a", "trace_id": "0" * 32, "span_id": "0" * 16}
    store = parse_frame({**base, "type": "FileStore", "task_id": "t",
                         "sha256": "abc", "byte_size": 10, "filename": "x.png"})
    assert isinstance(store, FileStoreFrame)
    assert store.dedup == "append_count" and store.persistent is False

    fetch = parse_frame({**base, "type": "FileFetch", "task_id": "t",
                         "name": "persist/r.pdf"})
    assert isinstance(fetch, FileFetchFrame)

    manage = parse_frame({**base, "type": "FileManage", "task_id": "t",
                          "command": {"kind": "list", "persistent": True}})
    assert isinstance(manage, FileManageFrame)

    res = parse_frame({**base, "type": "FileResult",
                       "ref_correlation_id": "c1", "saved_name": "x_1.png"})
    assert isinstance(res, FileResultFrame)
    assert res.saved_name == "x_1.png"


def test_file_command_discriminated_union() -> None:
    from bp_protocol.frames import (
        CopyFileRequest,
        DeleteFileRequest,
        ListFileRequest,
        WriteFileRequest,
        parse_frame,
    )

    base = {"agent_id": "a", "trace_id": "0" * 32, "span_id": "0" * 16,
            "type": "FileManage", "task_id": "t"}
    assert isinstance(parse_frame({**base, "command": {"kind": "list"}}).command, ListFileRequest)
    assert isinstance(parse_frame({**base, "command": {"kind": "delete", "name": "a*"}}).command, DeleteFileRequest)
    cp = parse_frame({**base, "command": {"kind": "copy", "src": "a", "dst": "b", "delete_original": True}}).command
    assert isinstance(cp, CopyFileRequest) and cp.delete_original is True
    wr = parse_frame({**base, "command": {"kind": "write", "filename": "n.txt", "text": "hi"}}).command
    assert isinstance(wr, WriteFileRequest) and wr.dedup == "append_count"


def test_unknown_command_kind_rejected() -> None:
    from pydantic import ValidationError

    from bp_protocol.frames import parse_frame

    base = {"agent_id": "a", "trace_id": "0" * 32, "span_id": "0" * 16,
            "type": "FileManage", "task_id": "t"}
    with pytest.raises(ValidationError):
        parse_frame({**base, "command": {"kind": "nope"}})


# ===========================================================================
# Pure name helpers
# ===========================================================================


def test_split_stash_name() -> None:
    from bp_router.file_store import _split_stash_name

    assert _split_stash_name("chart.png") == (False, "chart.png")
    assert _split_stash_name("persist/r.pdf") == (True, "r.pdf")
    # invalid: nested path, empty, control char
    assert _split_stash_name("a/b") is None
    assert _split_stash_name("") is None
    assert _split_stash_name("persist/") is None
    assert _split_stash_name("bad\x00name") is None


def test_scope_and_display() -> None:
    from bp_router.file_store import _display_name, _scope_for

    assert _scope_for(False, "sess_1") == "session:sess_1"
    assert _scope_for(True, "sess_1") == "persist"
    assert _display_name(False, "x.png") == "x.png"
    assert _display_name(True, "x.png") == "persist/x.png"


def test_next_dedup_candidate() -> None:
    from bp_router.file_store import _next_dedup_candidate

    assert _next_dedup_candidate("report.pdf", 1) == "report_1.pdf"
    assert _next_dedup_candidate("README", 2) == "README_2"
    # multi-dot keeps the LAST extension
    assert _next_dedup_candidate("a.tar.gz", 1) == "a.tar_1.gz"


# ===========================================================================
# Fake Scope for dedup / quota behaviour
# ===========================================================================


class _FakeScope:
    def __init__(self, user: str = "u1") -> None:
        self._user = user
        self.rows: dict[tuple[str, str], SimpleNamespace] = {}

    def _require_user(self) -> str:
        return self._user

    async def resolve_file_name(self, scope: str, filename: str):
        return self.rows.get((scope, filename))

    async def insert_file_name(self, *, scope, filename, file_id, byte_size) -> bool:
        if (scope, filename) in self.rows:
            return False
        self.rows[(scope, filename)] = SimpleNamespace(
            filename=filename, file_id=file_id, byte_size=byte_size,
        )
        return True

    async def repoint_file_name(self, *, scope, filename, file_id, byte_size) -> None:
        self.rows[(scope, filename)] = SimpleNamespace(
            filename=filename, file_id=file_id, byte_size=byte_size,
        )

    async def count_user_storage_bytes(self) -> int:
        return sum(r.byte_size for r in self.rows.values())

    async def delete_file_name(self, scope: str, filename: str) -> int:
        return 1 if self.rows.pop((scope, filename), None) is not None else 0


def test_allocate_new_name() -> None:
    from bp_router.file_store import _allocate_name

    sq = _FakeScope()
    saved, err, added = asyncio.run(_allocate_name(
        sq, scope="session:s", filename="a.txt", file_id="f1",
        byte_size=10, dedup="append_count",
    ))
    assert (saved, err, added) == ("a.txt", None, 10)


def test_allocate_idempotent_same_blob() -> None:
    from bp_router.file_store import _allocate_name

    sq = _FakeScope()
    asyncio.run(_allocate_name(sq, scope="s", filename="a.txt", file_id="f1",
                               byte_size=10, dedup="append_count"))
    # Same name + same blob → no new bytes, same name.
    saved, err, added = asyncio.run(_allocate_name(
        sq, scope="s", filename="a.txt", file_id="f1", byte_size=10,
        dedup="append_count",
    ))
    assert (saved, err, added) == ("a.txt", None, 0)


def test_allocate_error_on_collision() -> None:
    from bp_router.file_store import _allocate_name

    sq = _FakeScope()
    asyncio.run(_allocate_name(sq, scope="s", filename="a.txt", file_id="f1",
                               byte_size=10, dedup="error"))
    saved, err, added = asyncio.run(_allocate_name(
        sq, scope="s", filename="a.txt", file_id="f2", byte_size=20,
        dedup="error",
    ))
    assert saved is None and err == "filename_exists"


def test_allocate_overwrite_repoints_and_returns_delta() -> None:
    from bp_router.file_store import _allocate_name

    sq = _FakeScope()
    asyncio.run(_allocate_name(sq, scope="s", filename="a.txt", file_id="f1",
                               byte_size=10, dedup="overwrite"))
    saved, err, added = asyncio.run(_allocate_name(
        sq, scope="s", filename="a.txt", file_id="f2", byte_size=30,
        dedup="overwrite",
    ))
    assert (saved, err, added) == ("a.txt", None, 20)  # 30 - 10
    assert sq.rows[("s", "a.txt")].file_id == "f2"


def test_allocate_append_count_finds_free_slot() -> None:
    from bp_router.file_store import _allocate_name

    sq = _FakeScope()
    asyncio.run(_allocate_name(sq, scope="s", filename="a.txt", file_id="f1",
                               byte_size=10, dedup="append_count"))
    saved, err, added = asyncio.run(_allocate_name(
        sq, scope="s", filename="a.txt", file_id="f2", byte_size=20,
        dedup="append_count",
    ))
    assert (saved, err, added) == ("a_1.txt", None, 20)
    # A third different-content store bumps to _2.
    saved2, _e, _a = asyncio.run(_allocate_name(
        sq, scope="s", filename="a.txt", file_id="f3", byte_size=5,
        dedup="append_count",
    ))
    assert saved2 == "a_2.txt"


# ===========================================================================
# _file_copy — copy / move
# ===========================================================================


def test_file_copy_onto_self_is_noop_not_delete() -> None:
    """Regression: copy/move onto the SAME name must NOT delete the
    file. Pre-fix, `move(x → x)` allocated idempotently and then
    deleted the source — dropping the only name pointing at the blob."""
    from bp_protocol.frames import CopyFileRequest, FileManageFrame
    from bp_router.dispatch import _file_copy

    sq = _FakeScope()
    sq.rows[("session:s1", "a.txt")] = SimpleNamespace(
        filename="a.txt", file_id="f1", byte_size=10,
    )
    sent: list = []

    async def _put(f):  # type: ignore[no-untyped-def]
        sent.append(f)

    entry = SimpleNamespace(agent_id="agt", outbox=SimpleNamespace(put=_put))
    cmd = CopyFileRequest(src="a.txt", dst="a.txt", delete_original=True)
    frame = FileManageFrame(
        agent_id="agt", trace_id="t" * 32, span_id="s" * 16,
        task_id="tsk_1", command=cmd,
    )
    # conn is unused on the no-op path (it returns before the txn).
    asyncio.run(_file_copy(
        state=None, entry=entry, frame=frame, conn=None,
        sq=sq, session_id="s1", cmd=cmd,
    ))
    assert ("session:s1", "a.txt") in sq.rows  # NOT deleted
    assert len(sent) == 1
    assert sent[0].error is None
    assert sent[0].saved_name == "a.txt"


# ===========================================================================
# Quota gate
# ===========================================================================


def test_quota_ok_uncapped_and_capped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import bp_router.tasks as tasks_mod
    from bp_router.file_store import _quota_ok

    sq = _FakeScope()
    asyncio.run(sq.insert_file_name(scope="s", filename="x", file_id="f", byte_size=900))

    async def _level(_state, _uid):
        return "tier3"

    monkeypatch.setattr(tasks_mod, "_session_level", _level)

    state = SimpleNamespace(settings=SimpleNamespace(
        file_storage_quota_bytes={"tier3": 1000, "admin": None},
    ))

    # 900 used + 50 = 950 ≤ 1000 → ok.
    assert asyncio.run(_quota_ok(state, sq, "u1", 50)) is True
    # 900 + 200 = 1100 > 1000 → refused.
    assert asyncio.run(_quota_ok(state, sq, "u1", 200)) is False
    # added <= 0 always passes (idempotent / shrink).
    assert asyncio.run(_quota_ok(state, sq, "u1", 0)) is True


def test_quota_ok_none_ceiling_is_unlimited(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import bp_router.tasks as tasks_mod
    from bp_router.file_store import _quota_ok

    sq = _FakeScope()

    async def _level(_state, _uid):
        return "admin"

    monkeypatch.setattr(tasks_mod, "_session_level", _level)
    state = SimpleNamespace(settings=SimpleNamespace(
        file_storage_quota_bytes={"admin": None},
    ))
    assert asyncio.run(_quota_ok(state, sq, "u1", 10 ** 12)) is True


# ===========================================================================
# Handler source pins — derive-from-task authz + audit
# ===========================================================================


def test_derive_task_scope_checks_active_executor() -> None:
    from bp_router.dispatch import _derive_task_scope

    src = inspect.getsource(_derive_task_scope)
    # Reads user_id + session_id + active_agent_id from the task row.
    assert "SELECT user_id, session_id, active_agent_id FROM tasks" in src
    # Refuses unless the connecting agent is the active executor.
    assert 'row["active_agent_id"] != agent_id' in src


def test_handlers_derive_scope_not_assert() -> None:
    """All three handlers route identity through `_derive_task_scope`
    (task-row derivation + active-executor), never an asserted
    user_id/session_id."""
    import bp_router.dispatch as d

    for fn in (d._handle_file_store, d._handle_file_fetch, d._handle_file_manage):
        src = inspect.getsource(fn)
        assert "_derive_task_scope(conn, frame.task_id, entry.agent_id)" in src
        assert 'error="denied"' in src


def test_mutations_are_audited() -> None:
    """store / delete / copy / write append an audit event;
    fetch (read-only) does not."""
    import bp_router.dispatch as d

    assert 'event="file.store"' in inspect.getsource(d._handle_file_store)
    assert 'event="file.delete"' in inspect.getsource(d._handle_file_manage)
    assert 'event="file.copy"' in inspect.getsource(d._file_copy)
    assert 'event="file.write"' in inspect.getsource(d._file_write)
    # Fetch is read-only — no audit append.
    assert "append_audit_event" not in inspect.getsource(d._handle_file_fetch)


def test_store_quota_gated_before_allocation() -> None:
    import bp_router.dispatch as d

    src = inspect.getsource(d._handle_file_store)
    q_idx = src.index("_quota_ok(")
    alloc_idx = src.index("_allocate_name(")
    assert q_idx < alloc_idx, "quota must be checked before allocation"


def test_store_requires_uploaded_blob() -> None:
    """FileStore binds an EXISTING blob (uploaded via grant); a
    sha256 with no `files` row replies not_found, never fabricates."""
    import bp_router.dispatch as d

    src = inspect.getsource(d._handle_file_store)
    assert "get_file_by_sha256(frame.sha256)" in src
    assert 'error="not_found"' in src


def test_dispatch_routes_file_frames() -> None:
    import bp_router.dispatch as d

    # Grep the whole module to stay robust to the routing
    # entrypoint's name.
    msrc = inspect.getsource(d)
    assert "_handle_file_store(state, entry, frame)" in msrc
    assert "_handle_file_fetch(state, entry, frame)" in msrc
    assert "_handle_file_manage(state, entry, frame)" in msrc
