"""research.web — web tools (search / fetch / download).

`web_search` is pluggable via `SUITE_WEB_SEARCH_BACKEND`:

* `searxng` (default) — a Brave-API-compatible JSON endpoint
  (`SUITE_SEARXNG_URL`); returns a classic list of result links.
* `brave` — Brave's LLM-Context API (`SUITE_BRAVE_API_KEY`); returns
  AI-grounded context (title/url/snippets) for the query.
* `kagi` — Kagi's Search API (`SUITE_KAGI_API_KEY`); returns ranked results
  plus contextual collections (direct answer / weather up top, related
  questions / searches and infoboxes below), and routes `html_fetch` through
  Kagi's Extract API.

The chosen backend's key must be set; if it isn't, the suite falls back to
SearXNG with a logged warning. `html_fetch` returns Markdown (or raw HTML for
`raw=true`) for a list of URLs; `web_download` saves a URL to the file store.
The core functions take injectable fetchers so they are testable without a
network; `make_web_tools` wraps them as `LocalTool`s closing over settings —
the `web_search` tool's parameter schema reflects the active backend.
"""

from __future__ import annotations

import html
import logging
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

logger = logging.getLogger(__name__)

MD_CONVERTER_AGENT_ID = "md_converter"
_CONTENT_CAP = 100_000
# Two-stage timeout: a dead/unreachable host fails the CONNECT fast instead of
# burning the full `web_fetch_timeout_s` (the read stays generous so a large,
# legitimately-slow download still completes).
_CONNECT_TIMEOUT_S = 5.0

BRAVE_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
KAGI_SEARCH_URL = "https://kagi.com/api/v1/search"
KAGI_EXTRACT_URL = "https://kagi.com/api/v1/extract"
_KAGI_EXTRACT_MAX_URLS = 10  # Kagi Extract accepts 1-10 pages per call.

# Kagi Search API: `data` is keyed by collection name; the `workflow` request
# field decides which collection is the primary result list.
_KAGI_WORKFLOW_PRIMARY = {
    "search": "search",
    "images": "image",
    "videos": "video",
    "news": "news",
    "podcasts": "podcast",
}
# Noisy index dumps we never surface.
_KAGI_DROP = {"interesting_news", "interesting_finds", "web_archive"}
# Contextual collections rendered above the primary list when present.
_KAGI_HEADER = ("direct_answer", "weather")
_KAGI_SNIPPET_CAP = 500  # Per-snippet character cap (after HTML-unescaping).
_KAGI_MAX_COUNT = 20
_KAGI_LABELS = {
    "search": "Search results",
    "image": "Images",
    "video": "Videos",
    "news": "News",
    "podcast": "Podcasts",
    "podcast_creator": "Podcast creators",
    "adjacent_question": "Related questions",
    "direct_answer": "Direct answer",
    "infobox": "Infobox",
    "code": "Code",
    "package_tracking": "Package tracking",
    "public_records": "Public records",
    "weather": "Weather",
    "related_search": "Related searches",
    "listicle": "Listicles",
}

# Injectable fetchers (overridden in tests).
JsonGetter = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]
BytesGetter = Callable[[str, float, int], Awaitable[bytes]]
ApiRequester = Callable[..., Awaitable[dict[str, Any]]]


_settings = load_suite_settings()
_FETCH_HEADERS = {"User-Agent": _settings.web_fetch_user_agent}


async def _default_get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    t = httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout))
    async with httpx.AsyncClient(timeout=t, headers=_FETCH_HEADERS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _default_request_json(
    method: str, url: str, *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    """Generic JSON request (used by the Brave/Kagi backends, which need custom
    auth headers and — for Kagi — POST bodies)."""
    t = httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout))
    merged = {**_FETCH_HEADERS, **(headers or {})}
    async with httpx.AsyncClient(timeout=t, headers=merged) as client:
        resp = await client.request(method, url, params=params, json=json)
        resp.raise_for_status()
        return resp.json()


async def _default_get_bytes(url: str, timeout: float, cap: int) -> bytes:
    # safe_stream_get re-validates each redirect hop against the SSRF guard.
    t = httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout))
    async with httpx.AsyncClient(timeout=t, headers=_FETCH_HEADERS) as client:
        return await safe_stream_get(
            client, url, cap=cap, max_redirects=_settings.web_fetch_max_redirects
        )


def _resolve_backend(settings: SuiteSettings) -> str:
    """The effective search backend. If the configured backend's API key is
    missing, fall back to SearXNG with a logged warning so the operator notices
    the misconfiguration without breaking research."""
    backend = (settings.web_search_backend or "searxng").lower()
    if backend == "brave" and not settings.brave_api_key:
        logger.warning(
            "web_search_backend_key_missing",
            extra={"event": "web_search_backend_key_missing", "backend": "brave"},
        )
        return "searxng"
    if backend == "kagi" and not settings.kagi_api_key:
        logger.warning(
            "web_search_backend_key_missing",
            extra={"event": "web_search_backend_key_missing", "backend": "kagi"},
        )
        return "searxng"
    return backend


