"""Tests for the file-store `stat` + detailed `list` feature: protocol
round-trip, router query shape (static — mirrors
test_review_file_store_directory), the SDK `FileStash.stat` /
`list_detailed` frame round-trip, and the `stat_file` / enriched-list
tool dispatch. DB-free.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime

from bp_protocol.frames import (
    FileManageFrame,
    FileResultFrame,
    FileStatEntry,
    ListFileRequest,
    StatFileRequest,
    parse_frame,
)
from bp_router.db.queries import Scope
from bp_sdk import FileStash, FileStoreError, ToolCall, dispatch_file_tool, file_tools

_NOW = datetime(2026, 1, 2, 9, 0, 0, tzinfo=UTC)


def _entry(name="report.pdf", size=2048, mime="application/pdf"):
    return FileStatEntry(name=name, byte_size=size, mime_type=mime, created_at=_NOW)


# --------------------------------------------------------------------------
# protocol round-trip
# --------------------------------------------------------------------------

def _manage(cmd):
    return FileManageFrame(
        agent_id="a", trace_id="t", span_id="s", task_id="tk", command=cmd
    )


def test_stat_command_round_trips() -> None:
    rt = parse_frame(_manage(StatFileRequest(name="persist/r.pdf")).model_dump())
    assert rt.command.kind == "stat"
    assert rt.command.name == "persist/r.pdf"


def test_list_detail_flag_round_trips() -> None:
    assert parse_frame(_manage(ListFileRequest()).model_dump()).command.detail is False
    assert parse_frame(
        _manage(ListFileRequest(detail=True)).model_dump()
    ).command.detail is True


def test_result_carries_stat_and_entries() -> None:
    fr = FileResultFrame(
        agent_id="router", trace_id="t", span_id="s", ref_correlation_id="c",
        stat=_entry(), entries=[_entry(), _entry("a.png", 10, "image/png")],
    )
    rt = parse_frame(fr.model_dump())
    assert rt.stat.byte_size == 2048 and rt.stat.mime_type == "application/pdf"
    assert [e.name for e in rt.entries] == ["report.pdf", "a.png"]
    # defaults stay null when unused
    assert FileResultFrame(
        agent_id="router", trace_id="t", span_id="s", ref_correlation_id="c",
        error="not_found",
    ).stat is None


# --------------------------------------------------------------------------
# router query shape (static — no DB, mirrors the directory test)
# --------------------------------------------------------------------------

def test_stat_query_joins_and_scopes() -> None:
    src = inspect.getsource(Scope.stat_file_name)
    assert "JOIN files f" in src and "f.mime_type" in src
    # scoped by the trusted user_id (never caller-asserted)
    assert "fn.user_id = $1" in src
    assert "fn.scope = $2" in src and "fn.filename = $3" in src


def test_list_entries_query_joins_and_orders() -> None:
    src = inspect.getsource(Scope.list_file_entries)
    assert "JOIN files f" in src and "f.mime_type" in src
    assert "fn.user_id = $1" in src
    assert "ORDER BY fn.created_at DESC" in src
    # same literal-substring escaping as list_file_names
    assert "ILIKE $3 ESCAPE" in src


# --------------------------------------------------------------------------
# SDK FileStash.stat / list_detailed — frame round-trip
# --------------------------------------------------------------------------

class _FakeStash(FileStash):
    def __init__(self, result: FileResultFrame) -> None:  # bypass real init
        self._result = result
        self.sent: list = []

    def _store_frame_base(self):
        return {"agent_id": "a", "trace_id": "t", "span_id": "s", "task_id": "tk"}

    async def _round_trip(self, frame):
        self.sent.append(frame)
        return self._result


def test_stash_stat_builds_command_and_returns_entry() -> None:
    async def _drive() -> None:
        stash = _FakeStash(FileResultFrame(
            agent_id="router", trace_id="t", span_id="s",
            ref_correlation_id="c", stat=_entry(),
        ))
        out = await stash.stat("report.pdf")
        assert stash.sent[0].command.kind == "stat"
        assert stash.sent[0].command.name == "report.pdf"
        assert out.byte_size == 2048 and out.mime_type == "application/pdf"

    asyncio.run(_drive())


def test_stash_list_detailed_sets_detail_and_returns_entries() -> None:
    async def _drive() -> None:
        stash = _FakeStash(FileResultFrame(
            agent_id="router", trace_id="t", span_id="s",
            ref_correlation_id="c", entries=[_entry(), _entry("a.png", 10, "image/png")],
        ))
        out = await stash.list_detailed(persistent=True, query="a")
        cmd = stash.sent[0].command
        assert cmd.kind == "list" and cmd.detail is True and cmd.persistent is True
        assert [e.name for e in out] == ["report.pdf", "a.png"]

    asyncio.run(_drive())


# --------------------------------------------------------------------------
# tool dispatch — stat_file + enriched list
# --------------------------------------------------------------------------

class _FakeFiles:
    def __init__(self, *, stat=None, entries=None, boom=None) -> None:
        self._stat = stat
        self._entries = entries or []
        self._boom = boom

    async def stat(self, name):
        if self._boom:
            raise self._boom
        return self._stat

    async def list_detailed(self, *, persistent=False, query=None):
        return self._entries


def test_stat_file_tool_is_registered() -> None:
    assert "stat_file" in {s.name for s in file_tools("read_only")}


def test_stat_file_tool_formats_metadata() -> None:
    async def _drive() -> None:
        files = _FakeFiles(stat=_entry("big.png", 5 * 1024 * 1024, "image/png"))
        msg = await dispatch_file_tool(files, ToolCall(id="1", name="stat_file", args={"name": "big.png"}))
        assert msg.content["name"] == "big.png"
        assert msg.content["size"] == "5.0 MB"
        assert msg.content["bytes"] == 5 * 1024 * 1024
        assert msg.content["type"] == "image/png"
        assert msg.content["stored_at"].startswith("2026-01-02")

    asyncio.run(_drive())


def test_list_tool_returns_size_and_type() -> None:
    async def _drive() -> None:
        files = _FakeFiles(entries=[_entry("a.txt", 12, "text/plain"), _entry("b.png", 2048, "image/png")])
        msg = await dispatch_file_tool(files, ToolCall(id="1", name="list_session_file", args={}))
        assert msg.content["files"] == [
            {"name": "a.txt", "size": "12 B", "type": "text/plain"},
            {"name": "b.png", "size": "2.0 KB", "type": "image/png"},
        ]

    asyncio.run(_drive())


def test_stat_file_not_found_surfaces_error() -> None:
    async def _drive() -> None:
        files = _FakeFiles(boom=FileStoreError("not_found"))
        msg = await dispatch_file_tool(files, ToolCall(id="1", name="stat_file", args={"name": "nope"}))
        assert msg.content == {"error": "not_found"}

    asyncio.run(_drive())


def test_unknown_mime_renders_as_unknown() -> None:
    async def _drive() -> None:
        files = _FakeFiles(stat=_entry("x", 1, None))
        msg = await dispatch_file_tool(files, ToolCall(id="1", name="stat_file", args={"name": "x"}))
        assert msg.content["type"] == "unknown"

    asyncio.run(_drive())
