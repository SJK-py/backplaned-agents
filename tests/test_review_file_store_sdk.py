"""Router-managed file store — SDK `FileStash` (cutover phase 4/6).

The agent-facing name-based file API that replaces `ProxyFileManager`.
Each method round-trips a `File*` frame to the router and returns
NAMES, not opaque refs. Bytes never ride the WS frame (`store`
uploads over HTTP; `read` pulls a signed URL; `llm_ref` is a name
reference the router resolves).

These tests drive `FileStash` against a fake dispatcher that
completes the `pending_acks` round-trip with a canned `FileResult`,
so the frame construction + reply handling are exercised without a
live router. The HTTP upload/download legs are stubbed (covered by
the router-side handler tests + the integration suite).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from bp_protocol.frames import (
    CopyFileRequest,
    DeleteFileRequest,
    FileManageFrame,
    FileResultFrame,
    FileStoreFrame,
    ListFileRequest,
    WriteFileRequest,
)
from bp_sdk.files import FileStash, FileStoreError


class _Transport:
    def __init__(self, disp: _Disp, reply: Callable) -> None:
        self._disp = disp
        self._reply = reply
        self.sent: list = []

    async def send(self, frame) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(frame)
        fut = self._disp.pending.pop(frame.correlation_id)
        fut.set_result(self._reply(frame))


class _Disp:
    """Minimal dispatcher: register_for_task → future; transport.send
    resolves it with the configured reply built from the sent frame."""

    def __init__(self, reply: Callable) -> None:
        self.agent = SimpleNamespace(info=SimpleNamespace(agent_id="agt"))
        self.pending: dict = {}
        self.pending_acks = object()
        self.transport = _Transport(self, reply)

    def register_for_task(self, _pmap, corr, _task):  # type: ignore[no-untyped-def]
        fut = asyncio.get_running_loop().create_future()
        self.pending[corr] = fut
        return fut


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(task_id="task_1", trace_id="0" * 32, span_id="0" * 16)


def _stash(reply: Callable, tmp_path: Path) -> FileStash:
    return FileStash(
        _ctx(), inbox_dir=tmp_path, router_url="http://router",
        dispatcher=_Disp(reply),
    )


def _result(frame, **kw):  # type: ignore[no-untyped-def]
    return FileResultFrame(
        agent_id="router", trace_id="0" * 32, span_id="0" * 16,
        ref_correlation_id=frame.correlation_id, **kw,
    )


# ---------------------------------------------------------------------------
# llm_ref — pure
# ---------------------------------------------------------------------------


def test_llm_ref_shape(tmp_path: Path) -> None:
    fs = _stash(_result, tmp_path)
    assert fs.llm_ref("chart.png") == {"file_ref": {"name": "chart.png"}}
    assert fs.llm_ref("persist/r.pdf", as_="document") == {
        "file_ref": {"name": "persist/r.pdf", "as": "document"}
    }


# ---------------------------------------------------------------------------
# store — uploads (stubbed) then names
# ---------------------------------------------------------------------------


def test_store_sends_file_store_frame_and_returns_saved_name(
    tmp_path: Path, monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    sent: dict = {}

    def reply(frame):  # type: ignore[no-untyped-def]
        sent["frame"] = frame
        return _result(frame, saved_name="chart_1.png")

    fs = _stash(reply, tmp_path)

    async def _no_upload(*a, **k):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(fs, "_upload_blob", _no_upload)

    saved = asyncio.run(fs.store(
        b"PNGDATA", filename="chart.png", persistent=True, dedup="overwrite",
    ))
    assert saved == "chart_1.png"
    f = sent["frame"]
    assert isinstance(f, FileStoreFrame)
    assert f.filename == "chart.png"
    assert f.persistent is True
    assert f.dedup == "overwrite"
    assert f.byte_size == len(b"PNGDATA")


def test_write_sends_write_command(tmp_path: Path) -> None:
    captured: dict = {}

    def reply(frame):  # type: ignore[no-untyped-def]
        captured["frame"] = frame
        return _result(frame, saved_name="notes.txt")

    fs = _stash(reply, tmp_path)
    saved = asyncio.run(fs.write("notes.txt", "hello", persistent=False))
    assert saved == "notes.txt"
    f = captured["frame"]
    assert isinstance(f, FileManageFrame)
    assert isinstance(f.command, WriteFileRequest)
    assert f.command.text == "hello"


def test_list_returns_names(tmp_path: Path) -> None:
    captured: dict = {}

    def reply(frame):  # type: ignore[no-untyped-def]
        captured["frame"] = frame
        return _result(frame, names=["a.txt", "b.png"])

    fs = _stash(reply, tmp_path)
    names = asyncio.run(fs.list(persistent=True, query="a"))
    assert names == ["a.txt", "b.png"]
    cmd = captured["frame"].command
    assert isinstance(cmd, ListFileRequest)
    assert cmd.persistent is True and cmd.query == "a"


def test_delete_returns_count(tmp_path: Path) -> None:
    captured: dict = {}

    def reply(frame):  # type: ignore[no-untyped-def]
        captured["frame"] = frame
        return _result(frame, deleted_count=3)

    fs = _stash(reply, tmp_path)
    n = asyncio.run(fs.delete("draft_*"))
    assert n == 3
    assert isinstance(captured["frame"].command, DeleteFileRequest)
    assert captured["frame"].command.name == "draft_*"


def test_copy_move_flag(tmp_path: Path) -> None:
    captured: dict = {}

    def reply(frame):  # type: ignore[no-untyped-def]
        captured["frame"] = frame
        return _result(frame, saved_name="persist/r.pdf")

    fs = _stash(reply, tmp_path)
    saved = asyncio.run(fs.copy("r.pdf", "persist/r.pdf", move=True))
    assert saved == "persist/r.pdf"
    cmd = captured["frame"].command
    assert isinstance(cmd, CopyFileRequest)
    assert cmd.delete_original is True


# ---------------------------------------------------------------------------
# Error reply → FileStoreError
# ---------------------------------------------------------------------------


def test_router_error_raises_filestoreerror(tmp_path: Path) -> None:
    fs = _stash(lambda f: _result(f, error="quota_exceeded"), tmp_path)
    with pytest.raises(FileStoreError) as ei:
        asyncio.run(fs.write("big.txt", "x"))
    assert ei.value.code == "quota_exceeded"


def test_no_dispatcher_raises(tmp_path: Path) -> None:
    fs = FileStash(_ctx(), inbox_dir=tmp_path, router_url="http://r", dispatcher=None)
    with pytest.raises(RuntimeError, match="requires a dispatcher"):
        asyncio.run(fs.list())


# ---------------------------------------------------------------------------
# SDK dispatch routes FileResult to pending_acks
# ---------------------------------------------------------------------------


def test_dispatch_routes_file_result_to_pending_acks() -> None:
    pytest.importorskip("fastapi")
    import inspect

    from bp_sdk import dispatch as d

    src = inspect.getsource(d)
    assert "isinstance(frame, FileResultFrame)" in src
    assert "self.pending_acks.resolve(frame.ref_correlation_id, frame)" in src