async def _searxng_search(
    query: str, *, settings: SuiteSettings, count: int,
    time_range: str | None, language: str | None, get_json: JsonGetter | None,
) -> str:
    if not settings.searxng_url:
        return "Web search is not configured (no search backend set)."
    count = min(max(count, 1), 20)
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


async def _brave_search(
    query: str, *, settings: SuiteSettings, count: int,
    country: str | None, search_language: str | None,
    freshness: str | None, local_city: str | None,
    request_json: ApiRequester | None,
) -> str:
    request = request_json or _default_request_json
    count = min(max(count, 1), 20)
    params: dict[str, Any] = {"q": query, "count": count}
    if country:
        params["country"] = country
    if search_language:
        params["search_lang"] = search_language
    if freshness:
        params["freshness"] = freshness
    headers = {
        "X-Subscription-Token": settings.brave_api_key.get_secret_value(),
        "Accept": "application/json",
    }
    if local_city:
        # Location-aware search: Brave auto-enables `local` when a location
        # header is present.
        headers["X-Loc-City"] = local_city
    data = await request(
        "GET", BRAVE_CONTEXT_URL,
        params=params, headers=headers, timeout=settings.web_fetch_timeout_s,
    )
    generic = ((data.get("grounding") or {}).get("generic")) or []
    if not generic:
        return f"No results for {query!r}."
    blocks = []
    for i, item in enumerate(generic[:count]):
        snippets = item.get("snippets") or []
        body = "\n".join(s for s in snippets if s)
        blocks.append(
            f"{i + 1}. {item.get('title', '')}\n{item.get('url', '')}\n{body}"
        )
    return "\n\n".join(blocks)


def _kagi_label(name: str) -> str:
    return _KAGI_LABELS.get(name, name.replace("_", " ").capitalize())


def _kagi_result_line(n: int, item: dict[str, Any]) -> str:
    """One numbered result: title / url / snippet. `props` is intentionally
    dropped, and the snippet is HTML-unescaped then capped."""
    title = html.unescape(item.get("title") or "")
    url = item.get("url") or ""
    snippet = html.unescape(item.get("snippet") or "")[:_KAGI_SNIPPET_CAP]
    lines = [f"{n}. {title}".rstrip()]
    if url:
        lines.append(url)
    if snippet:
        lines.append(snippet)
    return "\n".join(lines)


def _kagi_collection_block(name: str, items: Any, limit: int) -> str:
    """Render a single collection (heading + up to `limit` results), or "" if
    it holds nothing usable."""
    if not isinstance(items, list):
        return ""
    rendered = [
        _kagi_result_line(i + 1, it)
        for i, it in enumerate(items[:limit])
        if isinstance(it, dict)
    ]
    if not rendered:
        return ""
    return f"## {_kagi_label(name)}\n" + "\n\n".join(rendered)


