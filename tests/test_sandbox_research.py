"""sandbox bash (real subprocess in a temp workspace) + research web tools
(stubbed fetch / peer). No router."""

from __future__ import annotations

import asyncio

from bp_agents.agents.research.web import html_fetch, make_web_tools, web_search
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


def test_html_fetch_routes_to_md_converter() -> None:
    async def _drive() -> None:
        peers = _StubPeers("# Page\n\nbody")
        ctx = _Ctx(peers=peers)
        out = await html_fetch(
            ctx, url="http://x", raw=False, settings=SuiteSettings()
        )
        assert "Page" in out
        # Routed to md_converter.webpage.
        dest, _payload, mode = peers.spawns[0]
        assert dest == "md_converter" and mode == "webpage"

    asyncio.run(_drive())


def test_make_web_tools_names() -> None:
    tools = make_web_tools(SuiteSettings())
    assert {t.spec.name for t in tools} == {"web_search", "html_fetch", "web_download"}
