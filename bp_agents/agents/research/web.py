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
from bp_sdk import ToolSpec

if TYPE_CHECKING:
    from bp_agents.settings import SuiteSettings
    from bp_sdk import TaskContext

MD_CONVERTER_AGENT_ID = "md_converter"
_CONTENT_CAP = 100_000

# Injectable fetchers (overridden in tests).
JsonGetter = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]
BytesGetter = Callable[[str, float, int], Awaitable[bytes]]


async def _default_get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _default_get_bytes(url: str, timeout: float, cap: int) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise ValueError("download exceeds cap")
            return bytes(buf)


async def web_search(
    query: str, *, settings: SuiteSettings, get_json: JsonGetter | None = None
) -> str:
    if not settings.searxng_url:
        return "Web search is not configured (no search backend set)."
    get = get_json or _default_get_json
    data = await get(
        f"{settings.searxng_url.rstrip('/')}/search",
        {"q": query, "format": "json"},
        settings.web_fetch_timeout_s,
    )
    results = (data.get("results") or [])[:5]
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
        return await web_search(args["query"], settings=settings)

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
                    "properties": {"query": {"type": "string"}},
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