async def _kagi_search(
    query: str, *, settings: SuiteSettings, count: int,
    kind: str | None, time_after: str | None, time_before: str | None,
    time_relative: str | None, region: str | None, file_type: str | None,
    request_json: ApiRequester | None,
) -> str:
    request = request_json or _default_request_json
    workflow = (kind or "search").lower()
    if workflow not in _KAGI_WORKFLOW_PRIMARY:
        workflow = "search"
    count = min(max(count, 1), _KAGI_MAX_COUNT)
    # Contextual collections get a tighter cap so they don't drown the primary
    # list (but always at least one).
    aux_count = max(1, count // 3)

    body: dict[str, Any] = {"query": query, "workflow": workflow, "limit": count}
    # Restrictive options ride on an inline lens (the only place file_type /
    # relative-time live; takes priority over query-embedded operators).
    lens: dict[str, Any] = {}
    if file_type:
        lens["file_type"] = file_type
    if time_after:
        lens["time_after"] = time_after
    if time_before:
        lens["time_before"] = time_before
    if time_relative:
        lens["time_relative"] = time_relative
    if region:
        lens["search_region"] = region
    if lens:
        body["lens"] = lens

    headers = {"Authorization": f"Bearer {settings.kagi_api_key.get_secret_value()}"}
    data = await request(
        "POST", KAGI_SEARCH_URL,
        json=body, headers=headers, timeout=settings.web_fetch_timeout_s,
    )
    # Error payloads carry an empty `data` list; success uses a keyed object.
    collections = data.get("data")
    if not isinstance(collections, dict):
        return f"No results for {query!r}."

    primary = _KAGI_WORKFLOW_PRIMARY[workflow]
    header = [
        block for name in _KAGI_HEADER
        if (block := _kagi_collection_block(name, collections.get(name), aux_count))
    ]
    primary_block = _kagi_collection_block(primary, collections.get(primary), count)
    footer = [
        block
        for name, items in collections.items()
        if name != primary and name not in _KAGI_HEADER and name not in _KAGI_DROP
        and (block := _kagi_collection_block(name, items, aux_count))
    ]

    blocks = [b for b in (*header, primary_block, *footer) if b]
    if not blocks:
        return f"No results for {query!r}."
    return "\n\n".join(blocks)


async def web_search(
    query: str, *, settings: SuiteSettings, count: int = 10,
    time_range: str | None = None, language: str | None = None,
    country: str | None = None, search_language: str | None = None,
    freshness: str | None = None, local_city: str | None = None,
    kind: str | None = None, time_after: str | None = None,
    time_before: str | None = None, time_relative: str | None = None,
    region: str | None = None, file_type: str | None = None,
    get_json: JsonGetter | None = None, request_json: ApiRequester | None = None,
) -> str:
    backend = _resolve_backend(settings)
    if backend == "brave":
        return await _brave_search(
            query, settings=settings, count=count, country=country,
            search_language=search_language, freshness=freshness,
            local_city=local_city, request_json=request_json,
        )
    if backend == "kagi":
        return await _kagi_search(
            query, settings=settings, count=count, kind=kind,
            time_after=time_after, time_before=time_before,
            time_relative=time_relative, region=region, file_type=file_type,
            request_json=request_json,
        )
    return await _searxng_search(
        query, settings=settings, count=count, time_range=time_range,
        language=language, get_json=get_json,
    )


async def _kagi_extract(
    urls: list[str], *, truncate: int, settings: SuiteSettings,
    request_json: ApiRequester | None,
) -> str:
    """Fetch + clean a batch of URLs via Kagi's Extract API (returns Markdown)."""
    request = request_json or _default_request_json
    pages = [{"url": u} for u in urls[:_KAGI_EXTRACT_MAX_URLS]]
    headers = {"Authorization": f"Bearer {settings.kagi_api_key.get_secret_value()}"}
    data = await request(
        "POST", KAGI_EXTRACT_URL,
        # `format` selects the *response* serialization, not the per-page
        # content type — "json" keeps the `data[].markdown` envelope (markdown
        # is the extracted content); "markdown" would return a bare text body
        # that resp.json() can't parse.
        json={"pages": pages, "format": "json"},
        headers=headers, timeout=settings.web_fetch_timeout_s,
    )
    items = data.get("data") or []
    blocks = []
    for item in items:
        url = item.get("url", "")
        md = item.get("markdown")
        if md:
            blocks.append(f"## {url}\n{md[:truncate]}")
        else:
            blocks.append(f"## {url}\n[Couldn't extract: {item.get('error') or 'unknown error'}]")
    return "\n\n".join(blocks) or "No content extracted."


async def _fetch_one(
    ctx: TaskContext, url: str, *, raw: bool, truncate: int,
    settings: SuiteSettings, get_bytes: BytesGetter | None,
) -> str:
    if raw:
        get = get_bytes or _default_get_bytes
        data = await get(url, settings.web_fetch_timeout_s, settings.web_fetch_max_bytes)
        return data.decode("utf-8", errors="replace")[:truncate]
    res = await ctx.peers.spawn(
        MD_CONVERTER_AGENT_ID,
        {"url": url, "output_type": "content", "truncate": truncate},
        mode="webpage",
    )
    return (res.output.content if res.output else "") or ""


async def html_fetch(
    ctx: TaskContext, *, urls: list[str] | str, raw: bool = False,
    truncate: int = 2000, settings: SuiteSettings,
    get_bytes: BytesGetter | None = None, request_json: ApiRequester | None = None,
) -> str:
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return "No URLs given."
    truncate = min(max(truncate, 0), _CONTENT_CAP)

    # Kagi backend (non-raw) → batch through Kagi Extract.
    if not raw and _resolve_backend(settings) == "kagi":
        return await _kagi_extract(
            urls, truncate=truncate, settings=settings, request_json=request_json,
        )

    blocks = []
    for url in urls:
        body = await _fetch_one(
            ctx, url, raw=raw, truncate=truncate, settings=settings, get_bytes=get_bytes,
        )
        blocks.append(f"## {url}\n{body}" if len(urls) > 1 else body)
    return "\n\n".join(blocks)


async def web_download(
    ctx: TaskContext, *, url: str, settings: SuiteSettings,
    get_bytes: BytesGetter | None = None,
) -> str:
    get = get_bytes or _default_get_bytes
    data = await get(url, settings.web_fetch_timeout_s, settings.web_fetch_max_bytes)
    name = PurePosixPath(urlparse(url).path).name or "download"
    return await ctx.files.store(data, filename=name)


def _search_tool_schema(backend: str) -> tuple[str, dict[str, Any]]:
    """Description + JSON-schema for the `web_search` tool, tailored to the
    active backend (Brave/Kagi run an LLM under the hood, so they take/produce
    different things than a classic SearXNG link search)."""
    if backend == "brave":
        return (
            "Web search via Brave's LLM-Context API: returns AI-grounded "
            "context (title, url, snippets) for your query rather than a raw "
            "link list.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "country": {
                        "type": "string",
                        "description": "2-letter country code, e.g. 'us', 'kr'.",
                    },
                    "search_language": {
                        "type": "string",
                        "description": "Search language code, e.g. 'en', 'ko'.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (1-20, default 10).",
                        "minimum": 1, "maximum": 20,
                    },
                    "freshness": {
                        "type": "string",
                        "description": (
                            "Restrict by recency: 'pd' (24h), 'pw' (7d), "
                            "'pm' (31d), 'py' (year), or 'YYYY-MM-DDtoYYYY-MM-DD'."
                        ),
                    },
                    "local_city": {
                        "type": "string",
                        "description": "City for location-aware results, e.g. 'Seoul'.",
                    },
                },
                "required": ["query"],
            },
        )
    if backend == "kagi":
        return (
            "Web search via Kagi's Search API: returns ranked results (title, "
            "url, snippet) plus contextual collections — a direct answer or "
            "weather box up top, related questions / searches and infoboxes "
            "below.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["search", "images", "videos", "news", "podcasts"],
                        "description": "Result workflow (default 'search').",
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "Max results in the primary list (1-20, default 10). "
                            "Contextual collections are capped tighter."
                        ),
                        "minimum": 1, "maximum": 20,
                    },
                    "region": {
                        "type": "string",
                        "description": "2-letter country code to localize results, e.g. 'us', 'kr'.",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Restrict to a file type, e.g. 'pdf'.",
                    },
                    "time_relative": {
                        "type": "string",
                        "enum": ["day", "week", "month"],
                        "description": "Restrict to results from the last day/week/month.",
                    },
                    "time_after": {
                        "type": "string",
                        "description": "Only results updated/published after this date (YYYY-MM-DD).",
                    },
                    "time_before": {
                        "type": "string",
                        "description": "Only results updated/published before this date (YYYY-MM-DD).",
                    },
                },
                "required": ["query"],
            },
        )
    return (
        "Search the web. Returns top results (title, url, snippet).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {
                    "type": "integer",
                    "description": "Number of results (1-20, default 10).",
                    "minimum": 1, "maximum": 20,
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
    )


