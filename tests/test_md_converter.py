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

    # OCR is opt-in: even fully configured, ocr=False (the default) stays plain
    # — no client built, no plugins. This is the fix for "OCR runs every time".
    mod._make_markitdown(ocr=False)
    assert client_kwargs == {}
    assert md_kwargs == {}

    # ocr=True engages the plugin with the bounded OCR client.
    mod._make_markitdown(ocr=True)

    # Client built from the OCR-specific credentials (secret unwrapped) and
    # bounded so a slow OCR endpoint can't hang the conversion (SDK defaults
    # are 600s × 2 retries).
    assert client_kwargs == {
        "api_key": "sk-test",
        "base_url": "https://oai.example/v1",
        "timeout": 60.0,
        "max_retries": 1,
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


def test_convert_forwards_ocr_flag(monkeypatch, tmp_path) -> None:
    """run_convert threads the request's `ocr` flag to the isolated worker, and
    defaults to OFF — OCR runs only when the caller asks, not on every call."""
    import sys

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    captured: dict = {}

    async def _fake_isolated(in_path: str, *, ocr: bool = False) -> str:
        captured["ocr"] = ocr
        return "# converted"

    monkeypatch.setattr(mod, "_convert_isolated", _fake_isolated)
    doc = tmp_path / "f.pdf"
    doc.write_text("x")

    def _run(payload: Convert) -> bool:
        async def _drive() -> bool:
            await run_convert(_Ctx(_StubFiles(doc)), payload)
            return captured["ocr"]

        return asyncio.run(_drive())

    assert _run(Convert(name="f.pdf", output_type="content", ocr=True)) is True
    assert _run(Convert(name="f.pdf", output_type="content", ocr=False)) is False
    # Default is opt-out: a request that omits `ocr` does NOT OCR.
    assert _run(Convert(name="f.pdf", output_type="content")) is False


def test_datalab_backend_gating(monkeypatch) -> None:
    """Datalab routing requires md_backend=datalab AND a key; a missing key
    falls back to markitdown (mirrors web_search backend selection)."""
    import sys

    from pydantic import SecretStr

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    monkeypatch.setattr(mod._settings, "md_backend", "markitdown")
    assert mod._datalab_backend_active() is False

    monkeypatch.setattr(mod._settings, "md_backend", "datalab")
    monkeypatch.setattr(mod._settings, "datalab_api_key", None)
    assert mod._datalab_backend_active() is False  # no key → fall back

    monkeypatch.setattr(mod._settings, "datalab_api_key", SecretStr("k"))
    assert mod._datalab_backend_active() is True


def test_convert_datalab_submit_poll_complete(monkeypatch, tmp_path) -> None:
    """Datalab path: submit the file, poll request_check_url until complete,
    return the markdown. Uses httpx MockTransport — no network."""
    import sys

    import httpx
    from pydantic import SecretStr

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    monkeypatch.setattr(mod._settings, "datalab_api_key", SecretStr("secret-k"))
    monkeypatch.setattr(mod, "_DATALAB_POLL_INTERVAL_S", 0.0)  # don't sleep in tests

    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.headers["X-API-Key"] == "secret-k"
            return httpx.Response(200, json={
                "success": True,
                "request_check_url": "https://www.datalab.to/api/v1/convert/REQ",
            })
        state["polls"] += 1
        if state["polls"] == 1:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(200, json={
            "status": "complete", "success": True, "markdown": "# Hello Datalab",
        })

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 test")

    async def _drive() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await mod._convert_datalab(str(f), filename="doc.pdf", client=client)

    assert asyncio.run(_drive()) == "# Hello Datalab"
    assert state["polls"] == 2  # processing, then complete


def test_convert_datalab_failure_raises_upstream(monkeypatch, tmp_path) -> None:
    """A failed Datalab job surfaces as UpstreamError (→ failed result the
    model can relay), not a silent empty conversion."""
    import sys

    import httpx
    from pydantic import SecretStr

    from bp_sdk.errors import UpstreamError

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    monkeypatch.setattr(mod._settings, "datalab_api_key", SecretStr("k"))
    monkeypatch.setattr(mod, "_DATALAB_POLL_INTERVAL_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={
                "success": True,
                "request_check_url": "https://www.datalab.to/api/v1/convert/REQ",
            })
        return httpx.Response(200, json={"status": "failed", "error": "corrupt pdf"})

    f = tmp_path / "d.pdf"
    f.write_bytes(b"x")

    async def _drive() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await mod._convert_datalab(str(f), filename="d.pdf", client=client)

    try:
        asyncio.run(_drive())
        raise AssertionError("expected UpstreamError")
    except UpstreamError as exc:
        assert "Datalab conversion failed" in str(exc)


