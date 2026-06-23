"""md_converter agent — convert (real MarkItDown on a temp file) +
webpage (stubbed fetch). No router."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bp_agents.agents.md_converter import Convert, Webpage, run_convert, run_webpage
from bp_agents.agents.md_converter.agent import _html_to_text


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


def test_webpage_fetch_error_is_handled_gracefully() -> None:
    """A fetch failure (403, read timeout, …) returns a clean message instead
    of raising an unhandled exception out of the handler."""
    import httpx

    async def _drive() -> None:
        ctx = _Ctx(None)

        req = httpx.Request("GET", "http://blocked.example")
        resp = httpx.Response(403, request=req)

        async def _fetch_403(url: str) -> bytes:
            raise httpx.HTTPStatusError("forbidden", request=req, response=resp)

        out = await run_webpage(
            ctx, Webpage(url="http://blocked.example"), fetch=_fetch_403
        )
        assert "Couldn't fetch" in out.content
        assert "HTTP 403" in out.content

        async def _fetch_timeout(url: str) -> bytes:
            raise httpx.ReadTimeout("slow")

        out2 = await run_webpage(
            ctx, Webpage(url="http://slow.example"), fetch=_fetch_timeout
        )
        assert "Couldn't fetch" in out2.content
        assert "timed out" in out2.content

    asyncio.run(_drive())


def test_html_to_text_regex_fallback() -> None:
    # The fallback used when MarkItDown can't parse a page.
    raw = (
        "<html><body><h2>Title</h2><p>Hello <b>world</b></p>"
        "<ul><li>one</li><li>two</li></ul></body></html>"
    )
    out = _html_to_text(raw)
    assert "## Title" in out
    assert "Hello world" in out
    assert "- one" in out and "- two" in out


def test_ocr_disabled_by_default() -> None:
    """With no SUITE_MD_OCR_* config, no OCR client is built (the `openai`
    import never runs) and MarkItDown is constructed plain — behaviour is
    byte-for-byte what it was before the OCR feature."""
    import sys
    import types

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    assert mod._ocr_llm_client() is None

    md_kwargs: dict = {}

    class _FakeMarkItDown:
        def __init__(self, **kwargs) -> None:
            md_kwargs.update(kwargs)

    fake_md = types.ModuleType("markitdown")
    fake_md.MarkItDown = _FakeMarkItDown
    saved = sys.modules.get("markitdown")
    sys.modules["markitdown"] = fake_md
    try:
        mod._make_markitdown()
    finally:
        if saved is not None:
            sys.modules["markitdown"] = saved
        else:
            del sys.modules["markitdown"]
    assert md_kwargs == {}  # no enable_plugins / llm_client / llm_model


def test_ocr_configured_builds_plugin_client(monkeypatch) -> None:
    """KEY + MODEL set ⇒ an OpenAI-compatible client is built from the
    dedicated OCR creds and handed to MarkItDown with enable_plugins +
    llm_model (+ llm_prompt when set)."""
    import sys
    import types

    from pydantic import SecretStr

    mod = sys.modules["bp_agents.agents.md_converter.agent"]

    monkeypatch.setattr(mod._settings, "md_ocr_api_key", SecretStr("sk-test"))
    monkeypatch.setattr(mod._settings, "md_ocr_model", "gpt-4o")
    monkeypatch.setattr(mod._settings, "md_ocr_base_url", "https://oai.example/v1")
    monkeypatch.setattr(mod._settings, "md_ocr_prompt", "Extract text.")
    mod._ocr_client_box.clear()

    client_kwargs: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            client_kwargs.update(kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    md_kwargs: dict = {}

    class _FakeMarkItDown:
        def __init__(self, **kwargs) -> None:
            md_kwargs.update(kwargs)

    fake_md = types.ModuleType("markitdown")
    fake_md.MarkItDown = _FakeMarkItDown
    monkeypatch.setitem(sys.modules, "markitdown", fake_md)

    mod._make_markitdown()

    # Client built from the OCR-specific credentials (secret unwrapped).
    assert client_kwargs == {
        "api_key": "sk-test",
        "base_url": "https://oai.example/v1",
    }
    # Plugin enabled and pointed at the vision model + custom prompt.
    assert md_kwargs["enable_plugins"] is True
    assert md_kwargs["llm_model"] == "gpt-4o"
    assert md_kwargs["llm_prompt"] == "Extract text."
    assert isinstance(md_kwargs["llm_client"], _FakeOpenAI)

    # Cached: a second call reuses the same client, no rebuild.
    again = mod._ocr_llm_client()
    assert again is md_kwargs["llm_client"]


def test_ocr_key_without_model_stays_disabled(monkeypatch) -> None:
    """A key alone (no model) must NOT enable OCR — the gate requires both,
    so a half-configured deploy fails safe to the built-in converters."""
    import sys

    from pydantic import SecretStr

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    monkeypatch.setattr(mod._settings, "md_ocr_api_key", SecretStr("sk-test"))
    monkeypatch.setattr(mod._settings, "md_ocr_model", None)
    mod._ocr_client_box.clear()
    assert mod._ocr_llm_client() is None


def test_decode_html_detects_non_utf8_charset() -> None:
    """The fallback decode must honour the page's charset — a naive UTF-8
    decode mangles EUC-KR/CP949 Korean pages into replacement chars."""
    from bp_agents.agents.md_converter.agent import _decode_html

    kr = "안녕하세요 세계"
    euckr = f'<meta charset="euc-kr"><h1>{kr}</h1>'.encode("euc-kr")
    assert kr in _decode_html(euckr)  # not '�…' mojibake
    # UTF-8 still works.
    assert kr in _decode_html(f"<h1>{kr}</h1>".encode())


def test_webpage_fallback_decodes_non_utf8_page(monkeypatch) -> None:
    """End to end: markitdown fails → the regex fallback still yields correct
    Korean from an EUC-KR page (previously came out as `�…`)."""
    import sys

    mod = sys.modules["bp_agents.agents.md_converter.agent"]

    def _boom(*a, **k):
        raise RuntimeError("markitdown unavailable")

    monkeypatch.setattr(mod, "_markitdown_bytes", _boom)

    async def _drive() -> str:
        kr = "안녕하세요 세계"
        euckr = (
            f'<html><head><meta charset="euc-kr"></head>'
            f"<body><h1>{kr}</h1></body></html>"
        ).encode("euc-kr")

        async def _fetch(url: str) -> bytes:
            return euckr

        out = await run_webpage(
            _Ctx(None), Webpage(url="http://x", truncate=2000), fetch=_fetch
        )
        return out.content

    assert "안녕하세요 세계" in asyncio.run(_drive())
