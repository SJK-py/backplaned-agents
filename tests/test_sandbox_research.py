"""sandbox bash (real subprocess in a temp workspace) + research web tools
(stubbed fetch / peer). No router."""

from __future__ import annotations

import asyncio
import os

import pytest

from bp_agents.agents.research.web import (
    BRAVE_CONTEXT_URL,
    KAGI_EXTRACT_URL,
    KAGI_SEARCH_URL,
    html_fetch,
    make_web_tools,
    web_search,
)
from bp_agents.agents.sandbox import Bash, run_bash
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


class _StubFiles:
    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []
        self.stored: list[bytes] = []

    async def write(self, filename: str, text: str) -> str:
        self.written.append((filename, text))
        return filename

    async def store(self, data, *, filename=None, **kw) -> str:
        self.stored.append(data)
        return filename or "download"


class _StubPeers:
    def __init__(self, content: str) -> None:
        self._content = content
        self.spawns: list[tuple] = []

    async def spawn(self, dest, payload, *, mode=None, **kw):
        self.spawns.append((dest, payload, mode))
        return ResultFrame(
            agent_id=dest, trace_id="0" * 32, span_id="0" * 16, task_id="t",
            status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=self._content),
        )


class _Ctx:
    def __init__(self, *, user_id="usr_a", files=None, peers=None) -> None:
        self.user_id = user_id
        self.files = files
        self.peers = peers


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #


def test_sandbox_bash_runs_in_workspace(tmp_path) -> None:
    async def _drive() -> None:
        settings = SuiteSettings(sandbox_root=str(tmp_path))
        ctx = _Ctx(files=_StubFiles())
        out = await run_bash(ctx, Bash(command="echo hello && pwd"), settings=settings)
        assert "hello" in out.content
        # Ran inside the per-user workspace.
        assert "usr_a" in out.content

    asyncio.run(_drive())


def test_sandbox_bash_large_output_saved(tmp_path) -> None:
    async def _drive() -> None:
        settings = SuiteSettings(sandbox_root=str(tmp_path), sandbox_max_inline_output=50)
        files = _StubFiles()
        ctx = _Ctx(files=files)
        out = await run_bash(
            ctx, Bash(command="for i in $(seq 1 200); do echo line $i; done"),
            settings=settings,
        )
        assert out.files == ["bash_output.txt"]
        assert files.written and "truncated" in out.content

    asyncio.run(_drive())


def test_sandbox_bash_empty_output_explains_success(tmp_path) -> None:
    # A command that prints nothing must not return a blank reply — the model
    # would misread it as failure. Explain it succeeded + how to inspect.
    async def _drive() -> None:
        settings = SuiteSettings(sandbox_root=str(tmp_path))
        out = await run_bash(ctx=_Ctx(files=_StubFiles()),
                             payload=Bash(command="true"), settings=settings)
        assert out.content.strip()
        assert "no output" in out.content and "ls" in out.content

    asyncio.run(_drive())


def test_storage_to_workspace_tells_model_the_relative_path(tmp_path) -> None:
    # The return must point the model at the cwd-relative name, since bash runs
    # in the workspace where the file lands.
    import importlib
    from pathlib import Path

    # The submodule (the package __init__ rebinds the name `agent` to the Agent
    # instance, so a plain `import ...agent as sb` would grab that, not module).
    sb = importlib.import_module("bp_agents.agents.sandbox.agent")

    async def _drive() -> None:
        src = tmp_path / "example.py"
        src.write_text("print('hi')")

        class _Files:
            async def read(self, name):  # noqa: ANN001
                return Path(src)
        ctx = _Ctx(files=_Files())
        settings = SuiteSettings(sandbox_root=str(tmp_path / "ws"))
        # storage_to_workspace reads module-level _settings; patch it.
        orig = sb._settings
        sb._settings = settings
        try:
            out = await sb.storage_to_workspace(
                ctx, sb.StorageToWorkspace(name="example.py")
            )
        finally:
            sb._settings = orig
        assert "example.py" in out.content
        # Names the relative form and explains bash's cwd.
        assert "./example.py" in out.content or "'example.py'" in out.content
        assert "bash" in out.content.lower()

    asyncio.run(_drive())


@pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root to exercise the uid-drop workspace I/O"
)
def test_workspace_io_works_when_dir_is_uid_owned() -> None:
    """Regression: bash chowns the workspace to the per-user uid; the
    stash<->workspace copies then run as ROOT with no CAP_DAC_OVERRIDE, so a
    direct write/read into the uid-owned dir EACCES'd. The fork+drop helpers
    must do the I/O as the uid instead. Verify the full round-trip with a real
    uid-owned workspace."""
    import shutil
    import uuid
    from pathlib import Path

    from bp_agents.agents.sandbox.agent import (
        _ensure_workspace,
        _read_bytes_as_uid,
        _write_bytes_as_uid,
    )

    uid = 2000
    # NOT pytest's tmp_path / mkdtemp: those sit under a 0o700 root-owned
    # intermediate dir, so the dropped uid can't traverse INTO the workspace.
    # Build directly under /tmp (0o777, world-traversable), mirroring the real
    # /home/<user> layout where the parent chain is reachable by the uid.
    root = Path("/tmp") / f"sbx_io_{uuid.uuid4().hex}"
    root.mkdir()
    os.chmod(root, 0o755)
    ws = root / "usr_test"
    try:
        # ensure_workspace hands the dir to the uid (as bash does).
        _ensure_workspace(ws, uid)
        assert os.stat(ws).st_uid == uid

        # storage_to_workspace's write path: root can't write here directly,
        # but the helper drops to the uid and succeeds.
        _write_bytes_as_uid(ws / "sample.py", b"print(1)\n", uid)
        assert (ws / "sample.py").exists()
        assert os.stat(ws / "sample.py").st_uid == uid

        # workspace_to_storage's read path round-trips the bytes back.
        assert _read_bytes_as_uid(ws / "sample.py", uid) == b"print(1)\n"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# research web tools
# --------------------------------------------------------------------------- #


def test_web_search_unconfigured() -> None:
    async def _drive() -> None:
        out = await web_search("anything", settings=SuiteSettings(searxng_url=None))
        assert "not configured" in out

    asyncio.run(_drive())


def test_web_search_formats_results() -> None:
    async def _drive() -> None:
        async def _get_json(url, params, timeout):
            assert params["q"] == "cats"
            return {"results": [
                {"title": "Cats", "url": "http://x", "content": "about cats"},
            ]}

        out = await web_search(
            "cats", settings=SuiteSettings(searxng_url="http://searx"),
            get_json=_get_json,
        )
        assert "Cats" in out and "http://x" in out

    asyncio.run(_drive())


def test_web_search_count_and_filters() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _get_json(url, params, timeout):
            captured["params"] = params
            return {"results": [
                {"title": f"t{i}", "url": "u", "content": "c"} for i in range(10)
            ]}

        out = await web_search(
            "cats", settings=SuiteSettings(searxng_url="http://s"),
            count=3, time_range="week", language="en", get_json=_get_json,
        )
        # SearXNG filter params forwarded; results capped at `count`.
        assert captured["params"]["time_range"] == "week"
        assert captured["params"]["language"] == "en"
        assert "3. t2" in out and "4. t3" not in out

    asyncio.run(_drive())


def test_html_fetch_routes_to_md_converter() -> None:
    async def _drive() -> None:
        peers = _StubPeers("# Page\n\nbody")
        ctx = _Ctx(peers=peers)
        out = await html_fetch(
            ctx, urls=["http://x"], raw=False, settings=SuiteSettings()
        )
        assert "Page" in out
        # Routed to md_converter.webpage.
        dest, _payload, mode = peers.spawns[0]
        assert dest == "md_converter" and mode == "webpage"

    asyncio.run(_drive())


def test_html_fetch_multiple_urls_each_headed() -> None:
    async def _drive() -> None:
        peers = _StubPeers("body")
        ctx = _Ctx(peers=peers)
        out = await html_fetch(
            ctx, urls=["http://a", "http://b"], raw=False, settings=SuiteSettings()
        )
        # Each URL gets its own header + spawn when more than one is given.
        assert "## http://a" in out and "## http://b" in out
        assert len(peers.spawns) == 2

    asyncio.run(_drive())


def test_make_web_tools_names() -> None:
    tools = make_web_tools(SuiteSettings())
    assert {t.spec.name for t in tools} == {"web_search", "html_fetch", "web_download"}


def test_web_search_falls_back_to_searxng_when_key_missing() -> None:
    # backend=brave but no key → fall back to SearXNG (here unconfigured).
    async def _drive() -> None:
        out = await web_search(
            "x", settings=SuiteSettings(web_search_backend="brave", searxng_url=None)
        )
        assert "not configured" in out

    asyncio.run(_drive())


