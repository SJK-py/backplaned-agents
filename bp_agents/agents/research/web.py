"""research.web — web tools (search / fetch / download).

`web_search` is pluggable via `SUITE_WEB_SEARCH_BACKEND`:

* `searxng` (default) — a Brave-API-compatible JSON endpoint
  (`SUITE_SEARXNG_URL`); returns a classic list of result links. Because
  SearXNG only relays upstream snippets, it can optionally escalate to
  content ranking (fetch + embed + rank the top pages) — see
  `SUITE_WEB_SEARCH_DEEP` and `research.deepsearch`.
* `brave` — Brave's LLM-Context API (`SUITE_BRAVE_API_KEY`); returns
  AI-grounded context (title/url/snippets) for the query.
* `kagi` — Kagi's Search API (`SUITE_KAGI_API_KEY`); returns ranked results
  plus contextual collections (direct answer / weather up top, related
  questions / searches and infoboxes below), and routes `html_fetch` through
  Kagi's Extract API.
* `exa` — Exa's neural `/search` (`SUITE_EXA_API_KEY`); returns query-relevant
  highlight excerpts per result, and routes `html_fetch` through Exa's
  `/contents` (an `extract_query` becomes Exa's own query-focused summary).

The chosen backend's key must be set; if it isn't, the suite falls back to
SearXNG with a logged warning. `html_fetch` returns Markdown (or raw HTML for
`raw=true`) for a list of URLs — and, given an `extract_query`, distils each
page to just the query-relevant facts via the lite preset; `web_download`
saves a URL to the file store.
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
from bp_sdk import Message, ToolSpec

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

# Default and per-backend ceiling for the number of results (`_KAGI_MAX_COUNT`
# lives with the other kagi constants below). Maxes are unified at 20.
_DEFAULT_COUNT = 10
_SEARXNG_MAX_COUNT = 20
_BRAVE_MAX_COUNT = 20

BRAVE_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
KAGI_SEARCH_URL = "https://kagi.com/api/v1/search"
KAGI_EXTRACT_URL = "https://kagi.com/api/v1/extract"
_KAGI_EXTRACT_MAX_URLS = 10  # Kagi Extract accepts 1-10 pages per call.

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
_EXA_MAX_COUNT = 20

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
    if backend == "exa" and not settings.exa_api_key:
        logger.warning(
            "web_search_backend_key_missing",
            extra={"event": "web_search_backend_key_missing", "backend": "exa"},
        )
        return "searxng"
    return backend


async def _searxng_rows(
    query: str, *, settings: SuiteSettings, count: int,
    time_range: str | None = None, language: str | None = None,
    get_json: JsonGetter | None = None,
) -> list[dict[str, Any]]:
    """Fetch raw SearXNG results (capped at `count`), each carrying
    `title / url / content / score`. Shared by the plain snippet search and
    the deep content-ranking path. Returns `[]` when unconfigured/empty."""
    if not settings.searxng_url:
        return []
    count = min(max(count, 1), _SEARXNG_MAX_COUNT)
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
    return (data.get("results") or [])[:count]


def _format_searxng_rows(rows: list[dict[str, Any]], query: str) -> str:
    if not rows:
        return f"No results for {query!r}."
    return "\n\n".join(
        f"{i + 1}. {r.get('title', '')}\n{r.get('url', '')}\n{r.get('content', '')}"
        for i, r in enumerate(rows)
    )


async def _searxng_search(
    query: str, *, settings: SuiteSettings, count: int,
    time_range: str | None, language: str | None, get_json: JsonGetter | None,
) -> str:
    if not settings.searxng_url:
        return "Web search is not configured (no search backend set)."
    rows = await _searxng_rows(
        query, settings=settings, count=count, time_range=time_range,
        language=language, get_json=get_json,
    )
    return _format_searxng_rows(rows, query)


async def _brave_search(
    query: str, *, settings: SuiteSettings, count: int,
    country: str | None, search_language: str | None,
    freshness: str | None, local_city: str | None,
    request_json: ApiRequester | None,
) -> str:
    request = request_json or _default_request_json
    count = min(max(count, 1), _BRAVE_MAX_COUNT)
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


async def _exa_search(
    query: str, *, settings: SuiteSettings, count: int,
    include_domains: list[str] | None, exclude_domains: list[str] | None,
    max_age_hours: int | None, request_json: ApiRequester | None,
) -> str:
    """Exa neural /search with query-relevant highlights as the per-result
    snippet. `type` is fixed by `exa_search_type` (a config mechanism knob)."""
    request = request_json or _default_request_json
    count = min(max(count, 1), _EXA_MAX_COUNT)
    body: dict[str, Any] = {
        "query": query,
        "type": settings.exa_search_type or "auto",
        "numResults": count,
        "contents": {"highlights": True},
    }
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains
    if max_age_hours is not None:
        body["maxAgeHours"] = max_age_hours
    headers = {"x-api-key": settings.exa_api_key.get_secret_value()}
    data = await request(
        "POST", EXA_SEARCH_URL,
        json=body, headers=headers, timeout=settings.web_fetch_timeout_s,
    )
    results = (data.get("results") or [])[:count]
    if not results:
        return f"No results for {query!r}."
    blocks = []
    for i, r in enumerate(results):
        highlights = r.get("highlights") or []
        body_txt = "\n".join(h for h in highlights if h)
        blocks.append(f"{i + 1}. {r.get('title', '')}\n{r.get('url', '')}\n{body_txt}")
    return "\n\n".join(blocks)


async def web_search(
    query: str, *, settings: SuiteSettings, count: int = _DEFAULT_COUNT,
    time_range: str | None = None, language: str | None = None,
    country: str | None = None, search_language: str | None = None,
    freshness: str | None = None, local_city: str | None = None,
    kind: str | None = None, time_after: str | None = None,
    time_before: str | None = None, time_relative: str | None = None,
    region: str | None = None, file_type: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None, max_age_hours: int | None = None,
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
    if backend == "exa":
        return await _exa_search(
            query, settings=settings, count=count,
            include_domains=include_domains, exclude_domains=exclude_domains,
            max_age_hours=max_age_hours, request_json=request_json,
        )
    return await _searxng_search(
        query, settings=settings, count=count, time_range=time_range,
        language=language, get_json=get_json,
    )


async def _kagi_extract_pairs(
    urls: list[str], *, truncate: int, settings: SuiteSettings,
    request_json: ApiRequester | None,
) -> list[tuple[str, str]]:
    """Fetch + clean a batch of URLs via Kagi's Extract API, returning
    `(url, markdown-or-error-placeholder)` pairs. Pairs (not a joined string)
    so callers can post-process each page's content — e.g. run a query-focused
    distillation pass — before formatting."""
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
    pairs: list[tuple[str, str]] = []
    for item in items:
        url = item.get("url", "")
        md = item.get("markdown")
        body = md[:truncate] if md else (
            f"[Couldn't extract: {item.get('error') or 'unknown error'}]"
        )
        pairs.append((url, body))
    return pairs


async def _exa_contents_pairs(
    urls: list[str], *, truncate: int, settings: SuiteSettings,
    summary_query: str | None, request_json: ApiRequester | None,
) -> list[tuple[str, str]]:
    """Fetch clean content for `urls` via Exa's /contents, returning
    `(url, content)` pairs. With `summary_query` set, Exa returns a
    query-focused summary per page (server-side distillation — no extra LLM
    call on our side); otherwise it returns capped page text. Note: on
    /contents the content options are TOP-LEVEL (unlike /search, where they
    nest under `contents`)."""
    request = request_json or _default_request_json
    body: dict[str, Any] = {"urls": urls}
    if summary_query:
        body["summary"] = {"query": summary_query}
    else:
        body["text"] = {"maxCharacters": truncate}
    headers = {"x-api-key": settings.exa_api_key.get_secret_value()}
    data = await request(
        "POST", EXA_CONTENTS_URL,
        json=body, headers=headers, timeout=settings.web_fetch_timeout_s,
    )
    results = data.get("results") or []
    pairs: list[tuple[str, str]] = []
    for r in results:
        url = r.get("url", "")
        if summary_query:
            content = (r.get("summary") or "").strip() or "[No summary returned.]"
        else:
            content = (r.get("text") or "").strip() or "[No content extracted.]"
        pairs.append((url, content))
    return pairs


def _join_blocks(pairs: list[tuple[str, str]], *, headered: bool) -> str:
    if headered:
        return "\n\n".join(f"## {url}\n{body}" for url, body in pairs)
    return "\n\n".join(body for _, body in pairs)


# Query-focused distillation: turn a fetched page into just the facts that
# bear on the caller's question, so a long page doesn't flood the loop's
# context with boilerplate. Runs on the research agent's lite preset.
_EXTRACT_SYSTEM = (
    "You pull only the information relevant to the user's query out of a web "
    "page. Keep concrete facts, figures, dates, names, quotes, and any source "
    "URLs; drop navigation, ads, and unrelated sections. Be faithful — never "
    "add anything not present in the text. If nothing is relevant, say so in "
    "one line."
)


async def _distill(
    ctx: TaskContext, content: str, query: str, *,
    lite_preset: str | None, settings: SuiteSettings,
) -> str:
    content = content.strip()
    if not content:
        return "[No content to extract.]"
    preset = lite_preset or settings.default_preset_lite
    resp = await ctx.llm.generate(
        [Message(role="system", content=_EXTRACT_SYSTEM),
         Message(role="user", content=f"Query: {query}\n\nPage content:\n{content}")],
        preset=preset,
    )
    return resp.text.strip() or "[Nothing relevant found.]"


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
    truncate: int = 2000, extract_query: str | None = None,
    lite_preset: str | None = None, settings: SuiteSettings,
    get_bytes: BytesGetter | None = None, request_json: ApiRequester | None = None,
) -> str:
    if isinstance(urls, str):
        urls = [urls]
    # Dedup the model's URL list (order-preserving). Search backends already
    # dedup their own result lists; this guards the one case they can't — the
    # model asking for the same URL twice in a single fetch call.
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return "No URLs given."

    extracting = bool(extract_query) and not raw
    # In extract mode we read more of each page (the distiller compresses it
    # back down), so the fetch cap is the extract budget rather than `truncate`.
    fetch_cap = settings.web_extract_fetch_chars if extracting else truncate
    fetch_cap = min(max(fetch_cap, 0), _CONTENT_CAP)
    backend = _resolve_backend(settings)

    # Exa (non-raw): /contents returns final content in one call — capped text,
    # or a query-focused summary when extracting (Exa distills server-side, so
    # we skip our own lite-preset distillation pass below).
    if not raw and backend == "exa":
        pairs = await _exa_contents_pairs(
            urls, truncate=fetch_cap, settings=settings,
            summary_query=extract_query if extracting else None,
            request_json=request_json,
        )
        return _join_blocks(pairs, headered=True) or "No content extracted."

    # Gather (url, body) for every URL, backend-appropriately. Kagi (non-raw)
    # batches through Kagi Extract; everything else fetches per URL.
    kagi = not raw and backend == "kagi"
    if kagi:
        pairs = await _kagi_extract_pairs(
            urls, truncate=fetch_cap, settings=settings, request_json=request_json,
        )
    else:
        pairs = [
            (
                url,
                await _fetch_one(
                    ctx, url, raw=raw, truncate=fetch_cap,
                    settings=settings, get_bytes=get_bytes,
                ),
            )
            for url in urls
        ]

    if extracting:
        pairs = [
            (url, await _distill(
                ctx, body, extract_query, lite_preset=lite_preset, settings=settings,
            ))
            for url, body in pairs
        ]
        return _join_blocks(pairs, headered=True) or "No content extracted."

    # Header each block when fetching multiple URLs, or always for Kagi (its
    # blocks carried `## {url}` headers before this refactor — keep that).
    result = _join_blocks(pairs, headered=kagi or len(pairs) > 1)
    if kagi and not result:
        return "No content extracted."
    return result


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
                        "description": (
                            f"Number of results (1-{_BRAVE_MAX_COUNT}, "
                            f"default {_DEFAULT_COUNT})."
                        ),
                        "minimum": 1, "maximum": _BRAVE_MAX_COUNT,
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
                            f"Max results in the primary list (1-{_KAGI_MAX_COUNT}, "
                            f"default {_DEFAULT_COUNT}). Contextual collections are "
                            "capped tighter."
                        ),
                        "minimum": 1, "maximum": _KAGI_MAX_COUNT,
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
    if backend == "exa":
        return (
            "Web search via Exa's neural search: returns the most relevant "
            "results (title, url) each with query-relevant highlight excerpts.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {
                        "type": "integer",
                        "description": (
                            f"Number of results (1-{_EXA_MAX_COUNT}, "
                            f"default {_DEFAULT_COUNT})."
                        ),
                        "minimum": 1, "maximum": _EXA_MAX_COUNT,
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict results to these domains, e.g. ['arxiv.org'].",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Drop results from these domains.",
                    },
                    "max_age_hours": {
                        "type": "integer",
                        "description": (
                            "Freshness: livecrawl pages whose cached content is "
                            "older than this many hours (0 = always livecrawl)."
                        ),
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
                    "description": (
                        f"Number of results (1-{_SEARXNG_MAX_COUNT}, "
                        f"default {_DEFAULT_COUNT})."
                    ),
                    "minimum": 1, "maximum": _SEARXNG_MAX_COUNT,
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


_DEEP_POLICIES = {"off", "auto", "always", "model"}


def _deep_policy(settings: SuiteSettings) -> str:
    """Effective SearXNG deep-search policy (unknown values → 'auto')."""
    policy = (settings.web_search_deep or "auto").lower()
    return policy if policy in _DEEP_POLICIES else "auto"


def make_web_tools(
    settings: SuiteSettings, *,
    lite_preset: str | None = None, embedding_preset: str | None = None,
) -> list[LocalTool]:
    backend = _resolve_backend(settings)
    search_desc, search_params = _search_tool_schema(backend)
    policy = _deep_policy(settings)
    # Deep content ranking only exists for SearXNG (Brave/Kagi snippets are
    # already strong, and there's no deep pipeline for them).
    deep_enabled = (
        backend == "searxng" and policy != "off" and bool(settings.searxng_url)
    )
    if policy == "model" and backend != "searxng":
        logger.warning(
            "web_search_deep_unsupported_backend",
            extra={"event": "web_search_deep_unsupported_backend", "backend": backend},
        )

    async def _deep_rank(ctx: TaskContext, query: str, rows: list, count: int) -> str:
        from bp_agents.agents.research.deepsearch import deep_searxng_search  # noqa: PLC0415

        return await deep_searxng_search(
            ctx, query, rows=rows, count=count, settings=settings,
            embedding_preset=embedding_preset,
        )

    async def _search(ctx: TaskContext, args: dict[str, Any]) -> str:
        query = args["query"]
        count = int(args.get("count", _DEFAULT_COUNT))
        # SearXNG + deep policy: fetch a wider pool, then content-rank when the
        # policy forces it (`always`) or the snippets are too thin (`auto`).
        # Under `model` the base tool keeps the `auto` behaviour (a floor the
        # model can only raise, via the separate deep_web_search tool).
        if deep_enabled:
            from bp_agents.agents.research.deepsearch import snippets_too_thin  # noqa: PLC0415

            rows = await _searxng_rows(
                query, settings=settings,
                count=count * settings.web_deep_fetch_multiplier,
                time_range=args.get("time_range"), language=args.get("language"),
            )
            if not rows:
                return f"No results for {query!r}."
            if policy == "always" or snippets_too_thin(rows, settings):
                return await _deep_rank(ctx, query, rows, count)
            return _format_searxng_rows(rows[:count], query)
        return await web_search(
            query, settings=settings, count=count,
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
            include_domains=args.get("include_domains"),
            exclude_domains=args.get("exclude_domains"),
            max_age_hours=args.get("max_age_hours"),
        )

    async def _deep_search(ctx: TaskContext, args: dict[str, Any]) -> str:
        query = args["query"]
        count = int(args.get("count", _DEFAULT_COUNT))
        rows = await _searxng_rows(
            query, settings=settings,
            count=count * settings.web_deep_fetch_multiplier,
            time_range=args.get("time_range"), language=args.get("language"),
        )
        if not rows:
            return f"No results for {query!r}."
        return await _deep_rank(ctx, query, rows, count)

    async def _fetch(ctx: TaskContext, args: dict[str, Any]) -> str:
        return await html_fetch(
            ctx, urls=args["urls"], raw=bool(args.get("raw", False)),
            truncate=int(args.get("truncate", 2000)),
            extract_query=args.get("extract_query") or None,
            lite_preset=lite_preset, settings=settings,
        )

    async def _download(ctx: TaskContext, args: dict[str, Any]) -> str:
        name = await web_download(ctx, url=args["url"], settings=settings)
        return f"Downloaded to the stash as {name}"

    fetch_desc = (
        "Fetch one or more URLs. raw=false (default) returns readable Markdown; "
        "raw=true returns raw HTML. Set extract_query to pull just the facts "
        "relevant to a question from long pages — it reads more of each page "
        "and returns a distilled, query-focused summary instead of the raw "
        "text (ignored when raw=true)."
    )
    if backend == "kagi":
        fetch_desc += " (Markdown is extracted via Kagi's Extract API.)"
    elif backend == "exa":
        fetch_desc += (
            " (Content comes from Exa's /contents API; extract_query returns "
            "Exa's own query-focused summary.)"
        )

    tools = [
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
                        "extract_query": {
                            "type": "string",
                            "description": (
                                "Optional. A question/topic to distill each "
                                "page down to — returns only the relevant facts "
                                "instead of the full text. Use for long pages."
                            ),
                        },
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

    # Under the `model` policy, offer a second tool so the research model can
    # opt into a thorough, content-ranked search per query (the floor — plain
    # web_search — stays on `auto`). SearXNG-only.
    if deep_enabled and policy == "model":
        tools.append(
            LocalTool(
                spec=ToolSpec(
                    name="deep_web_search",
                    description=(
                        "Slower, thorough web search: fetches and content-ranks "
                        "the top results instead of trusting their snippets. Use "
                        "for research-grade or ambiguous queries where snippets "
                        "may mislead; prefer plain web_search otherwise."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "count": {
                                "type": "integer",
                                "description": (
                                    f"Number of pages to return (1-{_SEARXNG_MAX_COUNT}, "
                                    f"default {_DEFAULT_COUNT})."
                                ),
                                "minimum": 1, "maximum": _SEARXNG_MAX_COUNT,
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
                handler=_deep_search,
            )
        )
    return tools
