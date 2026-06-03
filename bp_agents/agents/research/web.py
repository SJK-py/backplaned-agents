"""research.web — web tools (search / fetch / download).

Backend-agnostic: `web_search` targets a configurable Brave-API-compatible
endpoint (`SUITE_SEARXNG_URL`); `html_fetch` returns raw HTML or routes
the URL through `md_converter.webpage`; `web_download` saves a URL to the
file store by name. The core functions take an injectable fetcher so they
are testable without a network; `make_web_tools` wraps them as
`LocalTool`s closing over settings.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from bp_agents.common import LocalTool
from bp_agents.common.urlsafe import safe_stream_get
from bp_agents.settings import load_suite_settings
from bp_sdk import ToolSpec

if TYPE_CHECKING:
    from bp_agents.settings import SuiteSettings
    from bp_sdk import TaskContext

MD_CONVERTER_AGENT_ID = "md_converter"
_CONTENT_CAP = 100_000
# Two-stage timeout: a dead/unreachable host fails the CONNECT fast instead of
# burning the full `web_fetch_timeout_s` (the read stays generous so a large,
# legitimately-slow download still completes).
_CONNECT_TIMEOUT_S = 5.0

# Injectable fetchers (overridden in tests).
JsonGetter = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]
BytesGetter = Callable[[str, float, int], Awaitable[bytes]]


_settings = load_suite_settings()
_FETCH_HEADERS = {"User-Agent": _settings.web_fetch_user_agent}


async def _default_get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    t = httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout))
    async with httpx.AsyncClient(timeout=t, headers=_FETCH_HEADERS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _default_get_bytes(url: str, timeout: float, cap: int) -> bytes:
    # safe_stream_get re-validates each redirect hop against the SSRF guard.
    t = httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout))
    async with httpx.AsyncClient(timeout=t, headers=_FETCH_HEADERS) as client:
        return await safe_stream_get(
            client, url, cap=cap, max_redirects=_settings.web_fetch_max_redirects
        )


async def web_search(
    query: str, *, settings: SuiteSettings, count: int = 5,
    time_range: str | None = None, language: str | None = None,
    get_json: JsonGetter | None = None,
) -> str:
    if not settings.searxng_url:
        return "Web search is not configured (no search backend set)."
    count = min(max(count, 1), 10)
    get = get_json or _default_get_json
    params: dict[str, Any] = {"q": query, "format": "json"}
    if time_range:
        params["time_range"] = time_range
    if language:
        params["language"] = language
    data = await get(
        f"{settings.searxng_url.rstrip('/')}/search",
        params,
        settings.web_fetch_timeout_s,
    )
    results = (data.get("results") or [])[:count]
    if not results:
        return f"No results for {query!r}."
    return "\n\n".join(
        f"{i + 1}. {r.get('title', '')}\n{r.get('url', '')}\n{r.get('content', '')}"
        for i, r in enumerate(results)
    )


async def html_fetch(
    ctx: TaskContext, *, url: str, raw: bool = False, truncate: int = 2000,
    settings: SuiteSettings, get_bytes: BytesGetter | None = None,
) -> str:
    truncate = min(max(truncate, 0), _CONTENT_CAP)
    if raw:
        get = get_bytes or _default_get_bytes
        data = await get(url, settings.web_fetch_timeout_s, settings.web_fetch_max_bytes)
        return data.decode("utf-8", errors="replace")[:truncate]
    # Non-raw → md_converter.webpage (Markdown).
    res = await ctx.peers.spawn(
        MD_CONVERTER_AGENT_ID,
        {"url": url, "output_type": "content", "truncate": truncate},
        mode="webpage",
    )
    return (res.output.content if res.output else "") or ""


async def web_download(
    ctx: TaskContext, *, url: str, settings: SuiteSettings,
    get_bytes: BytesGetter | None = None,
) -> str:
    get = get_bytes or _default_get_bytes
    data = await get(url, settings.web_fetch_timeout_s, settings.web_fetch_max_bytes)
    name = PurePosixPath(urlparse(url).path).name or "download"
    return await ctx.files.store(data, filename=name)


def make_web_tools(settings: SuiteSettings) -> list[LocalTool]:
    async def _search(ctx: TaskContext, args: dict[str, Any]) -> str:
        return await web_search(
            args["query"], settings=settings,
            count=int(args.get("count", 5)),
            time_range=args.get("time_range"),
            language=args.get("language"),
        )

    async def _fetch(ctx: TaskContext, args: dict[str, Any]) -> str:
        return await html_fetch(
            ctx, url=args["url"], raw=bool(args.get("raw", False)),
            truncate=int(args.get("truncate", 2000)), settings=settings,
        )

    async def _download(ctx: TaskContext, args: dict[str, Any]) -> str:
        name = await web_download(ctx, url=args["url"], settings=settings)
        return f"Downloaded to the stash as {name}"

    return [
        LocalTool(
            spec=ToolSpec(
                name="web_search",
                description="Search the web. Returns top results (title, url, snippet).",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "count": {
                            "type": "integer",
                            "description": "Number of results (1-10, default 5).",
                            "minimum": 1, "maximum": 10,
                        },
                        "time_range": {
                            "type": "string",
                            "enum": ["day", "week", "month", "year"],
                            "description": "Restrict results by recency.",
                        },
                        "language": {
                            "type": "string",
                            "description": "Result language code, e.g. 'en', 'ko'.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            handler=_search,
        ),
        LocalTool(
            spec=ToolSpec(
                name="html_fetch",
                description=(
                    "Fetch a URL. raw=false (default) returns readable "
                    "Markdown; raw=true returns raw HTML."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "raw": {"type": "boolean"},
                        "truncate": {"type": "integer"},
                    },
                    "required": ["url"],
                },
            ),
            handler=_fetch,
        ),
        LocalTool(
            spec=ToolSpec(
                name="web_download",
                description="Download a URL to the file store; returns the saved name.",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
            handler=_download,
        ),
    ]
