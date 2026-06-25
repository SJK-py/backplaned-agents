"""Router-managed file store — Phase 6: LLM tool bundles
(`file_tools`) + the `dispatch_file_tool` executor.

`file_tools(bundle=...)` ships ready-made `ToolSpec`s; when the model
calls one, `dispatch_file_tool(ctx.files, tool_call)` runs it against
the stash and builds the tool response. `read_file` emits a name
`file_ref` (the ROUTER resolves it on the next turn — bytes never
enter the agent); the others echo the result. A `FileStoreError` is
surfaced as `{"error": code}` so the model can recover.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from bp_protocol.frames import FileStatEntry
from bp_sdk.files import FileStoreError

_NOW = datetime(2026, 1, 2, tzinfo=UTC)


class _FakeStash:
    """Records calls; returns canned values. Mirrors only the
    `FileStash` methods `dispatch_file_tool` touches."""

    def __init__(self, *, raise_code: str | None = None) -> None:
        self.calls: list[tuple] = []
        self._raise = raise_code

    async def list_detailed(self, *, persistent=False, query=None):  # type: ignore[no-untyped-def]
        self.calls.append(("list_detailed", persistent, query))
        if self._raise:
            raise FileStoreError(self._raise)
        return [
            FileStatEntry(name="a.txt", byte_size=12, mime_type="text/plain", created_at=_NOW),
            FileStatEntry(name="b.png", byte_size=2048, mime_type="image/png", created_at=_NOW),
        ]

    async def stat(self, name):  # type: ignore[no-untyped-def]
        self.calls.append(("stat", name))
        if self._raise:
            raise FileStoreError(self._raise)
        return FileStatEntry(name=name, byte_size=2048, mime_type="image/png", created_at=_NOW)

    async def write(self, filename, text, *, persistent=False):  # type: ignore[no-untyped-def]
        self.calls.append(("write", filename, text, persistent))
        if self._raise:
            raise FileStoreError(self._raise)
        return f"{filename}"  # echo (a real dedup append may rename)

    async def delete(self, name):  # type: ignore[no-untyped-def]
        self.calls.append(("delete", name))
        if self._raise:
            raise FileStoreError(self._raise)
        return 2

    async def copy(self, src, dst, *, move=False):  # type: ignore[no-untyped-def]
        self.calls.append(("copy", src, dst, move))
        if self._raise:
            raise FileStoreError(self._raise)
        return dst

    def llm_ref(self, name, *, as_=None):  # type: ignore[no-untyped-def]
        ref = {"name": name}
        if as_:
            ref["as"] = as_
        return {"file_ref": ref}


def _call(tool_name: str, /, **args):  # type: ignore[no-untyped-def]
    from bp_sdk.llm import ToolCall

    return ToolCall(id="tc_1", name=tool_name, args=dict(args))


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


def test_read_only_bundle_names() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import file_tools

    assert [t.name for t in file_tools("read_only")] == [
        "list_session_file",
        "list_persist_file",
        "stat_file",
        "read_file",
    ]


def test_full_bundle_adds_mutating_tools() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import file_tools

    names = [t.name for t in file_tools("full")]
    assert names == [
        "list_session_file",
        "list_persist_file",
        "stat_file",
        "read_file",
        "write_file",
        "delete_file",
        "copy_file",
    ]


def test_default_bundle_is_read_only() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import file_tools

    assert [t.name for t in file_tools()] == [t.name for t in file_tools("read_only")]


def test_unknown_bundle_raises() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import file_tools

    with pytest.raises(ValueError, match="bundle"):
        file_tools("bogus")  # type: ignore[arg-type]


def test_read_file_schema_requires_name() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import file_tools

    rf = next(t for t in file_tools("read_only") if t.name == "read_file")
    assert rf.parameters["required"] == ["name"]
    assert "name" in rf.parameters["properties"]


def test_is_file_tool() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import is_file_tool

    assert is_file_tool("read_file") is True
    assert is_file_tool("write_file") is True
    assert is_file_tool("call_some_agent") is False


# ---------------------------------------------------------------------------
# dispatch — read tools
# ---------------------------------------------------------------------------


def test_read_file_emits_name_file_ref() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(dispatch_file_tool(stash, _call("read_file", name="chart.png")))
    assert msg.role == "tool"
    assert msg.tool_call_id == "tc_1"
    assert msg.name == "read_file"
    # The tool result is a NAME file_ref — the router resolves it next turn.
    assert msg.content == [{"file_ref": {"name": "chart.png"}}]


def test_list_session_file_lists_session_scope() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(
        dispatch_file_tool(stash, _call("list_session_file", query="a"))
    )
    # Enriched: each entry carries name + human size + type.
    assert msg.content == {"files": [
        {"name": "a.txt", "size": "12 B", "type": "text/plain"},
        {"name": "b.png", "size": "2.0 KB", "type": "image/png"},
    ]}
    assert stash.calls == [("list_detailed", False, "a")]


def test_list_persist_file_lists_persist_scope() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    asyncio.run(dispatch_file_tool(stash, _call("list_persist_file")))
    assert stash.calls == [("list_detailed", True, None)]


def test_read_file_missing_name_is_soft_error() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(dispatch_file_tool(stash, _call("read_file")))
    assert msg.content == {"error": "read_file requires a 'name'"}


# ---------------------------------------------------------------------------
# dispatch — mutating tools
# ---------------------------------------------------------------------------


def test_write_file_echoes_saved_name() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(
        dispatch_file_tool(
            stash, _call("write_file", filename="notes.txt", text="hi", persistent=True)
        )
    )
    assert msg.content == {"saved_name": "notes.txt"}
    assert stash.calls == [("write", "notes.txt", "hi", True)]


def test_delete_file_returns_count() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(dispatch_file_tool(stash, _call("delete_file", name="draft_*")))
    assert msg.content == {"deleted_count": 2}
    assert stash.calls == [("delete", "draft_*")]


def test_copy_file_passes_move_flag_and_echoes_dst() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(
        dispatch_file_tool(
            stash, _call("copy_file", src="r.pdf", dst="persist/r.pdf", move=True)
        )
    )
    assert msg.content == {"saved_name": "persist/r.pdf"}
    assert stash.calls == [("copy", "r.pdf", "persist/r.pdf", True)]


def test_write_file_missing_args_is_soft_error() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    msg = asyncio.run(dispatch_file_tool(stash, _call("write_file", filename="x.txt")))
    assert "error" in msg.content
    assert stash.calls == []  # never reached the stash


# ---------------------------------------------------------------------------
# dispatch — error surfacing + guard
# ---------------------------------------------------------------------------


def test_file_store_error_surfaced_as_tool_response() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash(raise_code="quota_exceeded")
    msg = asyncio.run(
        dispatch_file_tool(
            stash, _call("write_file", filename="big.txt", text="x")
        )
    )
    # Surfaced (not raised) so the model can recover.
    assert msg.content == {"error": "quota_exceeded"}


def test_dispatch_rejects_non_file_tool() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk import dispatch_file_tool

    stash = _FakeStash()
    with pytest.raises(ValueError, match="not a file tool"):
        asyncio.run(dispatch_file_tool(stash, _call("call_some_agent", x=1)))
