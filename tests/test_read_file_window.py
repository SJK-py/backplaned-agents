"""read_file character windowing (text files): default 20k cap, max_chars
+ offset paging, truncation marker, and the image/PDF / non-UTF-8 / oversize
fallbacks. DB-free — the file store is faked.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from bp_sdk import ToolCall, dispatch_file_tool, file_tools
from bp_sdk.files import FileStoreError

ft = importlib.import_module("bp_sdk.file_tools")


class _FakeFiles:
    """Returns canned bytes for `read_bytes`; raises for names in `missing`."""

    def __init__(self, data: bytes = b"", *, missing: tuple = ()) -> None:
        self._data = data
        self._missing = missing
        self.read_calls: list[str] = []

    def llm_ref(self, name, *, as_=None):
        return {"file_ref": {"name": name}}

    async def read_bytes(self, name):
        self.read_calls.append(name)
        if name in self._missing:
            raise FileStoreError("not_found")
        return self._data


def _read(files, name, **args):
    return asyncio.run(
        dispatch_file_tool(files, ToolCall(id="1", name="read_file", args={"name": name, **args}))
    ).content


# --------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------

def test_read_file_spec_exposes_window_args() -> None:
    rf = next(s for s in file_tools("read_only") if s.name == "read_file")
    props = rf.parameters["properties"]
    assert "max_chars" in props and "offset" in props
    assert rf.parameters["required"] == ["name"]  # both optional


# --------------------------------------------------------------------------
# text detection
# --------------------------------------------------------------------------

def test_is_text_name() -> None:
    assert ft._is_text_name("notes.md") and ft._is_text_name("a.json")
    assert ft._is_text_name("data.csv") and ft._is_text_name("x.yaml")
    assert not ft._is_text_name("chart.png")
    assert not ft._is_text_name("report.pdf")
    assert not ft._is_text_name("noext")


# --------------------------------------------------------------------------
# windowing
# --------------------------------------------------------------------------

def test_short_text_returns_whole_no_marker() -> None:
    content = _read(_FakeFiles(b"hello world"), "s.txt")
    assert content == "File: s.txt (characters 0–11 of 11)\n\nhello world"


def test_default_cap_truncates_with_marker() -> None:
    big = ("x" * 100 + "\n") * 1000  # ~101000 chars
    content = _read(_FakeFiles(big.encode()), "big.txt")
    assert content.startswith(f"File: big.txt (characters 0–{ft._DEFAULT_READ_CHARS} of {len(big)})")
    body = content.split("\n\n", 1)[1]
    # body = the window + the marker; the window itself is exactly the cap
    assert body.startswith("x" * 50)
    assert f"call read_file again with offset={ft._DEFAULT_READ_CHARS} to continue" in content
    assert f"{len(big) - ft._DEFAULT_READ_CHARS} more characters" in content


def test_offset_and_max_chars_paging() -> None:
    text = "".join(str(i % 10) for i in range(100))  # 100 chars "0123..."
    content = _read(_FakeFiles(text.encode()), "p.txt", offset=10, max_chars=5)
    assert "(characters 10–15 of 100)" in content
    window = content.split("\n\n", 1)[1].split("\n\n…", 1)[0]
    assert window == text[10:15]
    assert "call read_file again with offset=15" in content


def test_max_chars_clamped_to_hard_ceiling() -> None:
    text = "a" * (ft._MAX_READ_CHARS + 5000)
    content = _read(_FakeFiles(text.encode()), "p.txt", max_chars=10**9)
    assert f"characters 0–{ft._MAX_READ_CHARS} of {len(text)}" in content


def test_offset_past_end_is_empty_window() -> None:
    content = _read(_FakeFiles(b"abc"), "s.txt", offset=999)
    assert "(characters 3–3 of 3)" in content
    assert "more characters" not in content


def test_non_int_args_fall_back_to_defaults() -> None:
    content = _read(_FakeFiles(b"hello"), "s.txt", max_chars="lots", offset="start")
    assert content == "File: s.txt (characters 0–5 of 5)\n\nhello"


# --------------------------------------------------------------------------
# fallbacks
# --------------------------------------------------------------------------

def test_image_returns_file_ref_unchanged() -> None:
    files = _FakeFiles(b"PNGDATA")
    content = _read(files, "chart.png")
    assert content == [{"file_ref": {"name": "chart.png"}}]
    assert files.read_calls == []  # binary is never downloaded SDK-side


def test_non_utf8_text_falls_back_to_file_ref() -> None:
    content = _read(_FakeFiles(b"\xff\xfe\x00bin"), "weird.txt")
    assert content == [{"file_ref": {"name": "weird.txt"}}]


def test_oversize_text_is_refused() -> None:
    huge = b"a" * (ft._MAX_TEXT_READ_BYTES + 1)
    content = _read(_FakeFiles(huge), "huge.txt")
    assert "too large to read as text" in content["error"]


def test_not_found_surfaces_error() -> None:
    content = _read(_FakeFiles(missing=("ghost.txt",)), "ghost.txt")
    assert content == {"error": "not_found"}


def test_missing_name_is_soft_error() -> None:
    content = asyncio.run(
        dispatch_file_tool(_FakeFiles(), ToolCall(id="1", name="read_file", args={}))
    ).content
    assert content == {"error": "read_file requires a 'name'"}


@pytest.mark.parametrize("name", ["notes.md", "data.json", "log.csv"])
def test_text_extensions_windowed(name: str) -> None:
    content = _read(_FakeFiles(b"abc"), name)
    assert content == f"File: {name} (characters 0–3 of 3)\n\nabc"
