"""md_converter agent — convert (real MarkItDown on a temp file) +
webpage (stubbed fetch). No router."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bp_agents.agents.md_converter import Convert, Webpage, run_convert, run_webpage


class _StubFiles:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.written: dict[str, str] = {}

    async def read(self, name: str) -> Path:
        return self._path

    async def write(self, filename: str, text: str) -> str:
        self.written[filename] = text
        return filename


class _Ctx:
    def __init__(self, files) -> None:
        self.files = files


def test_convert_content(tmp_path) -> None:
    async def _drive() -> None:
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text.")
        ctx = _Ctx(_StubFiles(doc))
        out = await run_convert(ctx, Convert(name="doc.md", output_type="content"))
        assert "Heading" in out.content
        assert "body text" in out.content

    asyncio.run(_drive())


def test_convert_auto_large_goes_to_file(tmp_path) -> None:
    async def _drive() -> None:
        doc = tmp_path / "big.md"
        doc.write_text("word " * 1000)  # > 2000 chars → auto picks file
        files = _StubFiles(doc)
        ctx = _Ctx(files)
        out = await run_convert(ctx, Convert(name="big.md", output_type="auto"))
        assert out.files == ["big.md"]
        assert "big.md" in files.written

    asyncio.run(_drive())


def test_webpage_with_stub_fetch() -> None:
    async def _drive() -> None:
        async def _fetch(url: str) -> bytes:
            return b"<html><body><h1>Hello</h1><p>world page</p></body></html>"

        ctx = _Ctx(None)
        out = await run_webpage(
            ctx, Webpage(url="http://example.com", truncate=1000), fetch=_fetch
        )
        assert "Hello" in out.content
        assert "world page" in out.content

    asyncio.run(_drive())
