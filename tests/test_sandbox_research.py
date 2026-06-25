"""sandbox bash (real subprocess in a temp workspace) + research web tools
(stubbed fetch / peer). No router."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from bp_agents.agents.research.web import (
    BRAVE_CONTEXT_URL,
    EXA_CONTENTS_URL,
    EXA_SEARCH_URL,
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


class _StubLlm:
    """Records prompts and echoes a canned distillation reply. `embed` returns
    deterministic keyword-count vectors ('cat'/'dog' axes) so content ranking
    is assertable without a real embedding model."""

    def __init__(self, reply="DISTILLED") -> None:
        self._reply = reply
        self.calls: list[tuple] = []
        self.embed_calls: list[tuple] = []

    async def generate(self, messages, *, preset=None, **kw):
        self.calls.append((messages, preset))
        return SimpleNamespace(text=self._reply)

    @staticmethod
    def _vec(text):
        t = text.lower()
        return [float(t.count("cat")), float(t.count("dog")), 0.1]

    async def embed(self, texts, *, preset=None, **kw):
        self.embed_calls.append((texts, preset))
        return [self._vec(t) for t in texts]


class _Ctx:
    def __init__(self, *, user_id="usr_a", files=None, peers=None, llm=None) -> None:
        self.user_id = user_id
        self.files = files
        self.peers = peers
        self.llm = llm


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


def test_stash_to_workspace_tells_model_the_relative_path(tmp_path) -> None:
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
        # stash_to_workspace reads module-level _settings; patch it.
        orig = sb._settings
        sb._settings = settings
        try:
            out = await sb.stash_to_workspace(
                ctx, sb.StashToWorkspace(name="example.py")
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

        # stash_to_workspace's write path: root can't write here directly,
        # but the helper drops to the uid and succeeds.
        _write_bytes_as_uid(ws / "sample.py", b"print(1)\n", uid)
        assert (ws / "sample.py").exists()
        assert os.stat(ws / "sample.py").st_uid == uid

        # workspace_to_stash's read path round-trips the bytes back.
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


def test_html_fetch_dedups_repeated_urls() -> None:
    # The model passing the same URL twice must fetch it once.
    async def _drive() -> None:
        peers = _StubPeers("body")
        ctx = _Ctx(peers=peers)
        out = await html_fetch(
            ctx, urls=["http://a", "http://a", ""], raw=False, settings=SuiteSettings()
        )
        assert len(peers.spawns) == 1
        # Single distinct URL → no per-URL header (original single-URL shape).
        assert "## http://a" not in out and out == "body"

    asyncio.run(_drive())


def test_html_fetch_extract_query_distills_each_page() -> None:
    async def _drive() -> None:
        peers = _StubPeers("a very long page body about many things")
        llm = _StubLlm(reply="just the relevant facts")
        ctx = _Ctx(peers=peers, llm=llm)
        out = await html_fetch(
            ctx, urls=["http://a"], extract_query="what is X?",
            lite_preset="lite-x", settings=SuiteSettings(),
        )
        # Distilled (not raw) content, headered for source attribution.
        assert "just the relevant facts" in out and "## http://a" in out
        # The distiller ran on the resolved lite preset, and the query rode
        # along in the user message.
        assert llm.calls and llm.calls[0][1] == "lite-x"
        assert "what is X?" in llm.calls[0][0][1].content

    asyncio.run(_drive())


def test_html_fetch_extract_query_ignored_for_raw() -> None:
    # raw=true bypasses md_converter and distillation (no llm call).
    async def _drive() -> None:
        llm = _StubLlm()

        async def _get_bytes(url, timeout, cap):
            return b"<html>raw</html>"

        ctx = _Ctx(llm=llm)
        out = await html_fetch(
            ctx, urls=["http://a"], raw=True, extract_query="q",
            settings=SuiteSettings(), get_bytes=_get_bytes,
        )
        assert "raw" in out and not llm.calls

    asyncio.run(_drive())


def test_make_web_tools_names() -> None:
    tools = make_web_tools(SuiteSettings())
    assert {t.spec.name for t in tools} == {"web_search", "html_fetch", "web_download"}


def test_html_fetch_tool_exposes_extract_query() -> None:
    fetch = {t.spec.name: t for t in make_web_tools(SuiteSettings())}["html_fetch"]
    assert "extract_query" in fetch.spec.parameters["properties"]


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
        # `format` must be "json" — "markdown" returns a bare body that breaks
        # JSON decoding.
        assert captured["json"]["format"] == "json"
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
    # All backends advertise a unified max count of 20.
    assert props["count"]["maximum"] == 20
    # html_fetch always takes a list of urls.
    assert brave_tools["html_fetch"].spec.parameters["properties"]["urls"]["type"] == "array"

    searxng_tools = {
        t.spec.name: t for t in make_web_tools(SuiteSettings(searxng_url="http://x"))
    }
    assert searxng_tools["web_search"].spec.parameters["properties"]["count"]["maximum"] == 20

    kagi_tools = {
        t.spec.name: t
        for t in make_web_tools(
            SuiteSettings(web_search_backend="kagi", kagi_api_key="tok")
        )
    }
    kagi_props = kagi_tools["web_search"].spec.parameters["properties"]
    assert {"query", "kind", "count", "region", "file_type", "time_relative",
            "time_after", "time_before"} <= set(kagi_props)
    assert kagi_props["count"]["maximum"] == 20


# --------------------------------------------------------------------------- #
# deep SearXNG content ranking
# --------------------------------------------------------------------------- #

from bp_agents.agents.research.deepsearch import (  # noqa: E402
    _cosine,
    _page_score,
    deep_searxng_search,
    snippets_too_thin,
)
from bp_agents.common.htmltext import html_to_text  # noqa: E402


def test_html_to_text_strips_scripts_styles_and_unescapes() -> None:
    out = html_to_text(
        "<style>a{color:red}</style><h1>Title</h1>"
        "<script>evil()</script><p>a&amp;b</p>"
    )
    assert "Title" in out and "a&b" in out
    assert "evil()" not in out and "color:red" not in out


def test_cosine_and_page_score_math() -> None:
    assert round(_cosine([1.0, 0.0], [1.0, 0.0]), 3) == 1.0
    assert round(_cosine([1.0, 0.0], [0.0, 1.0]), 3) == 0.0
    # Top-2 squared: 0.9² + 0.8² = 1.45 (the 0.1 chunk is dropped).
    assert round(_page_score([0.9, 0.1, 0.8], top_n=2), 3) == 1.45


def test_snippets_too_thin_heuristic() -> None:
    s = SuiteSettings()  # min 80 chars, fraction 0.5
    assert snippets_too_thin([{"content": "x"}, {"content": "y"}], s) is True
    assert snippets_too_thin([{"content": "z" * 100}, {"content": "w" * 100}], s) is False
    assert snippets_too_thin([], s) is False


def test_deep_search_ranks_by_fetched_content() -> None:
    # The dog page has the richer snippet, but the cat page's *content* matches
    # the query — content ranking must surface it first.
    async def _drive() -> None:
        rows = [
            {"title": "Dogs", "url": "http://dog", "content": "a long rich snippet here"},
            {"title": "Cats", "url": "http://cat", "content": "x"},
        ]
        pages = {
            "http://dog": b"<p>dog dog dog puppies kennel</p>",
            "http://cat": b"<p>cat cat cat felines kitten</p>",
        }

        async def _get_bytes(url):
            return pages[url]

        llm = _StubLlm()
        out = await deep_searxng_search(
            _Ctx(llm=llm), "cat", rows=rows, count=2,
            settings=SuiteSettings(), embedding_preset="emb", get_bytes=_get_bytes,
        )
        assert "Content-ranked" in out
        assert out.index("http://cat") < out.index("http://dog")
        # Embedded on the embedding preset.
        assert llm.embed_calls and llm.embed_calls[0][1] == "emb"

    asyncio.run(_drive())


def test_deep_search_unfetchable_falls_back_to_flagged_snippet() -> None:
    async def _drive() -> None:
        rows = [{"title": "Cats", "url": "http://cat", "content": "cat cat snippet"}]

        async def _get_bytes(url):
            raise RuntimeError("boom")

        out = await deep_searxng_search(
            _Ctx(llm=_StubLlm()), "cat", rows=rows, count=1,
            settings=SuiteSettings(), embedding_preset="emb", get_bytes=_get_bytes,
        )
        assert "http://cat" in out and "unverified" in out

    asyncio.run(_drive())


def test_deep_search_empty_rows() -> None:
    async def _drive() -> None:
        out = await deep_searxng_search(
            _Ctx(llm=_StubLlm()), "q", rows=[], count=3,
            settings=SuiteSettings(), embedding_preset="emb",
        )
        assert "No results" in out

    asyncio.run(_drive())


def test_deep_policy_model_exposes_deep_tool_on_searxng() -> None:
    names = {
        t.spec.name
        for t in make_web_tools(
            SuiteSettings(searxng_url="http://x", web_search_deep="model")
        )
    }
    assert "deep_web_search" in names


def test_deep_policy_model_no_deep_tool_on_brave() -> None:
    # Brave snippets are already strong — no deep pipeline, no extra tool.
    names = {
        t.spec.name
        for t in make_web_tools(
            SuiteSettings(
                web_search_backend="brave", brave_api_key="k", web_search_deep="model"
            )
        )
    }
    assert "deep_web_search" not in names


def test_deep_policy_auto_and_off_have_no_deep_tool() -> None:
    for policy in ("auto", "off"):
        names = {
            t.spec.name
            for t in make_web_tools(
                SuiteSettings(searxng_url="http://x", web_search_deep=policy)
            )
        }
        assert "deep_web_search" not in names


def test_web_search_handler_always_policy_content_ranks(monkeypatch) -> None:
    import bp_agents.agents.research.deepsearch as ds
    import bp_agents.agents.research.web as web

    async def _drive() -> None:
        async def _get_json(url, params, timeout):
            return {"results": [
                {"title": "Cats", "url": "http://cat", "content": "x"},
                {"title": "Dogs", "url": "http://dog", "content": "y"},
            ]}

        async def _fetch_bytes(url, *, settings):
            return b"cat cat cat" if "cat" in url else b"dog dog dog"

        monkeypatch.setattr(web, "_default_get_json", _get_json)
        monkeypatch.setattr(ds, "_default_fetch_bytes", _fetch_bytes)
        settings = SuiteSettings(
            searxng_url="http://x", web_search_deep="always"
        )
        search = {
            t.spec.name: t
            for t in make_web_tools(settings, embedding_preset="emb")
        }["web_search"]
        out = await search.handler(_Ctx(llm=_StubLlm()), {"query": "cat"})
        assert "Content-ranked" in out
        assert out.index("http://cat") < out.index("http://dog")

    asyncio.run(_drive())


# --------------------------------------------------------------------------- #
# exa backend (search + /contents fetch)
# --------------------------------------------------------------------------- #


def test_web_search_exa_backend() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return {"results": [
                {"title": "Cats", "url": "http://x", "highlights": ["meow", "purr"]},
            ]}

        out = await web_search(
            "cats",
            settings=SuiteSettings(
                web_search_backend="exa", exa_api_key="k", exa_search_type="fast"
            ),
            count=5, include_domains=["arxiv.org"], exclude_domains=["pinterest.com"],
            max_age_hours=24, request_json=_request,
        )
        assert captured["url"] == EXA_SEARCH_URL
        assert captured["headers"]["x-api-key"] == "k"
        # type comes from settings; content options nest under `contents`.
        assert captured["json"]["type"] == "fast"
        assert captured["json"]["numResults"] == 5
        assert captured["json"]["contents"] == {"highlights": True}
        assert captured["json"]["includeDomains"] == ["arxiv.org"]
        assert captured["json"]["excludeDomains"] == ["pinterest.com"]
        assert captured["json"]["maxAgeHours"] == 24
        # Highlights are the per-result snippet.
        assert "Cats" in out and "http://x" in out and "meow" in out and "purr" in out

    asyncio.run(_drive())


def test_web_search_exa_falls_back_when_key_missing() -> None:
    async def _drive() -> None:
        out = await web_search(
            "x", settings=SuiteSettings(web_search_backend="exa", searxng_url=None)
        )
        assert "not configured" in out

    asyncio.run(_drive())


def test_html_fetch_exa_contents_text() -> None:
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["url"] = url
            captured["json"] = json
            return {"results": [{"url": "http://a", "text": "the page body"}]}

        out = await html_fetch(
            None, urls=["http://a"], truncate=5000,
            settings=SuiteSettings(web_search_backend="exa", exa_api_key="k"),
            request_json=_request,
        )
        assert captured["url"] == EXA_CONTENTS_URL
        # On /contents, content options are TOP-LEVEL (not nested in `contents`).
        assert captured["json"]["text"] == {"maxCharacters": 5000}
        assert "summary" not in captured["json"]
        assert "## http://a" in out and "the page body" in out

    asyncio.run(_drive())


def test_html_fetch_exa_extract_query_uses_native_summary() -> None:
    # extract_query on Exa → request a query-focused summary server-side; our
    # own lite-preset distillation must NOT run.
    async def _drive() -> None:
        captured: dict = {}

        async def _request(method, url, *, params=None, json=None, headers=None, timeout):
            captured["json"] = json
            return {"results": [{"url": "http://a", "summary": "just the answer"}]}

        llm = _StubLlm()
        out = await html_fetch(
            _Ctx(llm=llm), urls=["http://a"], extract_query="what is X?",
            settings=SuiteSettings(web_search_backend="exa", exa_api_key="k"),
            request_json=_request,
        )
        assert captured["json"]["summary"] == {"query": "what is X?"}
        assert "text" not in captured["json"]
        assert "just the answer" in out
        # Exa distilled server-side — no extra LLM round-trip on our side.
        assert llm.calls == []

    asyncio.run(_drive())


def test_exa_search_tool_schema() -> None:
    tools = {
        t.spec.name: t
        for t in make_web_tools(SuiteSettings(web_search_backend="exa", exa_api_key="k"))
    }
    props = tools["web_search"].spec.parameters["properties"]
    assert {"query", "count", "include_domains", "exclude_domains", "max_age_hours"} <= set(props)
    assert props["count"]["maximum"] == 20
    # Exa has no deep_web_search tool (that's SearXNG-only).
    assert "deep_web_search" not in tools