def test_web_search_brave_backend() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["method"] = method
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return {"grounding": {"generic": [
                {"title": "Cats", "url": "http://x", "snippets": ["meow", "purr"]},
            ]}}

        out = await web_search(
            "cats",
            settings=SuiteSettings(web_search_backend="brave", brave_api_key="k"),
            country="kr", search_language="ko", freshness="pw",
            local_city="Seoul", request_json=_request,
        )
        assert captured["url"] == BRAVE_CONTEXT_URL
        assert captured["params"]["country"] == "kr"
        assert captured["params"]["search_lang"] == "ko"
        assert captured["params"]["freshness"] == "pw"
        assert captured["headers"]["X-Subscription-Token"] == "k"
        assert captured["headers"]["X-Loc-City"] == "Seoul"
        assert "Cats" in out and "http://x" in out and "meow" in out

    asyncio.run(_drive())


def test_web_search_kagi_backend() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return {"data": {
                "direct_answer": [{"title": "42", "url": "http://a"}],
                "search": [
                    {
                        "url": "http://x",
                        "title": "Cats &amp; Dogs",
                        "snippet": "meow &quot;purr&quot;",
                        "props": {"language": "en"},
                    },
                ],
                "related_search": [{"title": "kittens", "url": "http://k"}],
                "interesting_finds": [{"title": "drop me", "url": "http://d"}],
                "web_archive": [{"title": "drop me too", "url": "http://w"}],
            }}

        out = await web_search(
            "cats", count=9,
            settings=SuiteSettings(web_search_backend="kagi", kagi_api_key="tok"),
            region="kr", file_type="pdf", time_relative="week",
            request_json=_request,
        )
        assert captured["url"] == KAGI_SEARCH_URL
        assert captured["json"]["query"] == "cats"
        assert captured["json"]["workflow"] == "search"
        assert captured["json"]["limit"] == 9
        assert captured["json"]["lens"] == {
            "file_type": "pdf", "time_relative": "week", "search_region": "kr",
        }
        assert captured["headers"]["Authorization"] == "Bearer tok"
        # `.props` dropped; HTML entities unescaped.
        assert "language" not in out
        assert "Cats & Dogs" in out and 'meow "purr"' in out and "http://x" in out
        # direct_answer is a header (appears before the primary search list).
        assert out.index("Direct answer") < out.index("Search results")
        # related_search is a footer; dropped collections never appear.
        assert "Related searches" in out
        assert "drop me" not in out

    asyncio.run(_drive())


def test_web_search_kagi_snippet_cap_and_aux_limit() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["json"] = json
            return {"data": {
                "search": [{"url": "http://x", "title": "T", "snippet": "z" * 900}],
                "related_search": [
                    {"url": f"http://r{i}", "title": f"q{i}"} for i in range(10)
                ],
            }}

        out = await web_search(
            "cats", count=6,
            settings=SuiteSettings(web_search_backend="kagi", kagi_api_key="tok"),
            request_json=_request,
        )
        # No lens when no restrictive options were passed.
        assert "lens" not in captured["json"]
        # Snippet capped at 500 chars.
        assert "z" * 500 in out and "z" * 501 not in out
        # Aux collections capped at count // 3 == 2.
        assert "q0" in out and "q1" in out and "q2" not in out

    asyncio.run(_drive())


def test_html_fetch_kagi_extract_backend() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return {"data": [
                {"url": "http://a", "markdown": "# Page A"},
                {"url": "http://b", "markdown": None, "error": "timeout"},
            ]}

        out = await html_fetch(
            None, urls=["http://a", "http://b"],
            settings=SuiteSettings(web_search_backend="kagi", kagi_api_key="tok"),
            request_json=_request,
        )
        assert captured["url"] == KAGI_EXTRACT_URL
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["json"]["pages"] == [{"url": "http://a"}, {"url": "http://b"}]
        assert "Page A" in out and "Couldn't extract: timeout" in out

    asyncio.run(_drive())


def test_web_search_tool_schema_reflects_backend() -> None:
    brave_tools = {
        t.spec.name: t
        for t in make_web_tools(
            SuiteSettings(web_search_backend="brave", brave_api_key="k")
        )
    }
    props = brave_tools["web_search"].spec.parameters["properties"]
    assert {"query", "country", "search_language", "freshness", "local_city"} <= set(props)
    # html_fetch always takes a list of urls.
    assert brave_tools["html_fetch"].spec.parameters["properties"]["urls"]["type"] == "array"

    kagi_tools = {
        t.spec.name: t
        for t in make_web_tools(
            SuiteSettings(web_search_backend="kagi", kagi_api_key="tok")
        )
    }
    kagi_props = kagi_tools["web_search"].spec.parameters["properties"]
    assert {"query", "kind", "count", "region", "file_type", "time_relative",
            "time_after", "time_before"} <= set(kagi_props)