def make_web_tools(settings: SuiteSettings) -> list[LocalTool]:
    backend = _resolve_backend(settings)
    search_desc, search_params = _search_tool_schema(backend)

    async def _search(ctx: TaskContext, args: dict[str, Any]) -> str:
        return await web_search(
            args["query"], settings=settings,
            count=int(args.get("count", 10)),
            time_range=args.get("time_range"),
            language=args.get("language"),
            country=args.get("country"),
            search_language=args.get("search_language"),
            freshness=args.get("freshness"),
            local_city=args.get("local_city"),
            kind=args.get("kind"),
            time_after=args.get("time_after"),
            time_before=args.get("time_before"),
            time_relative=args.get("time_relative"),
            region=args.get("region"),
            file_type=args.get("file_type"),
        )

    async def _fetch(ctx: TaskContext, args: dict[str, Any]) -> str:
        return await html_fetch(
            ctx, urls=args["urls"], raw=bool(args.get("raw", False)),
            truncate=int(args.get("truncate", 2000)), settings=settings,
        )

    async def _download(ctx: TaskContext, args: dict[str, Any]) -> str:
        name = await web_download(ctx, url=args["url"], settings=settings)
        return f"Downloaded to the stash as {name}"

    fetch_desc = (
        "Fetch one or more URLs. raw=false (default) returns readable Markdown; "
        "raw=true returns raw HTML."
    )
    if backend == "kagi":
        fetch_desc += " (Markdown is extracted via Kagi's Extract API.)"

    return [
        LocalTool(
            spec=ToolSpec(
                name="web_search",
                description=search_desc,
                parameters=search_params,
            ),
            handler=_search,
        ),
        LocalTool(
            spec=ToolSpec(
                name="html_fetch",
                description=fetch_desc,
                parameters={
                    "type": "object",
                    "properties": {
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "One or more URLs to fetch.",
                        },
                        "raw": {"type": "boolean"},
                        "truncate": {"type": "integer"},
                    },
                    "required": ["urls"],
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