def test_run_convert_routes_to_datalab(monkeypatch, tmp_path) -> None:
    """When the Datalab backend is active, run_convert routes there and does
    NOT spawn the MarkItDown worker."""
    import sys

    mod = sys.modules["bp_agents.agents.md_converter.agent"]
    monkeypatch.setattr(mod, "_datalab_backend_active", lambda: True)
    called: dict = {}

    async def _fake_dl(path: str, *, filename: str, client=None) -> str:
        called["datalab"] = filename
        return "# datalab markdown"

    async def _fake_iso(path: str, *, ocr: bool = False) -> str:
        called["isolated"] = True
        return "# nope"

    monkeypatch.setattr(mod, "_convert_datalab", _fake_dl)
    monkeypatch.setattr(mod, "_convert_isolated", _fake_iso)
    doc = tmp_path / "f.pdf"
    doc.write_text("x")

    async def _drive():
        return await run_convert(
            _Ctx(_StubFiles(doc)), Convert(name="f.pdf", output_type="content")
        )

    out = asyncio.run(_drive())
    assert "datalab markdown" in out.content
    assert called.get("datalab") == "f.pdf"
    assert "isolated" not in called  # MarkItDown worker NOT used


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


# ---------------------------------------------------------------------------
# Conversion isolation: a crash / hang / OOM in the worker must surface as a
# clean UpstreamError (→ failed result + log), never a silent agent death.
# The _worker_argv seam lets us swap the real worker for a fast stand-in.
# ---------------------------------------------------------------------------


def _fake_worker(monkeypatch, mod, script: str) -> None:
    """Point `_convert_isolated` at a throwaway `python -c <script>` instead of
    the real worker module. The script gets (in_path, out_path) as argv[1:3]."""
    import sys

    def _argv(in_path: str, out_path: str, ocr: bool) -> list:
        return [sys.executable, "-c", script, in_path, out_path]

    monkeypatch.setattr(mod, "_worker_argv", _argv)


def _agent_mod():
    import sys

    return sys.modules["bp_agents.agents.md_converter.agent"]


def test_convert_isolated_success(monkeypatch) -> None:
    """Happy path: the worker writes Markdown to out_path; the parent reads it
    back verbatim."""
    mod = _agent_mod()
    _fake_worker(
        monkeypatch, mod,
        "import sys; open(sys.argv[2], 'w', encoding='utf-8')"
        ".write('# converted ok')",
    )
    out = asyncio.run(mod._convert_isolated("/in/does-not-matter"))
    assert out == "# converted ok"


def test_convert_isolated_timeout_raises_upstream(monkeypatch) -> None:
    """A worker that overruns the budget is killed and surfaced as a clean,
    visible error — not a hang."""
    from bp_sdk.errors import UpstreamError

    mod = _agent_mod()
    monkeypatch.setattr(mod._settings, "md_convert_timeout_s", 0.2)
    _fake_worker(monkeypatch, mod, "import time; time.sleep(30)")

    async def _drive() -> None:
        await mod._convert_isolated("/in/x")

    try:
        asyncio.run(_drive())
        raise AssertionError("expected UpstreamError")
    except UpstreamError as exc:
        assert "timed out" in str(exc)


def test_convert_isolated_signal_death_raises_upstream(monkeypatch) -> None:
    """A worker killed by a signal (e.g. SIGKILL from the OOM-killer) → a
    negative return code → an OOM-flavoured UpstreamError, not silence."""
    from bp_sdk.errors import UpstreamError

    mod = _agent_mod()
    _fake_worker(
        monkeypatch, mod,
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    )

    async def _drive() -> None:
        await mod._convert_isolated("/in/x")

    try:
        asyncio.run(_drive())
        raise AssertionError("expected UpstreamError")
    except UpstreamError as exc:
        assert "out of memory or was killed" in str(exc)


def test_convert_isolated_nonzero_exit_raises_upstream(monkeypatch) -> None:
    """A worker that exits non-zero (a conversion error) → UpstreamError; the
    stderr reason is logged, not leaked onto the wire."""
    from bp_sdk.errors import UpstreamError

    mod = _agent_mod()
    _fake_worker(
        monkeypatch, mod,
        "import sys; print('ValueError: bad pdf', file=sys.stderr); sys.exit(3)",
    )

    async def _drive() -> None:
        await mod._convert_isolated("/in/x")

    try:
        asyncio.run(_drive())
        raise AssertionError("expected UpstreamError")
    except UpstreamError as exc:
        assert "could not convert" in str(exc)
        # The raw stderr reason must NOT appear in the user-facing message.
        assert "bad pdf" not in str(exc)
